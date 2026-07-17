import asyncio
import unittest

from runtime import KeyedLockPool, TaskSupervisor


class _Logger:
    def __init__(self):
        self.errors = []

    def error(self, message, *args, **kwargs):
        self.errors.append(message)


class TestKeyedLockPool(unittest.IsolatedAsyncioTestCase):
    async def test_same_key_is_serialized(self):
        pool = KeyedLockPool()
        active = 0
        max_active = 0

        async def worker():
            nonlocal active, max_active
            async with pool.hold(("persona_demo", "10001")):
                active += 1
                max_active = max(max_active, active)
                await asyncio.sleep(0.01)
                active -= 1

        await asyncio.gather(*(worker() for _ in range(8)))
        self.assertEqual(max_active, 1)
        self.assertEqual(pool.size, 0)

    async def test_different_keys_can_run_concurrently(self):
        pool = KeyedLockPool()
        active = 0
        max_active = 0

        async def worker(key):
            nonlocal active, max_active
            async with pool.hold(key):
                active += 1
                max_active = max(max_active, active)
                await asyncio.sleep(0.01)
                active -= 1

        await asyncio.gather(worker("a"), worker("b"))
        self.assertEqual(max_active, 2)


class TestTaskSupervisor(unittest.IsolatedAsyncioTestCase):
    async def test_close_cancels_persistent_and_flushes_transient(self):
        logger = _Logger()
        supervisor = TaskSupervisor(logger, name_prefix="test")
        persistent_cancelled = asyncio.Event()
        transient_finished = asyncio.Event()

        async def persistent():
            try:
                await asyncio.Event().wait()
            finally:
                persistent_cancelled.set()

        async def transient():
            await asyncio.sleep(0.01)
            transient_finished.set()

        supervisor.spawn(persistent(), name="persistent", persistent=True)
        supervisor.spawn(transient(), name="transient")
        await asyncio.sleep(0)
        await supervisor.close(flush_timeout=1.0)

        self.assertTrue(persistent_cancelled.is_set())
        self.assertTrue(transient_finished.is_set())
        self.assertEqual(supervisor.pending_count, 0)
        self.assertFalse(logger.errors)

    async def test_spawn_after_close_closes_coroutine(self):
        logger = _Logger()
        supervisor = TaskSupervisor(logger)
        await supervisor.close()

        async def noop():
            return None

        self.assertIsNone(supervisor.spawn(noop(), name="late"))


if __name__ == "__main__":
    unittest.main()
