"""运行期并发与生命周期工具。

本模块不依赖 AstrBot，便于使用标准库单元测试：

- :class:`TaskSupervisor` 持有 fire-and-forget 任务引用、消费异常，并在插件
  ``terminate()`` 时区分长期任务和短期可 flush 任务进行收尾。
- :class:`KeyedLockPool` 按业务 key 串行执行临界区，避免同一 persona/user 的
  LLM 结算、命令修改和 WebUI 修改乱序覆盖。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Awaitable, Hashable, Optional, TypeVar


T = TypeVar("T")


class TaskSupervisor:
    """管理插件创建的后台任务。"""

    def __init__(self, logger, *, name_prefix: str = "emotion_favour") -> None:
        self._logger = logger
        self._name_prefix = name_prefix
        self._transient: set[asyncio.Task] = set()
        self._persistent: set[asyncio.Task] = set()
        self._closing = False

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def pending_count(self) -> int:
        return sum(not task.done() for task in self._transient | self._persistent)

    def spawn(
        self,
        awaitable: Awaitable[T],
        *,
        name: str,
        persistent: bool = False,
    ) -> Optional[asyncio.Task[T]]:
        """创建并保活任务；关闭阶段拒绝新任务。"""
        if self._closing:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            return None

        task = asyncio.create_task(
            awaitable,
            name=f"{self._name_prefix}.{name}",
        )
        bucket = self._persistent if persistent else self._transient
        bucket.add(task)
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task: asyncio.Task) -> None:
        self._transient.discard(task)
        self._persistent.discard(task)
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            self._logger.error(
                f"[EmotionFavour] 后台任务 {task.get_name()} 异常: {exc}",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    async def close(self, *, flush_timeout: float = 8.0) -> None:
        """停止长期任务，并给短期任务一个有限的完成窗口。"""
        self._closing = True

        persistent = [task for task in self._persistent if not task.done()]
        for task in persistent:
            task.cancel()
        if persistent:
            await asyncio.gather(*persistent, return_exceptions=True)

        transient = [task for task in self._transient if not task.done()]
        if transient:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*transient, return_exceptions=True),
                    timeout=max(0.1, float(flush_timeout)),
                )
            except asyncio.TimeoutError:
                for task in transient:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*transient, return_exceptions=True)

        self._transient.clear()
        self._persistent.clear()


@dataclass
class _LockEntry:
    lock: asyncio.Lock
    users: int = 0


class KeyedLockPool:
    """引用计数的按 key 异步锁池。"""

    def __init__(self) -> None:
        self._entries: dict[Hashable, _LockEntry] = {}
        self._registry_lock = asyncio.Lock()

    @property
    def size(self) -> int:
        return len(self._entries)

    @asynccontextmanager
    async def hold(self, key: Hashable):
        async with self._registry_lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _LockEntry(lock=asyncio.Lock())
                self._entries[key] = entry
            entry.users += 1

        try:
            async with entry.lock:
                yield
        finally:
            async with self._registry_lock:
                entry.users -= 1
                if entry.users <= 0 and not entry.lock.locked():
                    self._entries.pop(key, None)
