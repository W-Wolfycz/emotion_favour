import asyncio
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

PLUGIN_PARENT = Path(__file__).resolve().parents[2]
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

try:
    from emotion_favour.storage import FavourDBManager
except (ImportError, ModuleNotFoundError):
    FavourDBManager = None


@unittest.skipIf(FavourDBManager is None, "当前 Python 环境未安装 AstrBot/SQLModel 运行依赖")
class TestFavourDBManager(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = FavourDBManager(
            Path(self.tempdir.name),
            -100,
            100,
            local_timezone="Asia/Shanghai",
        )
        await self.db.init_db()

    async def asyncTearDown(self):
        await self.db.close()
        self.tempdir.cleanup()

    async def test_concurrent_first_create_produces_one_record(self):
        results = await asyncio.gather(*(
            self.db.create_record("persona_demo", "10001", favour=index)
            for index in range(20)
        ))
        self.assertEqual(sum(bool(result) for result in results), 1)
        records = await self.db.get_global_records("persona_demo")
        self.assertEqual(len(records), 1)

    async def test_concurrent_emotion_delta_is_atomic(self):
        await self.db.create_record("persona_demo", "10001", favour=0)
        await asyncio.gather(*(
            self.db.update_favour(
                "persona_demo", "10001", emotion_updates={"joy": 1}
            )
            for _ in range(80)
        ))
        record = await self.db.get_favour("persona_demo", "10001")
        self.assertEqual(record.joy, 80)

    async def test_pagination(self):
        for index in range(12):
            await self.db.create_record(
                "persona_demo", str(10000 + index), favour=index
            )
        page = await self.db.list_records(
            "persona_demo", offset=5, limit=5, sort_by="favour", sort_order="desc"
        )
        self.assertEqual([record.favour for record in page], [6, 5, 4, 3, 2])
        self.assertEqual(await self.db.count_records("persona_demo"), 12)

    async def test_user_id_search_applies_to_count_and_page(self):
        for user_id in ("10001", "10002", "demo-user"):
            await self.db.create_record("persona_demo", user_id, favour=0)
        self.assertEqual(
            await self.db.count_records("persona_demo", user_id_search="1000"),
            2,
        )
        records = await self.db.list_records(
            "persona_demo",
            user_id_search="demo",
            sort_by="user_id",
            sort_order="asc",
        )
        self.assertEqual([record.user_id for record in records], ["demo-user"])


@unittest.skipIf(FavourDBManager is None, "当前 Python 环境未安装 AstrBot/SQLModel 运行依赖")
class TestUtcMigration(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_local_naive_is_converted_to_utc(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "favour.db"
            conn = sqlite3.connect(path)
            conn.execute("""
                CREATE TABLE favour_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    persona_id TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL,
                    favour INTEGER NOT NULL DEFAULT 0,
                    created_at DATETIME,
                    updated_at DATETIME,
                    CONSTRAINT uq_persona_user UNIQUE (persona_id, user_id)
                )
            """)
            conn.execute(
                "INSERT INTO favour_records(persona_id,user_id,favour,created_at,updated_at) VALUES(?,?,?,?,?)",
                ("persona_demo", "10001", 1, "2026-01-02 08:00:00", "2026-01-02 08:00:00"),
            )
            conn.commit()
            conn.close()

            manager = FavourDBManager(
                Path(tmp), local_timezone="Asia/Shanghai"
            )
            await manager.init_db()
            record = await manager.get_favour("persona_demo", "10001")
            self.assertEqual(record.updated_at, datetime(2026, 1, 2, 0, 0, 0))
            backups = list((Path(tmp) / "backups").glob("pre_utc_migration_*.db"))
            self.assertEqual(len(backups), 1)
            await manager.close()


if __name__ == "__main__":
    unittest.main()
