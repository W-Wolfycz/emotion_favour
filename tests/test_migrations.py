"""db_migrations 的 SQLiteMigrationRunner 与三个迁移的本地直跑测试。

三个迁移只在「用户已有存量库」时才有实际动作，全新库永远走不到那条分支；本文件用
stdlib（sqlite3 + tempfile + zoneinfo）造存量库——缺 12 维情感列、缺
persona_tier_scripts 表、时间戳是本地 naive——再调用 storage.py 同款组装的真实 runner。

守的静默失效：时间戳位移方向反了或跑了两次 → 之后所有时间衰减系统性算错，界面上只是
一个看起来合理的数字；备份在迁移之后生成 → 真要回滚时拿到的是已经改坏的库；每次启动
重复备份 → 备份目录无限增长；迁移中途失败仍被记成「已应用」→ 半迁移 schema 永久保留、
下次启动不再重试。

运行：
    python3 -m unittest tests.test_migrations -v
"""
import importlib.util
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


def _load_shared():
    """按路径加载 tests/_shared.py（pytest 下不能按包名 import）。"""
    path = Path(__file__).resolve().parent / "_shared.py"
    spec = importlib.util.spec_from_file_location("ef_tests_shared", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

_shared = _load_shared()  # 顺带把插件目录放进 sys.path

from db_migrations import (  # noqa: E402
    MigrationError,
    SQLiteMigration,
    SQLiteMigrationRunner,
    add_display_name_column,
    add_emotion_columns,
    add_scar_columns,
    ensure_tier_scripts_table,
    normalize_utc_timestamps,
    validate_favour_schema,
)

LOCAL_TZ = ZoneInfo("Asia/Shanghai")

# 12 维情感列，与 db_migrations.EMOTION_COLUMNS 一致（存量库缺的就是这些）
EMOTION_COLUMNS = (
    "joy", "trust", "fear", "surprise", "sadness", "disgust",
    "anger", "anticipation", "pride", "guilt", "shame", "envy",
)

# 12 维伤痕列（v3.6.0 新增），存量库同样缺
SCAR_COLUMNS = tuple(f"scar_{name}" for name in EMOTION_COLUMNS)

MIGRATION_NAMES = (
    "add_emotion_columns_v1",
    "add_display_name_v1",
    "add_scar_columns_v1",
    "ensure_tier_scripts_table_v1",
    "normalize_utc_timestamps_v1",
)

# 存量库：没有 12 维情感列、没有 persona_tier_scripts、时间戳是本地 naive
LEGACY_SCHEMA = """
CREATE TABLE favour_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_id TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL,
    favour INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME,
    updated_at DATETIME,
    CONSTRAINT uq_persona_user UNIQUE (persona_id, user_id)
);
CREATE INDEX ix_favour_records_persona_id ON favour_records (persona_id);
CREATE TABLE persona_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_id TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    persona_hash TEXT NOT NULL DEFAULT '',
    updated_at DATETIME,
    CONSTRAINT uq_persona_summary_pid UNIQUE (persona_id)
);
INSERT INTO favour_records(persona_id, user_id, favour, created_at, updated_at)
    VALUES ('persona_demo', '10001', 42, '2026-01-02 03:04:05', '2026-03-04 05:06:07');
INSERT INTO persona_summaries(persona_id, summary, persona_hash, updated_at)
    VALUES ('persona_demo', 'summary', 'hash', '2026-02-03 04:05:06');
"""


class MigrationTestBase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data_dir = Path(temp.name)
        self.db_path = self.data_dir / "favour.db"

    def make_legacy_db(self):
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.executescript(LEGACY_SCHEMA)
            connection.commit()

    def make_runner(self, migrations=None, final_validator=validate_favour_schema):
        """按 storage.FavourDBManager.__init__ 的组装方式构造真实 runner。"""
        if migrations is None:
            migrations = (
                SQLiteMigration("add_emotion_columns_v1", add_emotion_columns),
                SQLiteMigration("add_display_name_v1", add_display_name_column),
                SQLiteMigration("add_scar_columns_v1", add_scar_columns),
                SQLiteMigration("ensure_tier_scripts_table_v1", ensure_tier_scripts_table),
                SQLiteMigration(
                    "normalize_utc_timestamps_v1",
                    lambda connection: normalize_utc_timestamps(
                        connection,
                        LOCAL_TZ,
                        self.data_dir / ".timestamps_utc_v1",
                    ),
                ),
            )
        return SQLiteMigrationRunner(
            self.db_path,
            self.data_dir / "backups",
            migrations,
            final_validator=final_validator,
        )

    def query(self, sql):
        with closing(sqlite3.connect(self.db_path)) as connection:
            return connection.execute(sql).fetchall()

    def columns(self, table):
        return tuple(
            str(row[1])
            for row in self.query(f'PRAGMA table_info("{table}")')
        )

    def tables(self):
        return {
            str(row[0])
            for row in self.query("SELECT name FROM sqlite_master WHERE type='table'")
        }

    def migration_names(self):
        if "emotion_favour_migrations" not in self.tables():
            return set()
        return {str(row[0]) for row in self.query("SELECT name FROM emotion_favour_migrations")}

    def backup_files(self):
        backup_dir = self.data_dir / "backups"
        if not backup_dir.exists():
            return []
        return sorted(path.name for path in backup_dir.iterdir())


class LegacyDatabaseUpgradeTest(MigrationTestBase):
    def test_legacy_database_is_upgraded_with_pre_migration_backup(self):
        """守「存量库升级后 schema 不全 / 备份是迁移后的库」：三个迁移都要真的应用、
        12 维列补齐、档位表建出来、final_validator 通过；备份必须还是迁移前的样子
        （否则回滚时拿到的是已经改坏的库，等真需要恢复才发现）。"""
        self.make_legacy_db()
        result = self.make_runner().run()

        self.assertEqual(result.applied_names, MIGRATION_NAMES)
        self.assertEqual(self.migration_names(), set(MIGRATION_NAMES))
        self.assertTrue(set(EMOTION_COLUMNS).issubset(set(self.columns("favour_records"))))
        self.assertTrue(set(SCAR_COLUMNS).issubset(set(self.columns("favour_records"))))
        self.assertIn("display_name", self.columns("favour_records"))
        self.assertIn("persona_tier_scripts", self.tables())

        backups = self.backup_files()
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0].startswith("pre_migration_"), backups[0])
        self.assertTrue(backups[0].endswith(".db"), backups[0])
        self.assertEqual(Path(result.backup_path).name, backups[0])

        # 备份必须是迁移前的库：没有新增情感列、没有档位表，且留有原始行
        with closing(sqlite3.connect(self.data_dir / "backups" / backups[0])) as backup:
            backup_columns = {
                str(row[1]) for row in backup.execute('PRAGMA table_info("favour_records")')
            }
            backup_tables = {
                str(row[0])
                for row in backup.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            row = backup.execute("SELECT favour FROM favour_records").fetchone()
        self.assertEqual(backup_columns & set(EMOTION_COLUMNS), set())
        self.assertEqual(backup_columns & set(SCAR_COLUMNS), set())
        self.assertNotIn("display_name", backup_columns)
        self.assertNotIn("persona_tier_scripts", backup_tables)
        self.assertEqual(row, (42,))

        # 旧行的情感列取默认 0，好感与其它字段不动；伤痕同样默认 0——
        # 若默认成非 0，升级后所有老用户会凭空带上伤痕，行为静默改变。
        self.assertEqual(self.query("SELECT favour FROM favour_records"), [(42,)])
        self.assertEqual(self.query("SELECT joy, envy FROM favour_records"), [(0, 0)])
        self.assertEqual(
            self.query("SELECT scar_trust, scar_anger FROM favour_records"), [(0, 0)]
        )
        # 昵称默认空串：升级那一刻管理台先只显示 ID，收到消息后才补
        self.assertEqual(self.query("SELECT display_name FROM favour_records"), [("",)])


class TimestampNormalizationTest(MigrationTestBase):
    def test_local_naive_timestamps_shift_back_to_utc_once(self):
        """守「位移方向反了/幅度错了」：Asia/Shanghai 本地 naive 必须减去 8 小时写成
        UTC naive。方向或幅度判错不会报错，只会让之后所有时间衰减整体偏快或偏慢，
        而且新库永远跑不到这条分支，本地以外几乎发现不了。"""
        self.make_legacy_db()
        self.make_runner().run()

        expected_created = (
            datetime(2026, 1, 2, 3, 4, 5, tzinfo=LOCAL_TZ)
            .astimezone(timezone.utc)
            .replace(tzinfo=None)
            .isoformat(sep=" ")
        )
        expected_updated = (
            datetime(2026, 3, 4, 5, 6, 7, tzinfo=LOCAL_TZ)
            .astimezone(timezone.utc)
            .replace(tzinfo=None)
            .isoformat(sep=" ")
        )
        self.assertLess(expected_created, "2026-01-02 03:04:05", "UTC+8 必须向前位移")
        self.assertEqual(
            self.query("SELECT created_at, updated_at FROM favour_records"),
            [(expected_created, expected_updated)],
        )
        self.assertEqual(
            self.query("SELECT updated_at FROM persona_summaries"),
            [(
                datetime(2026, 2, 3, 4, 5, 6, tzinfo=LOCAL_TZ)
                .astimezone(timezone.utc)
                .replace(tzinfo=None)
                .isoformat(sep=" "),
            )],
        )
        # 位移不得顺手动到同一行的其它字段
        self.assertEqual(self.query("SELECT favour, persona_id FROM favour_records"),
                         [(42, "persona_demo")])
        self.assertEqual(
            self.query("SELECT value FROM emotion_favour_meta WHERE key='timestamp_storage'"),
            [("utc_naive_v1",)],
        )


class RepeatedRunTest(MigrationTestBase):
    def test_second_run_applies_nothing_shifts_nothing_and_skips_backup(self):
        """守「幂等 marker 失效」：第二次启动必须 0 迁移、0 备份、时间戳原样。重复位移会
        让时间戳再退 8 小时（衰减凭空加速），重复备份会让目录随启动次数无限增长，
        两者都不会报错。"""
        self.make_legacy_db()
        runner = self.make_runner()
        first = runner.run()
        after_first = self.query("SELECT created_at, updated_at FROM favour_records")
        backups_after_first = self.backup_files()
        self.assertEqual(len(backups_after_first), 1)

        second = runner.run()
        self.assertEqual(second.applied_names, ())
        self.assertIsNone(second.backup_path)
        self.assertEqual(self.backup_files(), backups_after_first)
        self.assertEqual(self.query("SELECT created_at, updated_at FROM favour_records"),
                         after_first)
        self.assertEqual(self.migration_names(), set(MIGRATION_NAMES))
        self.assertIsNotNone(first.backup_path)

class FailedMigrationTest(MigrationTestBase):
    def test_failed_migration_rolls_back_schema_and_marker(self):
        """守「迁移失败被记成成功」：中途抛异常时，同一事务里的 DDL 与 marker 必须一起
        回滚，否则半迁移的 schema 会被当成已完成，之后每次启动都跳过，永远修不回来。"""
        self.make_legacy_db()

        def partial_then_fail(connection):
            connection.execute("CREATE TABLE probe_partial(id INTEGER)")
            raise RuntimeError("迁移中途失败")

        runner = self.make_runner(migrations=(
            SQLiteMigration(
                "first_ok",
                lambda connection: connection.execute("CREATE TABLE probe_first(id INTEGER)"),
            ),
            SQLiteMigration("second_boom", partial_then_fail),
        ))
        with self.assertRaises(MigrationError):
            runner.run()

        self.assertNotIn("probe_first", self.tables(), "失败迁移不得留下半成品表")
        self.assertNotIn("probe_partial", self.tables())
        self.assertEqual(self.migration_names(), set(), "失败迁移不得被记成已应用")


if __name__ == "__main__":
    unittest.main()
