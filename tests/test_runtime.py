"""并发原语的静默失效点：同一 key 必须串行且引用计数要回收、
清空/删除后旧 epoch 的写不得复活记录。

三者出问题都不会抛异常：竞态写覆盖、锁池/记录表无限增长、已清空的数据被排队中的
旧写重新写回，都要靠特定交错或事后比对才发现，所以各留一条断言。

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


class TestKeyedLockPool(unittest.IsolatedAsyncioTestCase):
    async def test_same_key_is_serialized(self):
        """守两种静默失效：同一 persona/user 的结算与命令修改并发交错（后写覆盖前写，
        数值悄悄算错），以及锁条目引用计数不回收（每个 key 永久留在池里）。"""
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
        """守「清空/恢复人格后旧写复活」：清空期间 try_epoch 必须挡住新写、把清空前
        取的 epoch 判为过期，清空后新写才放行。判错不报错，只会让已清空的记录回魂。"""
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
        """守单条记录的同类回归：删除用户记录后，删除前已排队的结算写入不得重建它
        （记录表里悄悄多出已删除用户，只能靠对账发现）。"""
        tracker = MutationEpochTracker()
        key = ("record", "persona_demo", "10001")
        old_epoch = tracker.snapshot(key)

        tracker.invalidate(key)

        self.assertFalse(tracker.is_current(key, old_epoch))
        self.assertTrue(tracker.is_current(key, tracker.snapshot(key)))

if __name__ == "__main__":
    unittest.main()
