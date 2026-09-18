"""并发原语的静默失效点：同一 key 必须串行且引用计数要回收、
清空/删除后旧 epoch 的写不得复活记录。

运行：
    python3 -m unittest tests.test_runtime -v
"""
import asyncio
import unittest

from runtime import (
    KeyedLockPool,
    MutationEpochTracker,
    PersonaMutationGate,
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

class TestPersonaMutationGate(unittest.IsolatedAsyncioTestCase):
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

class TestMutationEpochTracker(unittest.TestCase):
    def test_invalidate_rejects_old_snapshot(self):
        tracker = MutationEpochTracker()
        key = ("record", "persona_demo", "10001")
        old_epoch = tracker.snapshot(key)

        tracker.invalidate(key)

        self.assertFalse(tracker.is_current(key, old_epoch))
        self.assertTrue(tracker.is_current(key, tracker.snapshot(key)))

if __name__ == "__main__":
    unittest.main()
