# storage.py
import json
import string
import asyncio
import sqlite3
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


_METADATA_FIELDS = frozenset({
    'id', 'persona_id', 'user_id', 'favour', 'created_at', 'updated_at',
})


def _parse_db_datetime(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        try:
            return datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

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
    ):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "favour.db"
        self.db_url = f"sqlite+aiosqlite:///{self.db_path}"
        self.min_val = min_val
        self.max_val = max_val
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

    # 旧表升级：补齐缺失的情感列
    _EMOTION_COLUMNS = [
        ("joy", "INTEGER DEFAULT 0"),
        ("trust", "INTEGER DEFAULT 0"),
        ("fear", "INTEGER DEFAULT 0"),
        ("surprise", "INTEGER DEFAULT 0"),
        ("sadness", "INTEGER DEFAULT 0"),
        ("disgust", "INTEGER DEFAULT 0"),
        ("anger", "INTEGER DEFAULT 0"),
        ("anticipation", "INTEGER DEFAULT 0"),
        ("pride", "INTEGER DEFAULT 0"),
        ("guilt", "INTEGER DEFAULT 0"),
        ("shame", "INTEGER DEFAULT 0"),
        ("envy", "INTEGER DEFAULT 0"),
    ]

    async def _backup_sqlite(self, prefix: str) -> Optional[Path]:
        """使用 SQLite backup API 创建一致性数据库备份。"""
        if not self.db_path.exists() or self.db_path.stat().st_size == 0:
            return None
        backup_dir = self.data_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = utc_now().strftime("%Y%m%d_%H%M%S_UTC")
        target = backup_dir / f"{prefix}_{stamp}.db"

        def _copy() -> None:
            source_conn = sqlite3.connect(self.db_path)
            target_conn = sqlite3.connect(target)
            try:
                source_conn.backup(target_conn)
            finally:
                target_conn.close()
                source_conn.close()

        await asyncio.to_thread(_copy)
        return target

    async def _migrate_timestamps_to_utc(self, conn) -> int:
        """把旧版按服务器本地时间写入的 naive 时间转换成 UTC naive。"""
        converted = 0
        table_columns = {
            "favour_records": ("created_at", "updated_at"),
            "persona_summaries": ("updated_at",),
        }
        for table_name, columns in table_columns.items():
            exists = await conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"),
                {"name": table_name},
            )
            if exists.fetchone() is None:
                continue
            selected = ", ".join(("id", *columns))
            rows = (await conn.execute(text(f"SELECT {selected} FROM {table_name}"))).fetchall()
            for row in rows:
                updates = {}
                for index, column in enumerate(columns, start=1):
                    old = _parse_db_datetime(row[index])
                    if old is None:
                        continue
                    utc_value = (
                        old.replace(tzinfo=self.local_tz)
                        .astimezone(timezone.utc)
                        .replace(tzinfo=None)
                    )
                    updates[column] = utc_value
                if not updates:
                    continue
                assignments = ", ".join(f"{column}=:{column}" for column in updates)
                updates["row_id"] = row[0]
                await conn.execute(
                    text(f"UPDATE {table_name} SET {assignments} WHERE id=:row_id"),
                    updates,
                )
                converted += 1
        return converted

    async def init_db(self):
        if self._initialized:
            return

        async with self._init_lock:
            if self._initialized:
                return

            migration_marker = self.data_dir / ".timestamps_utc_v1"
            preexisting = self.db_path.exists() and self.db_path.stat().st_size > 0
            if preexisting and not migration_marker.exists():
                backup_path = await self._backup_sqlite("pre_utc_migration")
                if backup_path:
                    logger.info(f"UTC 时间迁移前数据库备份完成: {backup_path}")

            try:
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
                    else:
                        # 旧表升级：获取已有列，补齐缺失的情感列
                        col_result = await conn.execute(
                            text("PRAGMA table_info(favour_records)")
                        )
                        existing_cols = {row[1] for row in col_result.fetchall()}

                        for col_name, col_type in self._EMOTION_COLUMNS:
                            if col_name not in existing_cols:
                                await conn.execute(
                                    text(f"ALTER TABLE favour_records ADD COLUMN {col_name} {col_type}")
                                )
                                logger.info(f"数据库迁移：已添加列 {col_name}")

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

                    await conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS emotion_favour_meta (
                            key TEXT PRIMARY KEY,
                            value TEXT NOT NULL DEFAULT ''
                        )
                    """))
                    storage_row = await conn.execute(text(
                        "SELECT value FROM emotion_favour_meta WHERE key='timestamp_storage'"
                    ))
                    timestamp_storage = storage_row.scalar_one_or_none()
                    if table_exists and timestamp_storage != "utc_naive_v1":
                        converted = await self._migrate_timestamps_to_utc(conn)
                        logger.info(
                            f"数据库时间迁移完成: local naive -> UTC naive, rows={converted}"
                        )
                    await conn.execute(text("""
                        INSERT INTO emotion_favour_meta(key, value)
                        VALUES ('timestamp_storage', 'utc_naive_v1')
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """))

                self._initialized = True
                migration_marker.write_text("utc_naive_v1\n", encoding="utf-8")
                logger.info(f"印象数据库已初始化: {self.db_path}")
            except Exception as e:
                logger.error(f"数据库初始化失败: {e}")
                raise

    # ============ 昵称缓存 ============

    def _nickname_cache_path(self) -> Path:
        return self.data_dir / "nicknames.json"

    def _read_nickname_cache(self) -> dict:
        try:
            if self._nickname_cache_path().exists():
                return json.loads(self._nickname_cache_path().read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def get_cached_nicknames(self, user_ids: list[str]) -> dict[str, str]:
        """批量读取昵称缓存，返回 {user_id: nickname}。"""
        cache = self._read_nickname_cache()
        return {uid: cache.get(uid, "") for uid in user_ids if cache.get(uid)}

    def cache_nickname(self, user_id: str, nickname: str) -> None:
        """保存单个用户昵称到缓存。"""
        if not user_id or not nickname or nickname == user_id:
            return
        cache = self._read_nickname_cache()
        cache[user_id] = nickname
        try:
            self._nickname_cache_path().write_text(
                json.dumps(cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

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
        try:
            backup_dir = self.data_dir / "backups"
            backup_dir.mkdir(exist_ok=True)
            timestamp = utc_now().strftime("%Y%m%d_%H%M%S_UTC")
            filename = backup_dir / f"{prefix}_{timestamp}.json"

            data_to_save = []
            for r in records:
                d = r.dict()
                d['created_at'] = d['created_at'].isoformat() if d.get('created_at') else None
                d['updated_at'] = d['updated_at'].isoformat() if d.get('updated_at') else None
                data_to_save.append(d)

            async with aio_open(filename, "w", encoding="utf-8") as f:
                await f.write(json.dumps(data_to_save, ensure_ascii=False, indent=2))
            return str(filename)
        except Exception as e:
            logger.error(f"备份数据失败: {e}")
            return None

    async def get_favour(self, persona_id: str, user_id: str) -> Optional[FavourRecord]:
        await self.init_db()
        async with self.async_session() as session:
            stmt = select(FavourRecord).where(
                FavourRecord.persona_id == persona_id,
                FavourRecord.user_id == user_id,
            )
            result = await session.execute(stmt)
            return result.scalars().first()

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
