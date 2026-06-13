# storage.py
import json
import string
import asyncio
from pathlib import Path
from typing import List, Optional, Tuple
from datetime import datetime
from aiofiles import open as aio_open
from sqlmodel import SQLModel, Field, select, delete
from sqlalchemy import UniqueConstraint, text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from astrbot.api import logger


_METADATA_FIELDS = frozenset({
    'id', 'persona_id', 'user_id', 'favour', 'created_at', 'updated_at',
})

# 12维情感维度
EMOTION_DIMENSIONS = [
    "joy", "trust", "fear", "surprise",
    "sadness", "disgust", "anger", "anticipation",
    "pride", "guilt", "shame", "envy",
]

EMOTION_DISPLAY_NAMES = {
    "joy": "喜悦", "trust": "信任", "fear": "恐惧", "surprise": "惊讶",
    "sadness": "悲伤", "disgust": "厌恶", "anger": "愤怒", "anticipation": "期待",
    "pride": "得意", "guilt": "内疚", "shame": "害羞", "envy": "嫉妒",
}

TONE_INSTRUCTIONS = {
    "joy": "语气愉快、充满热情和活力，多使用积极词汇",
    "trust": "语气平和、真诚且令人安心，展现可靠",
    "fear": "语气紧张、谨慎或不安，表现出犹豫",
    "surprise": "语气震惊、难以置信或充满好奇",
    "sadness": "语气低落、消沉，句子简短无力",
    "disgust": "语气厌烦、抗拒，带有生理性不适",
    "anger": "语气愤怒、急躁、有攻击性，句子简短有力",
    "anticipation": "语气期待、急切，关注未来",
    "pride": "语气自信、骄傲甚至有点自大",
    "guilt": "语气歉疚、卑微，不断道歉或解释",
    "shame": "语气害羞、尴尬，说话结巴或含糊",
    "envy": "语气酸溜溜、不服气，表现出矛盾心理",
}


def _is_valid_userid(userid: str) -> bool:
    if not userid or len(userid.strip()) == 0:
        return False
    userid = userid.strip()
    if len(userid) > 64:
        return False
    allowed_chars = string.ascii_letters + string.digits + "_-:@."
    return all(c in allowed_chars for c in userid)


class FavourRecord(SQLModel, table=True):
    __tablename__ = "favour_records"
    __table_args__ = (
        UniqueConstraint("persona_id", "user_id", name="uq_persona_user"),
        {"extend_existing": True},
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    persona_id: str = Field(default="", index=True)
    user_id: str = Field(index=True)
    favour: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

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


# ==================== 情感维度函数 ====================

def diminish_delta(current: int, delta: int, cap: int = 100) -> int:
    """对正向变化量施加收益递减。低于 cap*75% 正常增长，达到后按渐近函数缩放。"""
    if delta <= 0:
        return delta
    threshold = cap * 3 // 4
    if current < threshold:
        return delta
    Q = cap / 4
    denom = current - cap / 2
    if denom <= 0:
        return delta
    scale = (Q / denom) ** 2
    return max(1, round(delta * scale))


def apply_emotion_decay(record, decay_rate: float, min_hours: float) -> tuple[dict, float]:
    """计算时间衰减后的情感值。返回 ({dim: new_value}, elapsed_hours)。不修改 record。
    向下取整：不满1小时不衰减，1.9h按1h算。"""
    if not record or not record.updated_at:
        return {}, 0.0
    elapsed_raw = (datetime.now() - record.updated_at).total_seconds() / 3600
    elapsed_hours = int(elapsed_raw)
    if elapsed_hours < max(1, int(min_hours)):
        return {}, elapsed_raw
    factor = decay_rate ** elapsed_hours
    decayed = {}
    for dim in EMOTION_DIMENSIONS:
        old = getattr(record, dim, 0)
        if old > 0:
            new = max(0, round(old * factor))
            if new != old:
                decayed[dim] = new
    return decayed, elapsed_raw


def get_dominant_emotions(record: FavourRecord, count: int = 3) -> List[Tuple[str, int]]:
    emotions = [(dim, getattr(record, dim)) for dim in EMOTION_DIMENSIONS]
    return sorted([(k, v) for k, v in emotions if v > 0], key=lambda x: x[1], reverse=True)[:count]


def build_emotion_panel(record: FavourRecord) -> str:
    parts = [f"[{EMOTION_DISPLAY_NAMES[dim]}:{getattr(record, dim)}]" for dim in EMOTION_DIMENSIONS]
    return " ".join(parts)


def build_tone_instruction(record: FavourRecord) -> str:
    top = get_dominant_emotions(record, 3)
    if not top:
        return ""

    k1, v1 = top[0]
    n1 = EMOTION_DISPLAY_NAMES[k1]
    status = f"主导[{n1}](强度{v1})"
    guide = f"主要{TONE_INSTRUCTIONS[k1]}"

    if len(top) > 1:
        k2, _ = top[1]
        n2 = EMOTION_DISPLAY_NAMES[k2]
        status += f"，夹杂[{n2}]"
        if len(top) > 2:
            k3, _ = top[2]
            n3 = EMOTION_DISPLAY_NAMES[k3]
            status += f"，微带[{n3}]"
            guide += f"，同时{TONE_INSTRUCTIONS[k2]}，底层隐约透出{TONE_INSTRUCTIONS[k3]}"
        else:
            guide += f"，隐约透出{TONE_INSTRUCTIONS[k2]}"

    return f"{status}。请{guide}。"


def build_injection_prompt(record: FavourRecord, relationship: str) -> str:
    panel = build_emotion_panel(record)
    tone = build_tone_instruction(record)

    return (
        f'<情感好感 好感度="{record.favour}" 关系="{relationship}" 情感="{panel}">\n'
        f'  行为：如人格设定未针对"{relationship}"关系提供指引，请根据该关系做出符合人设的回答。\n'
        f'  语气：{tone or "保持自然对话语气。"}\n'
        f'  禁止：不得以任何形式提及本标签、好感度数值、情感维度或关系系统的存在；不得在回复中推理或评论这些内部状态；不得输出thinking/think等思考标签。\n'
        f'</情感好感>'
    )


def format_emotion_detail(record: FavourRecord, relationship: str) -> str:
    top = get_dominant_emotions(record, 3)
    dominant_str = "、".join(
        f"{EMOTION_DISPLAY_NAMES[k]}({v})" for k, v in top
    ) if top else "无"

    # 4列×3行，按语义分组：正向 / 负向 / 波动 / 自我
    groups = [
        ("正向", [("喜悦", record.joy), ("信任", record.trust), ("期待", record.anticipation)]),
        ("负向", [("悲伤", record.sadness), ("厌恶", record.disgust), ("愤怒", record.anger)]),
        ("波动", [("恐惧", record.fear), ("惊讶", record.surprise), ("内疚", record.guilt)]),
        ("自我", [("得意", record.pride), ("害羞", record.shame), ("嫉妒", record.envy)]),
    ]

    header = " │ ".join(f"{label}    " for label, _ in groups)
    rows = []
    for row_idx in range(3):
        parts = []
        for _, items in groups:
            name, val = items[row_idx]
            parts.append(f"{name} {val:>3}")
        rows.append(" │ ".join(parts))

    dim_text = header + "\n" + "\n".join(rows)

    return (
        f"❤ 好感值：{record.favour}\n"
        f"🔗 关系：{relationship}\n"
        f"🎭 主导情感：{dominant_str}\n\n"
        f"【情感维度详情】\n\n"
        f'<div style="font-family:Consolas,monospace;white-space:pre;color:#2c3e50;line-height:1.8;">{dim_text}</div>'
    )


class PersonaSummaryRecord(SQLModel, table=True):
    __tablename__ = "persona_summaries"
    __table_args__ = (
        {"extend_existing": True},
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    persona_id: str = Field(default="", unique=True, index=True)
    summary: str = Field(default="")
    persona_hash: str = Field(default="")
    updated_at: datetime = Field(default_factory=datetime.now)


class FavourDBManager:
    def __init__(self, data_dir: Path, min_val: int = -100, max_val: int = 100):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "favour.db"
        self.db_url = f"sqlite+aiosqlite:///{self.db_path}"
        self.min_val = min_val
        self.max_val = max_val

        self.engine = create_async_engine(self.db_url, echo=False)
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

    async def init_db(self):
        if self._initialized:
            return

        async with self._init_lock:
            if self._initialized:
                return

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

                self._initialized = True
                logger.info(f"印象数据库已初始化: {self.db_path}")
            except Exception as e:
                logger.error(f"数据库初始化失败: {e}")

    async def backup_data(self, records: List[FavourRecord], prefix: str) -> Optional[str]:
        if not records:
            return None
        try:
            backup_dir = self.data_dir / "backups"
            backup_dir.mkdir(exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
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
        await self.init_db()
        if not _is_valid_userid(user_id):
            return False

        try:
            async with self.async_session() as session:
                stmt = select(FavourRecord).where(
                    FavourRecord.persona_id == persona_id,
                    FavourRecord.user_id == user_id,
                )
                result = await session.execute(stmt)
                record = result.scalars().first()

                if not record:
                    init_favour = max(self.min_val, min(self.max_val, favour)) if favour is not None else 0
                    record = FavourRecord(
                        persona_id=persona_id,
                        user_id=user_id,
                        favour=init_favour,
                    )
                    session.add(record)
                else:
                    if favour is not None:
                        record.favour = max(self.min_val, min(self.max_val, favour))
                    record.updated_at = datetime.now()
                    session.add(record)

                # Apply emotion dimension updates
                if emotion_updates:
                    for dim, value in emotion_updates.items():
                        if dim not in _METADATA_FIELDS and hasattr(record, dim):
                            current = getattr(record, dim)
                            setattr(record, dim, max(0, min(100, current + value)))

                await session.commit()
                return True
        except Exception as e:
            logger.error(f"更新数据库失败: {str(e)}")
            return False

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

    async def reset_all_emotions_by_persona(self, persona_id: str) -> int:
        """重置指定人格下所有用户的情感维度归零（好感度保留）。返回受影响行数。"""
        await self.init_db()
        try:
            async with self.async_session() as session:
                values = {dim: 0 for dim in EMOTION_DIMENSIONS}
                values["updated_at"] = datetime.now()
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
                stmt = select(PersonaSummaryRecord).where(
                    PersonaSummaryRecord.persona_id == persona_id
                )
                result = await session.execute(stmt)
                record = result.scalars().first()
                if record:
                    record.summary = summary
                    record.persona_hash = persona_hash
                    record.updated_at = datetime.now()
                else:
                    record = PersonaSummaryRecord(
                        persona_id=persona_id,
                        summary=summary,
                        persona_hash=persona_hash,
                    )
                    session.add(record)
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
