import asyncio
import unittest

from runtime import (
    KeyedLockPool,
    MutationEpochTracker,
    PersonaMutationGate,
    TaskSupervisor,
)


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


class TestPersonaMutationGate(unittest.IsolatedAsyncioTestCase):
    async def test_exclusive_waits_for_active_database_write(self):
        gate = PersonaMutationGate()
        epoch = await gate.try_epoch("persona_demo")
        write_entered = asyncio.Event()
        release_write = asyncio.Event()
        exclusive_entered = asyncio.Event()

        async def writer():
            async with gate.write("persona_demo", expected_epoch=epoch) as admitted:
                self.assertTrue(admitted)
                write_entered.set()
                await release_write.wait()

        async def exclusive():
            async with gate.exclusive("persona_demo"):
                exclusive_entered.set()

        writer_task = asyncio.create_task(writer())
        await write_entered.wait()
        exclusive_task = asyncio.create_task(exclusive())
        await asyncio.sleep(0)
        self.assertFalse(exclusive_entered.is_set())

        release_write.set()
        await asyncio.gather(writer_task, exclusive_task)
        self.assertTrue(exclusive_entered.is_set())

    async def test_destructive_operation_invalidates_old_epoch(self):
        gate = PersonaMutationGate()
        old_epoch = await gate.try_epoch("persona_demo")
        async with gate.exclusive("persona_demo", invalidate=True):
            self.assertIsNone(await gate.try_epoch("persona_demo"))

        async with gate.write(
            "persona_demo", expected_epoch=old_epoch
        ) as admitted:
            self.assertFalse(admitted)

        new_epoch = await gate.try_epoch("persona_demo")
        self.assertNotEqual(old_epoch, new_epoch)
        async with gate.write(
            "persona_demo", expected_epoch=new_epoch
        ) as admitted:
            self.assertTrue(admitted)

    async def test_snapshot_does_not_invalidate_waiting_write(self):
        gate = PersonaMutationGate()
        epoch = await gate.try_epoch("persona_demo")
        snapshot_entered = asyncio.Event()
        release_snapshot = asyncio.Event()
        write_finished = asyncio.Event()

        async def snapshot():
            async with gate.exclusive("persona_demo"):
                snapshot_entered.set()
                await release_snapshot.wait()

        async def writer():
            async with gate.write(
                "persona_demo", expected_epoch=epoch
            ) as admitted:
                self.assertTrue(admitted)
                write_finished.set()

        snapshot_task = asyncio.create_task(snapshot())
        await snapshot_entered.wait()
        writer_task = asyncio.create_task(writer())
        await asyncio.sleep(0)
        self.assertFalse(write_finished.is_set())

        release_snapshot.set()
        await asyncio.gather(snapshot_task, writer_task)
        self.assertTrue(write_finished.is_set())


class TestMutationEpochTracker(unittest.TestCase):
    def test_invalidate_rejects_old_snapshot(self):
        tracker = MutationEpochTracker()
        key = ("record", "persona_demo", "10001")
        old_epoch = tracker.snapshot(key)

        tracker.invalidate(key)

        self.assertFalse(tracker.is_current(key, old_epoch))
        self.assertTrue(tracker.is_current(key, tracker.snapshot(key)))

    def test_keys_are_isolated(self):
        tracker = MutationEpochTracker()
        first = ("record", "persona_demo", "10001")
        second = ("record", "persona_demo", "10002")

        tracker.invalidate(first)

        self.assertEqual(tracker.snapshot(first), 1)
        self.assertEqual(tracker.snapshot(second), 0)


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
