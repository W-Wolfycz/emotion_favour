import asyncio
import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

PLUGIN_PARENT = Path(__file__).resolve().parents[2]
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

import emotion_favour.storage as storage_module
from emotion_favour.storage import FavourDBManager
from emotion_favour.db_migrations import (
    MigrationError,
    SQLiteMigration,
    SQLiteMigrationRunner,
    validate_favour_schema,
)
from sqlalchemy import Column, String


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

    async def test_init_flag_remains_false_when_schema_validation_fails(self):
        original = self.db._validate_schema
        self.db._initialized = False
        self.db._validate_schema = lambda: (_ for _ in ()).throw(
            RuntimeError("schema invalid")
        )
        try:
            with self.assertRaisesRegex(RuntimeError, "schema invalid"):
                await self.db.init_db()
            self.assertFalse(self.db._initialized)
        finally:
            self.db._validate_schema = original

    async def test_backup_list_and_restore_merge(self):
        await self.db.create_record(
            "persona_demo", "10001", favour=20,
            emotions_absolute={"joy": 30, "trust": 40},
        )
        await self.db.create_record("persona_demo", "10002", favour=-10)
        records = await self.db.get_global_records("persona_demo")
        backup_path = await self.db.backup_data(records, "manual_persona")
        self.assertIsNotNone(backup_path)
        payload = json.loads(Path(backup_path).read_text(encoding="utf-8"))
        self.assertEqual(payload["format"], "emotion_favour_backup_v2")
        self.assertEqual(payload["timestamp_storage"], "utc_naive_v1")
        self.assertEqual(len(payload["records"]), 2)

        await self.db.set_record_fields(
            "persona_demo", "10001", favour=99,
            emotions_absolute={"joy": 1, "trust": 2},
        )
        await self.db.delete_favour("persona_demo", "10002")

        backups = await self.db.list_backups("persona_demo")
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0]["record_count"], 2)
        self.assertEqual(
            await self.db.get_backup_user_ids(
                Path(backup_path).name, "persona_demo"
            ),
            ("10001", "10002"),
        )
        existing = await self.db.get_existing_records(
            "persona_demo", ("10001", "missing_user")
        )
        self.assertEqual([record.user_id for record in existing], ["10001"])
        restored = await self.db.restore_backup(
            Path(backup_path).name, "persona_demo"
        )
        self.assertEqual(restored, 2)

        first = await self.db.get_favour("persona_demo", "10001")
        second = await self.db.get_favour("persona_demo", "10002")
        self.assertEqual((first.favour, first.joy, first.trust), (20, 30, 40))
        self.assertEqual(second.favour, -10)

    async def test_backup_persona_remains_available_after_clear(self):
        await self.db.create_record("persona_demo", "10001", favour=20)
        records = await self.db.get_global_records("persona_demo")
        backup_path = await self.db.backup_data(records, "manual_persona")
        self.assertIsNotNone(backup_path)

        self.assertTrue(await self.db.clear_persona("persona_demo"))
        self.assertNotIn("persona_demo", await self.db.get_distinct_personas())
        self.assertIn("persona_demo", await self.db.get_backup_personas())

    async def test_invalid_restore_keeps_existing_data_unchanged(self):
        await self.db.create_record(
            "persona_demo", "10001", favour=10,
            emotions_absolute={"joy": 20},
        )
        backup_dir = Path(self.tempdir.name) / "backups"
        backup_dir.mkdir(exist_ok=True)
        filename = "manual_persona_invalid_UTC.json"
        payload = {
            "format": "emotion_favour_backup_v2",
            "timestamp_storage": "utc_naive_v1",
            "records": [{
                "persona_id": "persona_demo",
                "user_id": "10001",
                "favour": 99,
                "joy": "invalid",
            }],
        }
        (backup_dir / filename).write_text(
            json.dumps(payload), encoding="utf-8"
        )

        with self.assertRaisesRegex(ValueError, "无效情感维度"):
            await self.db.restore_backup(filename, "persona_demo")
        record = await self.db.get_favour("persona_demo", "10001")
        self.assertEqual((record.favour, record.joy), (10, 20))

    async def test_backup_persona_scan_ignores_corrupt_files(self):
        await self.db.create_record("persona_demo", "10001", favour=10)
        records = await self.db.get_global_records("persona_demo")
        await self.db.backup_data(records, "manual_persona")
        backup_dir = Path(self.tempdir.name) / "backups"
        (backup_dir / "broken.json").write_text("{broken", encoding="utf-8")

        self.assertEqual(await self.db.get_backup_personas(), ["persona_demo"])

    async def test_restore_legacy_local_timestamps_as_utc(self):
        backup_dir = Path(self.tempdir.name) / "backups"
        backup_dir.mkdir(exist_ok=True)
        filename = "backup_all_database_20260102_080000.json"
        payload = [{
            "persona_id": "persona_demo",
            "user_id": "10001",
            "favour": 20,
            "created_at": "2026-01-02T08:00:00",
            "updated_at": "2026-01-02T08:00:00",
            **{dimension: 0 for dimension in storage_module.EMOTION_DIMENSIONS},
        }]
        (backup_dir / filename).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

        restored = await self.db.restore_backup(filename, "persona_demo")
        self.assertEqual(restored, 1)
        record = await self.db.get_favour("persona_demo", "10001")
        self.assertEqual(record.created_at, datetime(2026, 1, 2, 0, 0, 0))
        self.assertEqual(record.updated_at, datetime(2026, 1, 2, 0, 0, 0))

    async def test_restore_rejects_path_traversal(self):
        with self.assertRaisesRegex(ValueError, "备份文件名无效"):
            await self.db.restore_backup("../outside.json", "persona_demo")

    async def test_single_record_backup_preview_and_delete(self):
        await self.db.create_record(
            "persona_demo", "10001", favour=35,
            emotions_absolute={"joy": 12, "trust": 34},
        )
        records = await self.db.get_global_records("persona_demo")
        backup_path = await self.db.backup_data(records, "backup_user_10001")
        filename = Path(backup_path).name

        backups = await self.db.list_backups("persona_demo")
        self.assertEqual(backups[0]["preview"]["user_id"], "10001")
        self.assertEqual(backups[0]["preview"]["favour"], 35)
        self.assertEqual(backups[0]["preview"]["emotions"]["joy"], 12)
        self.assertEqual(backups[0]["preview"]["emotions"]["trust"], 34)

        await self.db.delete_backup(filename, "persona_demo")
        self.assertFalse(Path(backup_path).exists())
        self.assertEqual(await self.db.list_backups("persona_demo"), [])

    async def test_retention_only_removes_expired_valid_json(self):
        self.db.backup_retention_days = 1
        await self.db.create_record("persona_demo", "10001", favour=10)
        records = await self.db.get_global_records("persona_demo")
        backup_path = Path(await self.db.backup_data(records, "manual_persona"))
        backup_dir = backup_path.parent
        invalid_json = backup_dir / "unknown.json"
        sqlite_backup = backup_dir / "pre_migration_demo.db"
        invalid_json.write_text("{}", encoding="utf-8")
        sqlite_backup.write_bytes(b"sqlite backup placeholder")
        old_timestamp = datetime.now().timestamp() - (3 * 86400)
        for path in (backup_path, invalid_json, sqlite_backup):
            os.utime(path, (old_timestamp, old_timestamp))

        removed = await self.db.cleanup_expired_backups()

        self.assertEqual(removed, 1)
        self.assertFalse(backup_path.exists())
        self.assertTrue(invalid_json.exists())
        self.assertTrue(sqlite_backup.exists())

    async def test_retention_does_not_delete_backup_being_restored(self):
        self.db.backup_retention_days = 1
        await self.db.create_record("persona_demo", "10001", favour=10)
        records = await self.db.get_global_records("persona_demo")
        backup_path = Path(await self.db.backup_data(records, "manual_persona"))
        old_timestamp = datetime.now().timestamp() - (3 * 86400)
        os.utime(backup_path, (old_timestamp, old_timestamp))

        async with self.db.protect_backup(backup_path.name):
            self.assertEqual(await self.db.cleanup_expired_backups(), 0)
            self.assertTrue(backup_path.exists())

        self.assertEqual(await self.db.cleanup_expired_backups(), 1)
        self.assertFalse(backup_path.exists())


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
            backups = list((Path(tmp) / "backups").glob("pre_migration_*.db"))
            self.assertEqual(len(backups), 1)
            await manager.close()

    async def test_aware_timestamp_keeps_its_real_utc_instant(self):
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
                (
                    "persona_demo",
                    "10001",
                    1,
                    "2026-01-02T00:00:00+00:00",
                    "2026-01-02T08:00:00+08:00",
                ),
            )
            conn.commit()
            conn.close()

            manager = FavourDBManager(
                Path(tmp), local_timezone="Asia/Shanghai"
            )
            await manager.init_db()
            record = await manager.get_favour("persona_demo", "10001")
            self.assertEqual(record.created_at, datetime(2026, 1, 2, 0, 0, 0))
            self.assertEqual(record.updated_at, datetime(2026, 1, 2, 0, 0, 0))
            await manager.close()


class TestSqlModelHotReload(unittest.TestCase):
    def test_removed_legacy_columns_do_not_survive_reload(self):
        table = storage_module.FavourRecord.__table__
        table.append_column(Column("session_id", String(), nullable=True))
        self.assertIn("session_id", table.columns)

        reloaded = importlib.reload(storage_module)
        self.assertNotIn("session_id", reloaded.FavourRecord.__table__.columns)
        self.assertEqual(
            list(reloaded.FavourRecord.__table__.columns.keys()),
            [
                "id", "persona_id", "user_id", "favour", "created_at", "updated_at",
                "joy", "trust", "fear", "surprise", "sadness", "disgust", "anger",
                "anticipation", "pride", "guilt", "shame", "envy",
            ],
        )


class TestSQLiteMigrationRunner(unittest.TestCase):
    def test_runner_records_migration_and_creates_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "favour.db"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE favour_records (id INTEGER PRIMARY KEY, persona_id TEXT, user_id TEXT)")
            conn.execute("INSERT INTO favour_records VALUES (1, 'persona_demo', '10001')")
            conn.commit()
            conn.close()

            runner = SQLiteMigrationRunner(
                db_path,
                root / "backups",
                (SQLiteMigration("add_marker", lambda c: c.execute("CREATE TABLE marker (id INTEGER)")),),
            )
            result = runner.run()
            self.assertEqual(result.applied_names, ("add_marker",))
            self.assertIsNotNone(result.backup_path)
            self.assertTrue(result.backup_path.exists())
            self.assertEqual(runner.run().applied_names, ())

    def test_backup_precedes_migration_bookkeeping_and_is_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "favour.db"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE legacy_data (id INTEGER PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO legacy_data(value) VALUES ('kept')")
            conn.commit()
            conn.close()

            def fail_after_backup(connection):
                connection.execute("CREATE TABLE partial_change (id INTEGER)")
                raise RuntimeError("stop")

            runner = SQLiteMigrationRunner(
                db_path,
                root / "backups",
                (SQLiteMigration("failing", fail_after_backup),),
            )
            with self.assertRaises(MigrationError):
                runner.run()

            backups = list((root / "backups").glob("pre_migration_*.db"))
            self.assertEqual(len(backups), 1)
            backup = sqlite3.connect(backups[0])
            try:
                self.assertEqual(backup.execute("PRAGMA quick_check").fetchone()[0], "ok")
                tables = {
                    row[0]
                    for row in backup.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertIn("legacy_data", tables)
                self.assertNotIn("emotion_favour_migrations", tables)
                self.assertNotIn("partial_change", tables)
            finally:
                backup.close()

            live = sqlite3.connect(db_path)
            try:
                live_tables = {
                    row[0]
                    for row in live.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertIn("legacy_data", live_tables)
                self.assertNotIn("emotion_favour_migrations", live_tables)
                self.assertNotIn("partial_change", live_tables)
                self.assertEqual(
                    live.execute("SELECT value FROM legacy_data").fetchone()[0],
                    "kept",
                )
            finally:
                live.close()

    def test_schema_validator_reports_missing_column(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as handle:
            conn = sqlite3.connect(handle.name)
            conn.execute("CREATE TABLE favour_records (id INTEGER PRIMARY KEY)")
            conn.execute("CREATE TABLE persona_summaries (id INTEGER PRIMARY KEY)")
            conn.commit()
            with self.assertRaises(MigrationError):
                validate_favour_schema(conn)
            conn.close()


if __name__ == "__main__":
    unittest.main()
