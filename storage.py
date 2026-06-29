# storage.py
import json
import math
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

from .log import logger


_METADATA_FIELDS = frozenset({
    'id', 'persona_id', 'user_id', 'favour', 'created_at', 'updated_at',
})

# 12维情感维度
EMOTION_DIMENSIONS = [
    "joy", "trust", "fear", "surprise",
    "sadness", "disgust", "anger", "anticipation",
    "pride", "guilt", "shame", "envy",
]

# 按心理学持续性分组：衰减时同组用同一系数
# - volatile: 短期反应（几分钟到几小时散）
# - standard: 中等持续情绪
# - sticky:   长期心境（慢沉淀）
EMOTION_GROUPS = {
    "volatile": ["surprise", "anticipation"],
    "standard": ["joy", "anger", "disgust", "fear"],
    "sticky":   ["trust", "sadness", "guilt", "shame", "pride", "envy"],
}

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


def apply_emotion_decay(
    record,
    decay_rate_volatile: float,
    decay_rate_standard: float,
    decay_rate_sticky: float,
    min_hours: float,
) -> tuple[dict, float]:
    """计算时间衰减后的情感值。返回 ({dim: new_value}, elapsed_hours)。不修改 record。
    向下取整：不满1小时不衰减，1.9h按1h算。

    各维度按所属分组使用不同系数（参照 EMOTION_GROUPS）：
    - volatile（surprise/anticipation）: 短期反应，快速消散
    - standard（joy/anger/disgust/fear）: 中等持续
    - sticky（trust/sadness/guilt/shame/pride/envy）: 长期心境，慢沉淀
    """
    if not record or not record.updated_at:
        return {}, 0.0
    elapsed_raw = (datetime.now() - record.updated_at).total_seconds() / 3600
    elapsed_hours = int(elapsed_raw)
    if elapsed_hours < max(1, int(min_hours)):
        return {}, elapsed_raw

    decayed = {}
    for dim in EMOTION_DIMENSIONS:
        old = getattr(record, dim, 0)
        if old <= 0:
            continue
        if dim in EMOTION_GROUPS["volatile"]:
            rate = decay_rate_volatile
        elif dim in EMOTION_GROUPS["sticky"]:
            rate = decay_rate_sticky
        else:
            rate = decay_rate_standard
        factor = rate ** elapsed_hours
        new = max(0, round(old * factor))
        if new != old:
            decayed[dim] = new
    return decayed, elapsed_raw


TICK_SECONDS = 180  # 1 tick = 3 min


def compute_favour_decay(
    favour_now: int, updated_at: datetime, *,
    anchor: int, gamma0: float = 0.005, scale: float = 50,
    now: Optional[datetime] = None,
) -> int:
    """lazy 衰减（读时计算，不落盘）：根据 Δt 反向积分非线性 ODE 的封闭解。
    只对 favour_now > anchor 的部分衰减（σ=+1）；≤ anchor 原值返回。
    γ(x) = γ₀·(1 + u/S)，u = favour - anchor。"""
    if favour_now <= anchor:
        return favour_now
    if not updated_at:
        return favour_now
    now = now or datetime.now()
    elapsed_ticks = int((now - updated_at).total_seconds() // TICK_SECONDS)
    if elapsed_ticks <= 0:
        return favour_now  # 时钟回拨或不足 1 tick
    v0 = favour_now - anchor
    a = v0 / (scale + v0)
    K = a * math.exp(-gamma0 * elapsed_ticks)
    v1 = K * scale / (1 - K)
    return round(anchor + v1)


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


def compute_relationship_progress(favour: int, x: int, y: int) -> Tuple[int, str]:
    """计算 favour 在区间 [x, y] 内的进度百分比（0-100）与语义描述。

    用于对话注入：让 AI 感知用户在当前关系阶段内的积累程度，对冲好感度衰减
    带来的挫败感（提供正向进度反馈）。

    语义统一为「向下一阶段过渡的进度」，正负向区间均适用：
    - 「喜欢」[50, 89] 进度 90% = 即将触及「亲密」
    - 「厌恶」[-90, -51] 进度 90% = 即将松动到「反感」
    """
    if y <= x:
        return 100, "处于此关系巅峰"
    progress = (favour - x) / (y - x)
    progress = max(0.0, min(1.0, progress))
    pct = round(progress * 100)
    if pct < 20:
        sem = "刚进入此关系阶段"
    elif pct < 80:
        sem = "稳定在此关系阶段"
    elif pct < 95:
        sem = "接近下一阶段"
    else:
        sem = "处于此关系巅峰"
    return pct, sem


def build_injection_prompt(
    record: FavourRecord,
    relationship: str,
    favour_range: Optional[Tuple[int, int]] = None,
    effective_favour: Optional[int] = None,
    tier_extras: Optional[dict] = None,
) -> str:
    """构造 LLM 对话注入 prompt（XML 子项展开形式）。

    - record: 用于读取 12 维情感面板与主导情感
    - relationship: 关系名称（main.py 已算好，可能含 admin override）
    - favour_range: 当前关系区间 (x, y)，用于进度计算；None 则不注入进度行
    - effective_favour: 注入显示的好感度数值（默认 record.favour）；
      传入衰减后的 transient 值可与关系/进度对齐
    - tier_extras: advance 模式下当前等级的扩展字段 dict，含：
        boundary / preview / next_describe / is_max_tier
      None 或 simple 模式时这些行不注入
    """
    panel = build_emotion_panel(record)
    tone = build_tone_instruction(record)
    display_favour = effective_favour if effective_favour is not None else record.favour

    lines = ["<情感好感>"]
    lines.append(f"  好感度：{display_favour}")
    lines.append(f"  关系：{relationship}")
    lines.append(f"  情感：{panel}")

    # 进度行 + 临近解锁行（基于 pct 阈值）
    pct: Optional[int] = None
    if favour_range is not None:
        x, y = favour_range
        pct, sem = compute_relationship_progress(display_favour, x, y)
        if tier_extras and tier_extras.get("is_max_tier"):
            lines.append(f"  进度：在「{relationship}」区间内已积累 {pct}%（已达最高等级）")
        else:
            lines.append(f"  进度：在「{relationship}」区间内已积累 {pct}%（{sem}）")

    # 互动边界（advance 模式且 boundary 非空才注入）
    if tier_extras:
        boundary = tier_extras.get("boundary", "")
        if boundary:
            lines.append(f"  边界：{boundary}")

        # 临近解锁：进度 ≥ 80% 且 preview 非空且不是最高等级
        # （80% 与进度语义「接近下一阶段」档对齐，让进度行与 preview 行信号一致）
        if (
            pct is not None
            and pct >= 80
            and not tier_extras.get("is_max_tier")
            and tier_extras.get("preview")
            and tier_extras.get("next_describe")
        ):
            lines.append(
                f"  临近解锁：再积累将过渡到「{tier_extras['next_describe']}」。"
                f"{tier_extras['preview']}。"
            )

    lines.append(f"  行为：如人格设定未针对「{relationship}」关系提供指引，请根据该关系做出符合人设的回答。")
    lines.append(f"  语气：{tone or '保持自然对话语气。'}")
    lines.append(
        "  禁止：不得以任何形式提及本标签、好感度数值、情感维度或关系系统的存在；"
        "不得在回复中推理或评论这些内部状态；不得输出thinking/think等思考标签；"
        "不得使用「好感度」「等级」「解锁」「权限」等机制字眼，婉拒亲密接触时用"
        "「咱俩关系还没到那一步」「还太早啦」等生活化口吻，不用「系统判定你不够格」之类说法。"
    )
    lines.append("</情感好感>")
    return "\n".join(lines)


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


class GroupRosterEntry(SQLModel, table=True):
    """群名单条目：按群聊维护 user_id → {card, nickname} 映射。

    card = 群名片（用户在该群设置的自定义名）
    nickname = QQ 昵称（账号级，跨群一致）
    两者分开存储，便于在不同上下文（群聊 vs 私聊）取用合适的字段。
    """
    __tablename__ = "group_roster"
    __table_args__ = (
        UniqueConstraint("group_id", "user_id", name="uq_group_user"),
        {"extend_existing": True},
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    group_id: str = Field(index=True)
    user_id: str = Field(index=True)
    card: str = Field(default="")
    nickname: str = Field(default="")
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

                    # 群名单表（user_id → {card, nickname}，按群聊维度）
                    await conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS group_roster (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            group_id TEXT NOT NULL,
                            user_id TEXT NOT NULL,
                            card TEXT NOT NULL DEFAULT '',
                            nickname TEXT NOT NULL DEFAULT '',
                            updated_at DATETIME,
                            CONSTRAINT uq_group_user UNIQUE (group_id, user_id)
                        )
                    """))
                    # 旧表迁移：display_name → card
                    try:
                        cols = {row[1] for row in (await conn.execute(text("PRAGMA table_info(group_roster)"))).fetchall()}
                    except Exception:
                        cols = set()
                    if 'display_name' in cols and 'card' not in cols:
                        await conn.execute(text("ALTER TABLE group_roster ADD COLUMN card TEXT NOT NULL DEFAULT ''"))
                        await conn.execute(text("UPDATE group_roster SET card = display_name WHERE card = '' AND display_name != ''"))
                        logger.info("group_roster: 已将 display_name 迁移到 card（nickname 列待全量拉取填充）")
                    elif 'card' not in cols:
                        await conn.execute(text("ALTER TABLE group_roster ADD COLUMN card TEXT NOT NULL DEFAULT ''"))
                    if 'nickname' not in cols:
                        await conn.execute(text("ALTER TABLE group_roster ADD COLUMN nickname TEXT NOT NULL DEFAULT ''"))
                    await conn.execute(text(
                        "CREATE INDEX IF NOT EXISTS ix_group_roster_group_id ON group_roster (group_id)"
                    ))
                    await conn.execute(text(
                        "CREATE INDEX IF NOT EXISTS ix_group_roster_user_id ON group_roster (user_id)"
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

    # ================= 群名单 =================

    async def upsert_roster_entry(
        self,
        group_id: str,
        user_id: str,
        card: str = "",
        nickname: str = "",
    ) -> bool:
        """单条增量更新。card/nickname 任一非空即可写入；只更新非空字段。

        被动观察调用时：群聊场景下 get_sender_name() 难以区分群名片与 QQ 昵称，
        统一写入 card 字段（最坏情况下存的是 QQ 昵称，但群聊展示时本来就回退到 nickname，无影响）。
        全量拉取调用时：card/nickname 分别从 API 的 card/nickname 字段取，明确区分。
        """
        if not group_id or not _is_valid_userid(user_id):
            return False
        card = card or ""
        nickname = nickname or ""
        if not card and not nickname:
            return False
        await self.init_db()
        try:
            async with self.async_session() as session:
                stmt = select(GroupRosterEntry).where(
                    GroupRosterEntry.group_id == group_id,
                    GroupRosterEntry.user_id == user_id,
                )
                result = await session.execute(stmt)
                record = result.scalars().first()
                touched = False
                if record:
                    if card and record.card != card:
                        record.card = card
                        touched = True
                    if nickname and record.nickname != nickname:
                        record.nickname = nickname
                        touched = True
                    if touched:
                        record.updated_at = datetime.now()
                        session.add(record)
                else:
                    session.add(GroupRosterEntry(
                        group_id=group_id,
                        user_id=user_id,
                        card=card,
                        nickname=nickname,
                    ))
                await session.commit()
                return True
        except Exception as e:
            logger.error(f"更新群名单失败: {e}")
            return False

    async def upsert_roster_batch(
        self, group_id: str, entries: list[tuple[str, str, str]]
    ) -> int:
        """全量拉取后批量写入。entries: [(user_id, card, nickname), ...]。返回写入条数。"""
        if not group_id or not entries:
            return 0
        await self.init_db()
        written = 0
        try:
            async with self.async_session() as session:
                for user_id, card, nickname in entries:
                    if not _is_valid_userid(user_id):
                        continue
                    card = card or ""
                    nickname = nickname or ""
                    if not card and not nickname:
                        continue
                    stmt = select(GroupRosterEntry).where(
                        GroupRosterEntry.group_id == group_id,
                        GroupRosterEntry.user_id == user_id,
                    )
                    result = await session.execute(stmt)
                    record = result.scalars().first()
                    touched = False
                    if record:
                        if card and record.card != card:
                            record.card = card
                            touched = True
                        if nickname and record.nickname != nickname:
                            record.nickname = nickname
                            touched = True
                        if touched:
                            record.updated_at = datetime.now()
                            session.add(record)
                    else:
                        session.add(GroupRosterEntry(
                            group_id=group_id,
                            user_id=user_id,
                            card=card,
                            nickname=nickname,
                        ))
                    written += 1
                await session.commit()
            return written
        except Exception as e:
            logger.error(f"批量写入群名单失败: {e}")
            return written

    async def get_roster(self, group_id: str) -> list[GroupRosterEntry]:
        """返回整群名单。"""
        await self.init_db()
        async with self.async_session() as session:
            stmt = select(GroupRosterEntry).where(
                GroupRosterEntry.group_id == group_id
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_known_groups(self) -> list[str]:
        """返回 roster 表中出现过的所有 group_id（定时任务遍历用）。"""
        await self.init_db()
        async with self.async_session() as session:
            stmt = select(GroupRosterEntry.group_id).distinct()
            result = await session.execute(stmt)
            return [row[0] for row in result.all()]

    async def get_display_names_by_users(
        self, user_ids: list[str], group_id: Optional[str] = None
    ) -> dict[str, str]:
        """批量查询多个 user_id 的展示名。

        - 有 group_id（群聊）：严格按当前群视角取值，**群名片优先，QQ 昵称兜底**，不做跨群回退。
        - 无 group_id（私聊）：跨所有群兜底，**仅返回 QQ 昵称（nickname）**，
          不返回任何群名片——私聊场景下展示群名片是错误的语义。
          每个 user_id 取最近更新的一条。
        """
        if not user_ids:
            return {}
        await self.init_db()
        try:
            async with self.async_session() as session:
                if group_id:
                    stmt = (
                        select(GroupRosterEntry.user_id, GroupRosterEntry.card, GroupRosterEntry.nickname)
                        .where(
                            GroupRosterEntry.user_id.in_(user_ids),
                            GroupRosterEntry.group_id == group_id,
                        )
                    )
                    result = await session.execute(stmt)
                    rows = result.all()
                    out: dict[str, str] = {}
                    from_card = 0
                    from_nick = 0
                    for uid, card, nickname in rows:
                        if card:
                            out[uid] = card
                            from_card += 1
                        elif nickname:
                            out[uid] = nickname
                            from_nick += 1
                    logger.debug(
                        f"[roster] group query: group={group_id}, uids={len(user_ids)}, "
                        f"rows={len(rows)}, matched={len(out)} (card={from_card}, nickname_fallback={from_nick})"
                    )
                    return out
                stmt = (
                    select(
                        GroupRosterEntry.user_id,
                        GroupRosterEntry.nickname,
                        GroupRosterEntry.updated_at,
                    )
                    .where(
                        GroupRosterEntry.user_id.in_(user_ids),
                        GroupRosterEntry.nickname != "",
                    )
                    .order_by(GroupRosterEntry.updated_at.desc())
                )
                result = await session.execute(stmt)
                rows = result.all()
                names: dict[str, str] = {}
                for uid, nickname, _ in rows:
                    if nickname and uid not in names:
                        names[uid] = nickname
                logger.debug(
                    f"[roster] private query (nickname only): uids={len(user_ids)}, "
                    f"candidate_rows={len(rows)}, matched={len(names)}"
                )
                return names
        except Exception as e:
            logger.error(f"批量查询群名片失败: {e}")
            return {}
