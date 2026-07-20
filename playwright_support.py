"""Playwright Chromium 自修复工具。

本模块不依赖 AstrBot，便于在标准库测试中验证错误识别和子进程调用。
"""

from __future__ import annotations

import asyncio
import sys


CHROMIUM_INSTALL_TIMEOUT_SECONDS = 600.0


class ChromiumInstallError(RuntimeError):
    """自动安装 Chromium 失败。"""


def is_missing_chromium_error(exc: BaseException) -> bool:
    """只识别 Playwright 明确报告浏览器可执行文件缺失的错误。"""
    message = str(exc).casefold()
    return (
        "executable doesn't exist" in message
        and (
            "playwright install" in message
            or "download new browsers" in message
        )
    )


def _output_tail(output: bytes | None, *, limit: int = 2000) -> str:
    if not output:
        return ""
    text = output.decode(errors="replace").strip()
    return text[-limit:]


async def _kill_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.kill()
    except ProcessLookupError:
        pass
    await process.wait()


async def install_chromium(
    *,
    python_executable: str = sys.executable,
    timeout: float = CHROMIUM_INSTALL_TIMEOUT_SECONDS,
) -> str:
    """使用当前 Python 环境下载 Playwright Chromium，返回安装器输出尾部。"""
    process = await asyncio.create_subprocess_exec(
        python_executable,
        "-m",
        "playwright",
        "install",
        "chromium",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(
            process.communicate(),
            timeout=max(1.0, float(timeout)),
        )
    except asyncio.TimeoutError as exc:
        await _kill_process(process)
        raise ChromiumInstallError(
            f"Chromium 自动安装超过 {float(timeout):g} 秒，已终止安装进程；"
            "可在 AstrBot Python 环境手动执行 `python -m playwright install chromium`"
        ) from exc
    except asyncio.CancelledError:
        await _kill_process(process)
        raise

    output_tail = _output_tail(output)
    if process.returncode != 0:
        detail = f"；安装器输出：{output_tail}" if output_tail else ""
        raise ChromiumInstallError(
            f"Chromium 自动安装失败，退出码 {process.returncode}{detail}；"
            "可在 AstrBot Python 环境手动执行 `python -m playwright install chromium`"
        )
    return output_tail
