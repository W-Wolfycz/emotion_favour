"""emotion_favour 的 SQLite 迁移 runner 与 schema 校验。

迁移只负责结构和数据升级；SQLModel/aiosqlite 连接仍由 storage.py 管理。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence


class MigrationError(RuntimeError):
    """数据库迁移或校验失败。"""


@dataclass(frozen=True)
class SQLiteMigration:
    name: str
    apply: Callable[[sqlite3.Connection], None]
    validate: Optional[Callable[[sqlite3.Connection], None]] = None


@dataclass(frozen=True)
class MigrationResult:
    applied_names: tuple[str, ...]
    backup_path: Optional[Path]


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone() is not None


def column_names(connection: sqlite3.Connection, table_name: str) -> tuple[str, ...]:
    return tuple(
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table_name}")')
    )


def unique_index_columns(
    connection: sqlite3.Connection,
    table_name: str,
) -> set[tuple[str, ...]]:
    result: set[tuple[str, ...]] = set()
    for row in connection.execute(f'PRAGMA index_list("{table_name}")'):
        if not bool(row[2]):
            continue
        index_name = str(row[1]).replace('"', '""')
        columns = tuple(
            str(index_row[2])
            for index_row in connection.execute(f'PRAGMA index_info("{index_name}")')
        )
        result.add(columns)
    return result


def require_columns(
    connection: sqlite3.Connection,
    table_name: str,
    expected: Iterable[str],
) -> None:
    if not table_exists(connection, table_name):
        raise MigrationError(f"缺少数据库表: {table_name}")
    existing = set(column_names(connection, table_name))
    missing = sorted(set(expected) - existing)
    if missing:
        raise MigrationError(f"表 {table_name} 缺少列: {', '.join(missing)}")


def validate_sqlite_integrity(connection: sqlite3.Connection) -> None:
    """执行只读完整性检查；发现损坏时停止后续迁移。"""
    rows = connection.execute("PRAGMA quick_check").fetchall()
    if rows != [("ok",)]:
        raise MigrationError(
            f"SQLite 完整性检查失败（{len(rows)} 项），已停止数据库迁移"
        )


def _parse_datetime(value, *, naive_timezone=None) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    if naive_timezone is not None:
        return (
            parsed.replace(tzinfo=naive_timezone)
            .astimezone(timezone.utc)
            .replace(tzinfo=None)
        )
    return parsed


class SQLiteMigrationRunner:
    """带备份、事务、迁移记录和最终校验的同步 SQLite runner。"""

    def __init__(
        self,
        db_path: Path,
        backup_dir: Path,
        migrations: Sequence[SQLiteMigration],
        final_validator: Optional[Callable[[sqlite3.Connection], None]] = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.backup_dir = Path(backup_dir)
        self.migrations = tuple(migrations)
        self.final_validator = final_validator
        names = [migration.name for migration in self.migrations]
        if len(names) != len(set(names)):
            raise ValueError("SQLite migration names must be unique")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _backup(self, source: sqlite3.Connection) -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%fZ")
        target = self.backup_dir / f"pre_migration_{stamp}.db"
        temporary = target.with_suffix(".db.tmp")
        try:
            with closing(sqlite3.connect(temporary)) as destination:
                source.backup(destination)
                destination.commit()
                validate_sqlite_integrity(destination)
            temporary.replace(target)
            return target
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def run(self) -> MigrationResult:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            migration_table_exists = table_exists(
                connection, "emotion_favour_migrations"
            )
            applied = set()
            if migration_table_exists:
                applied = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM emotion_favour_migrations"
                    )
                }
            pending = [migration for migration in self.migrations if migration.name not in applied]
            if not pending:
                if (
                    self.final_validator is not None
                    and table_exists(connection, "favour_records")
                    and table_exists(connection, "persona_summaries")
                ):
                    self.final_validator(connection)
                validate_sqlite_integrity(connection)
                return MigrationResult((), None)

            has_data = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name != 'emotion_favour_migrations' LIMIT 1"
            ).fetchone() is not None
            if has_data:
                validate_sqlite_integrity(connection)
            backup_path = self._backup(connection) if has_data else None
            applied_names: list[str] = []
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS emotion_favour_migrations ("
                    "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                for migration in pending:
                    migration.apply(connection)
                    if migration.validate is not None:
                        migration.validate(connection)
                    connection.execute(
                        "INSERT INTO emotion_favour_migrations(name, applied_at) VALUES (?, ?)",
                        (migration.name, datetime.now(timezone.utc).isoformat()),
                    )
                    applied_names.append(migration.name)
                if (
                    self.final_validator is not None
                    and table_exists(connection, "favour_records")
                    and table_exists(connection, "persona_summaries")
                ):
                    self.final_validator(connection)
                validate_sqlite_integrity(connection)
                connection.commit()
            except Exception as exc:
                connection.rollback()
                raise MigrationError(f"数据库迁移失败: {exc}") from exc
            return MigrationResult(tuple(applied_names), backup_path)


FAVOUR_COLUMNS = (
    "id", "persona_id", "user_id", "favour", "created_at", "updated_at",
    "joy", "trust", "fear", "surprise", "sadness", "disgust", "anger",
    "anticipation", "pride", "guilt", "shame", "envy",
)
PERSONA_SUMMARY_COLUMNS = ("id", "persona_id", "summary", "persona_hash", "updated_at")


def validate_favour_schema(connection: sqlite3.Connection) -> None:
    """验证当前 ORM 依赖的表、列和唯一约束；允许保留未知旧列。"""
    require_columns(connection, "favour_records", FAVOUR_COLUMNS)
    require_columns(connection, "persona_summaries", PERSONA_SUMMARY_COLUMNS)
    if ("persona_id", "user_id") not in unique_index_columns(connection, "favour_records"):
        raise MigrationError("favour_records 缺少 (persona_id, user_id) 唯一约束")
    if ("persona_id",) not in unique_index_columns(connection, "persona_summaries"):
        raise MigrationError("persona_summaries 缺少 persona_id 唯一约束")


def add_emotion_columns(connection: sqlite3.Connection) -> None:
    if not table_exists(connection, "favour_records"):
        return
    existing = set(column_names(connection, "favour_records"))
    for name in FAVOUR_COLUMNS[6:]:
        if name not in existing:
            connection.execute(f'ALTER TABLE favour_records ADD COLUMN "{name}" INTEGER DEFAULT 0')


def normalize_utc_timestamps(
    connection: sqlite3.Connection,
    local_timezone,
    legacy_marker: Optional[Path] = None,
) -> None:
    """迁移旧 naive 本地时间；已完成旧标记时保持幂等。"""
    if not table_exists(connection, "favour_records"):
        return
    if table_exists(connection, "emotion_favour_meta"):
        marker = connection.execute(
            "SELECT value FROM emotion_favour_meta WHERE key='timestamp_storage'"
        ).fetchone()
        if marker and marker[0] == "utc_naive_v1":
            return
    if legacy_marker is not None and legacy_marker.exists():
        connection.execute(
            "CREATE TABLE IF NOT EXISTS emotion_favour_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')"
        )
        connection.execute(
            "INSERT INTO emotion_favour_meta(key,value) VALUES('timestamp_storage','utc_naive_v1') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        return
    for table_name, columns in (
        ("favour_records", ("created_at", "updated_at")),
        ("persona_summaries", ("updated_at",)),
    ):
        if not table_exists(connection, table_name):
            continue
        rows = connection.execute(
            f'SELECT id, {", ".join(columns)} FROM "{table_name}"'
        ).fetchall()
        for row in rows:
            values = {}
            for index, column in enumerate(columns, start=1):
                old = _parse_datetime(
                    row[index],
                    naive_timezone=local_timezone,
                )
                if old is not None:
                    values[column] = old.isoformat(sep=" ")
            if values:
                assignment = ", ".join(f'"{key}"=?' for key in values)
                connection.execute(
                    f'UPDATE "{table_name}" SET {assignment} WHERE id=?',
                    (*values.values(), row[0]),
                )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS emotion_favour_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')"
    )
    connection.execute(
        "INSERT INTO emotion_favour_meta(key,value) VALUES('timestamp_storage','utc_naive_v1') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )
