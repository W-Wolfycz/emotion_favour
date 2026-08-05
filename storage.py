# storage.py
import json
import string
import asyncio
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional, Tuple
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from aiofiles import open as aio_open
from sqlmodel import SQLModel, Field, select, delete
from sqlalchemy import UniqueConstraint, event, func, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from .domain import (
    EMOTION_DIMENSIONS,
    EMOTION_DISPLAY_NAMES,
    EMOTION_GROUPS,
    TICK_SECONDS,
    TONE_INSTRUCTIONS,
    apply_emotion_decay,
    build_emotion_panel,
    build_injection_prompt,
    build_system_prompt_extra,
    build_tone_instruction,
    compute_favour_decay,
    compute_relationship_progress,
    diminish_delta,
    format_emotion_detail,
    get_dominant_emotions,
    utc_now,
)
from .log import logger
from .db_migrations import (
    SQLiteMigration,
    SQLiteMigrationRunner,
    add_emotion_columns,
    normalize_utc_timestamps,
    validate_favour_schema,
)


_METADATA_FIELDS = frozenset({
    'id', 'persona_id', 'user_id', 'favour', 'created_at', 'updated_at',
})
_BACKUP_FORMAT = "emotion_favour_backup_v2"
_TIMESTAMP_STORAGE_UTC = "utc_naive_v1"
_TIMESTAMP_STORAGE_LEGACY_LOCAL = "legacy_local_naive"


def _parse_db_datetime(value, *, naive_timezone=None) -> Optional[datetime]:
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

def _is_valid_userid(userid: str) -> bool:
    if not userid or len(userid.strip()) == 0:
        return False
    userid = userid.strip()
    if len(userid) > 64:
        return False
    allowed_chars = string.ascii_letters + string.digits + "_-:@."
    return all(c in allowed_chars for c in userid)


def _reset_plugin_table_metadata(*table_names: str) -> None:
    """热重载前移除旧 Table 映射，避免已删除字段残留在 SQLModel metadata。"""
    for table_name in table_names:
        existing = SQLModel.metadata.tables.get(table_name)
        if existing is not None:
            SQLModel.metadata.remove(existing)


_reset_plugin_table_metadata("favour_records", "persona_summaries")


class FavourRecord(SQLModel, table=True):
    __tablename__ = "favour_records"
    __table_args__ = (
        UniqueConstraint("persona_id", "user_id", name="uq_persona_user"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    persona_id: str = Field(default="", index=True)
    user_id: str = Field(index=True)
    favour: int = Field(default=0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    # 12 维情感 (0-100)
    joy: int = Field(default=0)
    trust: int = Field(default=0)
    fear: int = Field(default=0)
    surprise: int = Field(default=0)
    sadness: int = Field(default=0)
    disgust: int = Field(default=0)
    anger: int = Field(default=0)
    anticipation: int = Field(default=0)
    pride: int = Field(default=0)
    guilt: int = Field(default=0)
    shame: int = Field(default=0)
    envy: int = Field(default=0)


class PersonaSummaryRecord(SQLModel, table=True):
    __tablename__ = "persona_summaries"

    id: Optional[int] = Field(default=None, primary_key=True)
    persona_id: str = Field(default="", unique=True, index=True)
    summary: str = Field(default="")
    persona_hash: str = Field(default="")
    updated_at: datetime = Field(default_factory=utc_now)


class FavourDBManager:
    def __init__(
        self,
        data_dir: Path,
        min_val: int = -100,
        max_val: int = 100,
        *,
        local_timezone: str = "Asia/Shanghai",
        backup_retention_days: int = 0,
    ):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "favour.db"
        self.db_url = f"sqlite+aiosqlite:///{self.db_path}"
        self.min_val = min_val
        self.max_val = max_val
        try:
            retention_days = int(backup_retention_days)
        except (TypeError, ValueError):
            retention_days = 0
        self.backup_retention_days = max(0, retention_days)
        try:
            self.local_tz = ZoneInfo(local_timezone)
        except Exception:
            self.local_tz = ZoneInfo("Asia/Shanghai")

        self.engine = create_async_engine(self.db_url, echo=False)

        @event.listens_for(self.engine.sync_engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        self.async_session = sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        self._initialized = False
        self._init_lock = asyncio.Lock()
        self._backup_lock = asyncio.Lock()
        self._protected_backup_files: dict[str, int] = {}
        self._migration_runner = SQLiteMigrationRunner(
            self.db_path,
            self.data_dir / "backups",
            (
                SQLiteMigration("add_emotion_columns_v1", add_emotion_columns),
                SQLiteMigration(
                    "normalize_utc_timestamps_v1",
                    lambda connection: normalize_utc_timestamps(
                        connection,
                        self.local_tz,
                        self.data_dir / ".timestamps_utc_v1",
                    ),
                ),
            ),
            final_validator=validate_favour_schema,
        )

    async def init_db(self):
        if self._initialized:
            return

        async with self._init_lock:
            if self._initialized:
                return

            try:
                migration_result = await asyncio.to_thread(self._migration_runner.run)
                if migration_result.backup_path:
                    logger.info(f"数据库迁移前备份完成: {migration_result.backup_path}")
                async with self.engine.begin() as conn:
                    # 检查表是否已存在
                    result = await conn.execute(
                        text("SELECT name FROM sqlite_master WHERE type='table' AND name='favour_records'")
                    )
                    table_exists = result.fetchone() is not None

                    if not table_exists:
                        # 全新安装：用 CREATE TABLE IF NOT EXISTS，避免索引冲突
                        await conn.execute(text("""
                            CREATE TABLE IF NOT EXISTS favour_records (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                persona_id TEXT NOT NULL DEFAULT '',
                                user_id TEXT NOT NULL,
                                favour INTEGER NOT NULL DEFAULT 0,
                                created_at DATETIME,
                                updated_at DATETIME,
                                joy INTEGER NOT NULL DEFAULT 0,
                                trust INTEGER NOT NULL DEFAULT 0,
                                fear INTEGER NOT NULL DEFAULT 0,
                                surprise INTEGER NOT NULL DEFAULT 0,
                                sadness INTEGER NOT NULL DEFAULT 0,
                                disgust INTEGER NOT NULL DEFAULT 0,
                                anger INTEGER NOT NULL DEFAULT 0,
                                anticipation INTEGER NOT NULL DEFAULT 0,
                                pride INTEGER NOT NULL DEFAULT 0,
                                guilt INTEGER NOT NULL DEFAULT 0,
                                shame INTEGER NOT NULL DEFAULT 0,
                                envy INTEGER NOT NULL DEFAULT 0,
                                CONSTRAINT uq_persona_user UNIQUE (persona_id, user_id)
                            )
                        """))
                        # 索引用 IF NOT EXISTS，热重载也不会冲突
                        await conn.execute(text(
                            "CREATE INDEX IF NOT EXISTS ix_favour_records_persona_id ON favour_records (persona_id)"
                        ))
                        await conn.execute(text(
                            "CREATE INDEX IF NOT EXISTS ix_favour_records_user_id ON favour_records (user_id)"
                        ))
                    # 人设摘要表
                    await conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS persona_summaries (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            persona_id TEXT NOT NULL DEFAULT '',
                            summary TEXT NOT NULL DEFAULT '',
                            persona_hash TEXT NOT NULL DEFAULT '',
                            updated_at DATETIME,
                            CONSTRAINT uq_persona_summary_pid UNIQUE (persona_id)
                        )
                    """))
                    await conn.execute(text(
                        "CREATE INDEX IF NOT EXISTS ix_persona_summaries_pid ON persona_summaries (persona_id)"
                    ))

                    await conn.execute(text(
                        "CREATE TABLE IF NOT EXISTS emotion_favour_meta ("
                        "key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')"
                    ))

                await asyncio.to_thread(self._validate_schema)
                self._initialized = True
                logger.info(f"印象数据库已初始化: {self.db_path}")
            except Exception as e:
                self._initialized = False
                logger.error(f"数据库初始化失败: {e}")
                raise

    def _validate_schema(self) -> None:
        with sqlite3.connect(self.db_path) as connection:
            validate_favour_schema(connection)

    async def close(self) -> None:
        await self.engine.dispose()
        self._initialized = False

    def to_local_datetime(self, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        return (
            value.replace(tzinfo=timezone.utc)
            .astimezone(self.local_tz)
            .replace(tzinfo=None)
        )

    async def backup_data(self, records: List[FavourRecord], prefix: str) -> Optional[str]:
        if not records:
            return None
        async with self._backup_lock:
            temporary = None
            try:
                backup_dir = self.data_dir / "backups"
                backup_dir.mkdir(parents=True, exist_ok=True)
                safe_prefix = "".join(
                    char if char.isascii() and (char.isalnum() or char in "_-") else "_"
                    for char in str(prefix)
                ).strip("_") or "backup"
                timestamp = utc_now().strftime("%Y%m%d_%H%M%S_%f_UTC")
                filename = backup_dir / f"{safe_prefix}_{timestamp}.json"
                temporary = filename.with_suffix(".json.tmp")

                data_to_save = []
                for r in records:
                    d = r.model_dump() if hasattr(r, "model_dump") else r.dict()
                    d['created_at'] = d['created_at'].isoformat() if d.get('created_at') else None
                    d['updated_at'] = d['updated_at'].isoformat() if d.get('updated_at') else None
                    data_to_save.append(d)

                payload = {
                    "format": _BACKUP_FORMAT,
                    "timestamp_storage": _TIMESTAMP_STORAGE_UTC,
                    "records": data_to_save,
                }
                async with aio_open(temporary, "w", encoding="utf-8") as f:
                    await f.write(json.dumps(payload, ensure_ascii=False, indent=2))
                await asyncio.to_thread(temporary.replace, filename)
                protected = frozenset(self._protected_backup_files)
                removed = await asyncio.to_thread(
                    self._cleanup_expired_backups, protected
                )
                if removed:
                    logger.info(f"已清理 {removed} 个过期 JSON 备份")
                return str(filename)
            except Exception as e:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
                logger.error(f"备份数据失败: {e}")
                return None

    def _cleanup_expired_backups(
        self,
        protected_files: frozenset[str] = frozenset(),
    ) -> int:
        """删除超过保留期的有效 JSON 业务备份；未知或损坏文件保持不动。"""
        if self.backup_retention_days <= 0:
            return 0
        backup_dir = self.data_dir / "backups"
        if not backup_dir.exists():
            return 0
        now_timestamp = datetime.now(timezone.utc).timestamp()
        retention_seconds = self.backup_retention_days * 86400
        removed = 0
        try:
            paths = list(backup_dir.glob("*.json"))
        except OSError:
            return 0
        for path in paths:
            try:
                if path.name in protected_files:
                    continue
                age_seconds = max(0.0, now_timestamp - path.stat().st_mtime)
                if age_seconds <= retention_seconds:
                    continue
                self._load_backup_payload(path.name)
                path.unlink()
                removed += 1
            except (OSError, ValueError):
                continue
        return removed

    async def cleanup_expired_backups(self) -> int:
        async with self._backup_lock:
            return await asyncio.to_thread(
                self._cleanup_expired_backups,
                frozenset(self._protected_backup_files),
            )

    @asynccontextmanager
    async def protect_backup(self, filename: str):
        """在恢复流程期间阻止保留期清理或手动删除目标备份。"""
        name = str(filename or "").strip()
        if not name or Path(name).name != name or not name.endswith(".json"):
            raise ValueError("备份文件名无效")
        async with self._backup_lock:
            self._protected_backup_files[name] = (
                self._protected_backup_files.get(name, 0) + 1
            )
        try:
            yield
        finally:
            async with self._backup_lock:
                users = self._protected_backup_files.get(name, 0) - 1
                if users > 0:
                    self._protected_backup_files[name] = users
                else:
                    self._protected_backup_files.pop(name, None)

    def _resolve_backup_path(self, filename: str) -> Path:
        name = str(filename or "").strip()
        if not name or Path(name).name != name or not name.endswith(".json"):
            raise ValueError("备份文件名无效")
        backup_dir = (self.data_dir / "backups").resolve()
        path = (backup_dir / name).resolve()
        if path.parent != backup_dir:
            raise ValueError("备份文件路径无效")
        if not path.is_file():
            raise ValueError("备份文件不存在")
        if path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError("备份文件过大")
        return path

    def _load_backup_payload(self, filename: str) -> tuple[Path, list[dict], str]:
        path = self._resolve_backup_path(filename)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("备份文件损坏或无法读取") from exc

        if isinstance(payload, dict):
            if payload.get("format") != _BACKUP_FORMAT:
                raise ValueError("不支持的备份格式")
            timestamp_storage = str(payload.get("timestamp_storage", ""))
            if timestamp_storage != _TIMESTAMP_STORAGE_UTC:
                raise ValueError("不支持的备份时间格式")
            records_payload = payload.get("records")
        elif isinstance(payload, list):
            records_payload = payload
            timestamp_storage = (
                _TIMESTAMP_STORAGE_UTC
                if path.stem.endswith("_UTC")
                else _TIMESTAMP_STORAGE_LEGACY_LOCAL
            )
        else:
            raise ValueError("不支持的备份格式")
        if not isinstance(records_payload, list):
            raise ValueError("备份文件缺少记录列表")
        records = [item for item in records_payload if isinstance(item, dict)]
        if len(records) != len(records_payload):
            raise ValueError("备份文件包含无效记录")
        return path, records, timestamp_storage

    def _backup_kind(self, filename: str) -> str:
        if filename.startswith("pre_restore_"):
            return "恢复前自动备份"
        if filename.startswith("pre_edit_"):
            return "编辑前自动备份"
        if filename.startswith("manual_persona_"):
            return "手动备份"
        if filename.startswith("backup_user_"):
            return "单用户清空备份"
        if filename.startswith("backup_all_database_"):
            return "整个人格清空备份"
        return "数据备份"

    def _scan_backups(self, persona_id: str) -> list[dict]:
        backup_dir = self.data_dir / "backups"
        if not backup_dir.exists():
            return []
        items = []
        for path in backup_dir.glob("*.json"):
            try:
                _, payload, _ = self._load_backup_payload(path.name)
                matched = [
                    item for item in payload
                    if str(item.get("persona_id", "")) == persona_id
                ]
                if not matched:
                    continue
                modified = datetime.fromtimestamp(
                    path.stat().st_mtime, tz=timezone.utc
                ).astimezone(self.local_tz)
                preview = None
                if len(matched) == 1:
                    record = matched[0]
                    try:
                        favour = int(record.get("favour", 0))
                    except (TypeError, ValueError):
                        favour = 0
                    emotions = {}
                    for dim in EMOTION_DIMENSIONS:
                        try:
                            emotions[dim] = int(record.get(dim, 0))
                        except (TypeError, ValueError):
                            emotions[dim] = 0
                    preview = {
                        "user_id": str(record.get("user_id", "")),
                        "favour": favour,
                        "emotions": emotions,
                    }
                items.append({
                    "filename": path.name,
                    "kind": self._backup_kind(path.name),
                    "created_at": modified.strftime("%Y-%m-%d %H:%M:%S"),
                    "record_count": len(matched),
                    "size_bytes": path.stat().st_size,
                    "preview": preview,
                    "_modified": path.stat().st_mtime,
                })
            except (OSError, ValueError):
                continue
        items.sort(key=lambda item: item["_modified"], reverse=True)
        for item in items:
            item.pop("_modified", None)
        return items[:100]

    def _scan_backup_personas(self) -> list[str]:
        """返回有效 JSON 备份中出现过的人格，供清空后的恢复入口使用。"""
        backup_dir = self.data_dir / "backups"
        if not backup_dir.exists():
            return []
        personas: set[str] = set()
        for path in backup_dir.glob("*.json"):
            try:
                _, payload, _ = self._load_backup_payload(path.name)
            except (OSError, ValueError):
                continue
            for item in payload:
                raw_persona_id = str(item.get("persona_id", ""))
                persona_id = raw_persona_id.strip()
                if persona_id == raw_persona_id and 0 < len(persona_id) <= 64:
                    personas.add(persona_id)
        return sorted(personas)

    async def list_backups(self, persona_id: str) -> list[dict]:
        """列出包含指定人格记录的 JSON 备份，不向调用方暴露绝对路径。"""
        if not persona_id:
            return []
        async with self._backup_lock:
            return await asyncio.to_thread(self._scan_backups, persona_id)

    async def get_backup_personas(self) -> list[str]:
        """返回备份中仍可恢复的人格，即使其数据库记录已全部清空。"""
        async with self._backup_lock:
            return await asyncio.to_thread(self._scan_backup_personas)

    def _select_backup_records(
        self,
        payload: list[dict],
        persona_id: str,
    ) -> tuple[list[dict], tuple[str, ...]]:
        selected = [
            item for item in payload
            if str(item.get("persona_id", "")) == persona_id
        ]
        if not selected:
            raise ValueError("该备份不包含当前人格的数据")
        user_ids: list[str] = []
        seen_users: set[str] = set()
        for item in selected:
            user_id = str(item.get("user_id", "")).strip()
            if not _is_valid_userid(user_id):
                raise ValueError("备份中存在无效 user_id")
            if user_id in seen_users:
                raise ValueError("备份中存在重复用户记录")
            seen_users.add(user_id)
            user_ids.append(user_id)
        return selected, tuple(user_ids)

    async def get_backup_user_ids(
        self,
        filename: str,
        persona_id: str,
    ) -> tuple[str, ...]:
        """返回备份中指定人格会被合并恢复的用户 ID。"""
        if not persona_id or len(persona_id) > 64:
            raise ValueError("persona_id 无效")
        async with self._backup_lock:
            _, payload, _ = await asyncio.to_thread(
                self._load_backup_payload, filename
            )
        _, user_ids = self._select_backup_records(payload, persona_id)
        return user_ids

    async def restore_backup(self, filename: str, persona_id: str) -> int:
        """把备份中指定人格的记录合并恢复，已存在记录按备份值覆盖。"""
        await self.init_db()
        if not persona_id or len(persona_id) > 64:
            raise ValueError("persona_id 无效")
        async with self._backup_lock:
            _, payload, timestamp_storage = await asyncio.to_thread(
                self._load_backup_payload, filename
            )
        selected, _ = self._select_backup_records(payload, persona_id)

        now = utc_now()
        clean_rows: list[dict] = []
        for item in selected:
            user_id = str(item.get("user_id", "")).strip()
            try:
                favour = max(self.min_val, min(self.max_val, int(item.get("favour", 0))))
            except (TypeError, ValueError) as exc:
                raise ValueError("备份中存在无效好感度") from exc
            row = {
                "persona_id": persona_id,
                "user_id": user_id,
                "favour": favour,
                "created_at": _parse_db_datetime(
                    item.get("created_at"),
                    naive_timezone=(
                        self.local_tz
                        if timestamp_storage == _TIMESTAMP_STORAGE_LEGACY_LOCAL
                        else None
                    ),
                ) or now,
                "updated_at": _parse_db_datetime(
                    item.get("updated_at"),
                    naive_timezone=(
                        self.local_tz
                        if timestamp_storage == _TIMESTAMP_STORAGE_LEGACY_LOCAL
                        else None
                    ),
                ) or now,
            }
            for dim in EMOTION_DIMENSIONS:
                try:
                    row[dim] = max(0, min(100, int(item.get(dim, 0))))
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"备份中存在无效情感维度: {dim}") from exc
            clean_rows.append(row)

        async with self.async_session() as session:
            try:
                for row in clean_rows:
                    stmt = sqlite_insert(FavourRecord).values(**row)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["persona_id", "user_id"],
                        set_={
                            key: value for key, value in row.items()
                            if key not in {"persona_id", "user_id"}
                        },
                    )
                    await session.execute(stmt)
                await session.commit()
            except Exception:
                await session.rollback()
                raise
        return len(clean_rows)

    async def delete_backup(self, filename: str, persona_id: str) -> None:
        """删除当前人格专属 JSON 备份；混合人格备份拒绝从 UI 删除。"""
        if not persona_id:
            raise ValueError("persona_id 无效")
        name = str(filename or "").strip()
        async with self._backup_lock:
            if self._protected_backup_files.get(name, 0) > 0:
                raise RuntimeError("该备份正在恢复，暂时不能删除")
            path, payload, _ = await asyncio.to_thread(
                self._load_backup_payload, name
            )
            personas = {
                str(item.get("persona_id", ""))
                for item in payload
            }
            if persona_id not in personas:
                raise ValueError("该备份不属于当前人格")
            if personas != {persona_id}:
                raise ValueError("混合人格备份不能在管理台删除")
            try:
                await asyncio.to_thread(path.unlink)
            except OSError as exc:
                raise RuntimeError("删除备份文件失败") from exc

    async def get_favour(self, persona_id: str, user_id: str) -> Optional[FavourRecord]:
        await self.init_db()
        async with self.async_session() as session:
            stmt = select(FavourRecord).where(
                FavourRecord.persona_id == persona_id,
                FavourRecord.user_id == user_id,
            )
            result = await session.execute(stmt)
            return result.scalars().first()

    async def get_existing_records(
        self,
        persona_id: str,
        user_ids: tuple[str, ...],
    ) -> List[FavourRecord]:
        """返回指定用户中当前已存在的记录，供局部恢复前保护。"""
        await self.init_db()
        if not user_ids:
            return []
        records: list[FavourRecord] = []
        async with self.async_session() as session:
            for start in range(0, len(user_ids), 500):
                chunk = user_ids[start:start + 500]
                stmt = select(FavourRecord).where(
                    FavourRecord.persona_id == persona_id,
                    FavourRecord.user_id.in_(chunk),
                ).order_by(FavourRecord.id.asc())
                result = await session.execute(stmt)
                records.extend(result.scalars().all())
        return records

    async def update_favour(self, persona_id: str, user_id: str, favour: Optional[int] = None, emotion_updates: Optional[dict] = None) -> bool:
        """原子 UPSERT：favour 为绝对值，emotion_updates 为增量。"""
        await self.init_db()
        if not _is_valid_userid(user_id):
            return False

        try:
            async with self.async_session() as session:
                now = utc_now()
                clean_deltas: dict[str, int] = {}
                for dim, value in (emotion_updates or {}).items():
                    if dim not in EMOTION_DIMENSIONS:
                        continue
                    try:
                        clean_deltas[dim] = int(value)
                    except (TypeError, ValueError):
                        continue

                insert_values = {
                    "persona_id": persona_id,
                    "user_id": user_id,
                    "favour": max(self.min_val, min(self.max_val, favour)) if favour is not None else 0,
                    "created_at": now,
                    "updated_at": now,
                    **{dim: max(0, min(100, delta)) for dim, delta in clean_deltas.items()},
                }
                stmt = sqlite_insert(FavourRecord).values(**insert_values)
                updates = {"updated_at": now}
                if favour is not None:
                    updates["favour"] = max(self.min_val, min(self.max_val, favour))
                for dim, delta in clean_deltas.items():
                    column = getattr(FavourRecord, dim)
                    updates[dim] = func.max(0, func.min(100, column + delta))
                stmt = stmt.on_conflict_do_update(
                    index_elements=["persona_id", "user_id"],
                    set_=updates,
                )
                await session.execute(stmt)
                await session.commit()
                return True
        except Exception as e:
            logger.error(f"更新数据库失败: {str(e)}")
            return False

    async def set_record_fields(
        self,
        persona_id: str,
        user_id: str,
        favour: Optional[int] = None,
        emotions_absolute: Optional[dict] = None,
    ) -> bool:
        """绝对值写入（WebUI 管理员手动覆盖场景）。

        与 update_favour 的差异：
        - ``emotions_absolute`` 是绝对值而非 delta，直接覆盖当前值
        - 不施加 ``diminish_delta`` 收益递减（admin 覆盖不应被软化）
        - favour 走 min_val/max_val clamp；emotions 每维走 0-100 clamp
        - record 不存在时按 favour 创建（emotions 部分写入）
        - 始终刷新 updated_at
        """
        await self.init_db()
        if not _is_valid_userid(user_id):
            return False
        try:
            async with self.async_session() as session:
                now = utc_now()
                clean_emotions: dict[str, int] = {}
                for dim, value in (emotions_absolute or {}).items():
                    if dim not in EMOTION_DIMENSIONS:
                        continue
                    try:
                        clean_emotions[dim] = max(0, min(100, int(value)))
                    except (ValueError, TypeError):
                        continue
                insert_values = {
                    "persona_id": persona_id,
                    "user_id": user_id,
                    "favour": max(self.min_val, min(self.max_val, favour)) if favour is not None else 0,
                    "created_at": now,
                    "updated_at": now,
                    **clean_emotions,
                }
                stmt = sqlite_insert(FavourRecord).values(**insert_values)
                updates = {"updated_at": now, **clean_emotions}
                if favour is not None:
                    updates["favour"] = max(self.min_val, min(self.max_val, favour))
                stmt = stmt.on_conflict_do_update(
                    index_elements=["persona_id", "user_id"],
                    set_=updates,
                )
                await session.execute(stmt)
                await session.commit()
                return True
        except Exception as e:
            logger.error(f"绝对值写入失败: {str(e)}")
            return False

    async def create_record(
        self,
        persona_id: str,
        user_id: str,
        *,
        favour: int = 0,
        emotions_absolute: Optional[dict] = None,
    ) -> bool:
        """原子创建记录；已存在时返回 False。"""
        await self.init_db()
        if not _is_valid_userid(user_id):
            return False
        now = utc_now()
        values = {
            "persona_id": persona_id,
            "user_id": user_id,
            "favour": max(self.min_val, min(self.max_val, int(favour))),
            "created_at": now,
            "updated_at": now,
        }
        for dim, value in (emotions_absolute or {}).items():
            if dim in EMOTION_DIMENSIONS:
                values[dim] = max(0, min(100, int(value)))
        try:
            async with self.async_session() as session:
                stmt = sqlite_insert(FavourRecord).values(**values).on_conflict_do_nothing(
                    index_elements=["persona_id", "user_id"],
                )
                result = await session.execute(stmt)
                await session.commit()
                return bool(result.rowcount)
        except Exception as e:
            logger.error(f"创建记录失败: {e}")
            return False

    async def get_distinct_personas(self) -> list[str]:
        """返回 DB 中存在记录的所有 persona_id（去重、按字母序）。供 WebUI 切换器使用。"""
        await self.init_db()
        try:
            async with self.async_session() as session:
                stmt = select(FavourRecord.persona_id).distinct()
                result = await session.execute(stmt)
                return sorted({row[0] for row in result.fetchall() if row[0]})
        except Exception as e:
            logger.error(f"查询 persona 列表失败: {str(e)}")
            return []

    async def delete_favour(self, persona_id: str, user_id: str) -> Tuple[bool, str]:
        await self.init_db()
        try:
            async with self.async_session() as session:
                stmt = select(FavourRecord).where(
                    FavourRecord.persona_id == persona_id,
                    FavourRecord.user_id == user_id,
                )
                result = await session.execute(stmt)
                record = result.scalars().first()

                if not record:
                    return False, "未找到记录"

                await session.delete(record)
                await session.commit()
                return True, "删除成功"
        except Exception as e:
            logger.error(f"删除记录失败: {str(e)}")
            return False, f"数据库错误: {str(e)}"

    async def get_global_records(self, persona_id: str) -> List[FavourRecord]:
        await self.init_db()
        async with self.async_session() as session:
            stmt = select(FavourRecord).where(FavourRecord.persona_id == persona_id)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def count_records(self, persona_id: str, *, user_id_search: str = "") -> int:
        await self.init_db()
        async with self.async_session() as session:
            conditions = [FavourRecord.persona_id == persona_id]
            if user_id_search:
                conditions.append(FavourRecord.user_id.contains(user_id_search))
            stmt = select(func.count(FavourRecord.id)).where(*conditions)
            result = await session.execute(stmt)
            return int(result.scalar_one() or 0)

    async def get_records_for_effective_sort(
        self,
        persona_id: str,
        *,
        user_id_search: str = "",
    ) -> List[FavourRecord]:
        """返回用于 transient 好感排序的候选记录，由调用方计算衰减后再分页。"""
        await self.init_db()
        conditions = [FavourRecord.persona_id == persona_id]
        if user_id_search:
            conditions.append(FavourRecord.user_id.contains(user_id_search))
        async with self.async_session() as session:
            stmt = select(FavourRecord).where(*conditions).order_by(FavourRecord.id.asc())
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def list_records(
        self,
        persona_id: str,
        *,
        offset: int = 0,
        limit: int = 50,
        sort_by: str = "favour",
        sort_order: str = "desc",
        user_id_search: str = "",
    ) -> List[FavourRecord]:
        """数据库侧分页；favour 排序基于存储值，展示时仍计算 transient 衰减值。"""
        await self.init_db()
        columns = {
            "user_id": FavourRecord.user_id,
            "favour": FavourRecord.favour,
            "updated_at": FavourRecord.updated_at,
            "created_at": FavourRecord.created_at,
        }
        column = columns.get(sort_by, FavourRecord.favour)
        ordering = column.asc() if sort_order == "asc" else column.desc()
        async with self.async_session() as session:
            conditions = [FavourRecord.persona_id == persona_id]
            if user_id_search:
                conditions.append(FavourRecord.user_id.contains(user_id_search))
            stmt = (
                select(FavourRecord)
                .where(*conditions)
                .order_by(ordering, FavourRecord.id.asc())
                .offset(max(0, int(offset)))
                .limit(max(1, min(200, int(limit))))
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def reset_all_emotions_by_persona(self, persona_id: str) -> int:
        """重置指定人格下所有用户的情感维度归零（好感度保留）。返回受影响行数。"""
        await self.init_db()
        try:
            async with self.async_session() as session:
                values = {dim: 0 for dim in EMOTION_DIMENSIONS}
                values["updated_at"] = utc_now()
                stmt = (
                    update(FavourRecord)
                    .where(FavourRecord.persona_id == persona_id)
                    .values(**values)
                )
                result = await session.execute(stmt)
                await session.commit()
                return result.rowcount or 0
        except Exception as e:
            logger.error(f"批量重置情感维度失败: {str(e)}")
            return 0

    async def clear_persona(self, persona_id: str) -> bool:
        """清空指定人格下的所有好感度记录"""
        await self.init_db()
        try:
            async with self.async_session() as session:
                stmt = delete(FavourRecord).where(FavourRecord.persona_id == persona_id)
                await session.execute(stmt)
                await session.commit()
                return True
        except Exception as e:
            logger.error(f"清空人格记录失败: {str(e)}")
            return False

    # ================= 人设摘要 =================

    async def get_persona_summary(self, persona_id: str) -> Optional[dict]:
        """获取人设摘要，返回 {summary, hash} 或 None。"""
        await self.init_db()
        async with self.async_session() as session:
            stmt = select(PersonaSummaryRecord).where(
                PersonaSummaryRecord.persona_id == persona_id
            )
            result = await session.execute(stmt)
            record = result.scalars().first()
            if record:
                return {"summary": record.summary, "hash": record.persona_hash}
            return None

    async def save_persona_summary(
        self, persona_id: str, summary: str, persona_hash: str
    ) -> bool:
        """保存或更新人设摘要。"""
        await self.init_db()
        try:
            async with self.async_session() as session:
                now = utc_now()
                stmt = sqlite_insert(PersonaSummaryRecord).values(
                    persona_id=persona_id,
                    summary=summary,
                    persona_hash=persona_hash,
                    updated_at=now,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["persona_id"],
                    set_={
                        "summary": stmt.excluded.summary,
                        "persona_hash": stmt.excluded.persona_hash,
                        "updated_at": now,
                    },
                )
                await session.execute(stmt)
                await session.commit()
                return True
        except Exception as e:
            logger.error(f"保存人设摘要失败: {e}")
            return False

    async def delete_persona_summary(self, persona_id: str) -> bool:
        """删除人设摘要。"""
        await self.init_db()
        try:
            async with self.async_session() as session:
                await session.execute(
                    delete(PersonaSummaryRecord).where(
                        PersonaSummaryRecord.persona_id == persona_id
                    )
                )
                await session.commit()
                return True
        except Exception as e:
            logger.error(f"删除人设摘要失败: {e}")
            return False
