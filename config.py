"""emotion_favour 类型化配置读取与运行时校验。"""

from __future__ import annotations

import json

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, NamedTuple


DEFAULT_RELATIONSHIPS = [
    "极度厌恶", "厌恶", "反感", "普通", "喜欢", "亲密", "挚爱",
]


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value, low, high):
    return max(low, min(high, value))


MENTION_SCOPES = ("all", "group", "mention")


def _mention_scope(value: Any, warnings: list) -> str:
    """注入范围：只接受 all / group / mention，写错就落回 all 并留一条告警。"""
    scope = str(value or "").strip().lower()
    if scope in MENTION_SCOPES:
        return scope
    if scope:
        warnings.append(f"mention_scope 取值 {scope!r} 无效，已按 all 处理")
    return "all"


def _group(config: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    """取一个 object 分组；缺失或类型不对时当空组处理。"""
    value = config.get(name, {}) or {}
    return value if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class PluginSettings:
    favour_mode: str
    group_sort_by: str
    min_favour_value: int
    max_favour_value: int
    default_favour: int
    llm_provider: str

    relationship_mode: str
    relationship_simple_list: list[str]
    relationship_advance_raw: Any

    mention_favour_enabled: bool
    mention_scope: str
    mention_favour_max: int
    mention_name_match: bool

    admin_default_favour: int
    admin_default_relationship: str
    favour_envoys: frozenset[str]
    favour_change_min: int
    favour_change_max: int
    emotion_change_min: int
    emotion_change_max: int

    history_rounds: int
    use_chat_memory: bool
    emotion_decay_enabled: bool
    emotion_decay_rate_volatile: float
    emotion_decay_rate_standard: float
    emotion_decay_rate_sticky: float
    emotion_decay_min_hours: float
    favour_decay_enabled: bool
    favour_decay_anchor: int
    backup_retention_days: int

    scar_enabled: bool
    scar_penalty_k: float
    scar_repair_k2: float

    settlement_timeout_seconds: float
    terminate_flush_timeout_seconds: float
    llm_request_max_retries: int
    tier_script_enabled: bool

    log_with_bot_id: bool

    warnings: tuple[str, ...]

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any]) -> "PluginSettings":
        warnings: list[str] = []

        min_favour = _as_int(config.get("min_favour_value"), -100)
        max_favour = _as_int(config.get("max_favour_value"), 100)
        if min_favour >= max_favour:
            warnings.append("min_favour_value 必须小于 max_favour_value，已回退 -100~100")
            min_favour, max_favour = -100, 100

        mode = str(config.get("favour_mode", "normal") or "normal").lower()
        if mode not in {"galgame", "normal", "realistic"}:
            warnings.append(f"未知 favour_mode={mode!r}，已回退 normal")
            mode = "normal"

        sort_by = str(config.get("group_sort_by", "default") or "default")
        if sort_by not in {"default", "favour", "nickname", "userid"}:
            warnings.append(f"未知 group_sort_by={sort_by!r}，已回退 default")
            sort_by = "default"

        rel_conf = config.get("relationship_config", {}) or {}
        if not isinstance(rel_conf, Mapping):
            rel_conf = {}
            warnings.append("relationship_config 不是对象，已回退默认配置")
        relationship_mode = str(rel_conf.get("mode", "simple") or "simple")
        if relationship_mode not in {"simple", "advance"}:
            warnings.append(f"未知 relationship mode={relationship_mode!r}，已回退 simple")
            relationship_mode = "simple"
        simple_list = rel_conf.get("simple_list", DEFAULT_RELATIONSHIPS)
        if not isinstance(simple_list, list) or not any(str(x).strip() for x in simple_list):
            simple_list = list(DEFAULT_RELATIONSHIPS)
            warnings.append("simple_list 为空或类型错误，已回退默认关系列表")
        else:
            simple_list = [str(x).strip() for x in simple_list if str(x).strip()]

        # v3.6.0 把原先混在一起的 advanced_config 拆成了下面几组；旧路径以隐藏项形式
        # 保留在 schema 里，所以这里"新路径优先、旧路径兜底"，老部署升级后不必重填。
        settlement = _group(config, "settlement_config")
        decay = settlement  # v3.6.0 起衰减并入 settlement_config（结算与衰减）
        access = _group(config, "access_config")
        runtime = _group(config, "runtime_config")
        mention = _group(config, "mention_config")
        legacy = _group(config, "advanced_config")
        for name in ("advanced_config", "settlement_config",
                     "access_config", "runtime_config", "mention_config"):
            if name in config and not isinstance(config[name], Mapping):
                warnings.append(f"{name} 不是对象，已忽略")

        def moved(group, key, default=None):
            """读搬过组的配置项：新分组优先，旧 advanced_config 兜底。

            AstrBot 建配置时会按 schema 给每个键补默认值（`_config_schema_to_default_config`），
            所以"新键存在"根本不能说明用户在新分组里填过——升级后新键立刻等于默认值。
            因此只有在「新值与默认值不同」时才认新路径，否则一律以旧路径的值为准；
            两边都没有时用代码里的默认值。
            """
            value = group.get(key)
            legacy_value = legacy.get(key)
            if value is None:
                return legacy_value if legacy_value is not None else default
            if legacy_value is not None and value == default:
                return legacy_value
            return value

        def moved_bool(group, key, default):
            value = moved(group, key, default)
            return default if value is None else bool(value)

        favour_min = _as_int(moved(settlement, "favour_change_min", -5), -5)
        favour_max = _as_int(moved(settlement, "favour_change_max", 5), 5)
        if favour_min > favour_max:
            warnings.append("favour_change_min 大于 favour_change_max，已交换两者")
            favour_min, favour_max = favour_max, favour_min

        emotion_min = _as_int(moved(settlement, "emotion_change_min", -10), -10)
        emotion_max = _as_int(moved(settlement, "emotion_change_max", 5), 5)
        if emotion_min > emotion_max:
            warnings.append("emotion_change_min 大于 emotion_change_max，已交换两者")
            emotion_min, emotion_max = emotion_max, emotion_min

        # 旧单值键 emotion_decay_rate 的兼容分支已删：AstrBot 构造插件配置时会按 schema
        # 递归补默认值并丢弃 schema 之外的旧键，插件拿到 conf 时它必然不存在。
        volatile = _clamp(_as_float(moved(decay, "emotion_decay_rate_volatile", 0.7), 0.7), 0.0, 1.0)
        standard = _clamp(_as_float(moved(decay, "emotion_decay_rate_standard", 0.85), 0.85), 0.0, 1.0)
        sticky = _clamp(_as_float(moved(decay, "emotion_decay_rate_sticky", 0.93), 0.93), 0.0, 1.0)

        default_favour = _clamp(
            _as_int(config.get("default_favour"), 0), min_favour, max_favour,
        )
        admin_default = _clamp(
            _as_int(moved(access, "admin_default_favour", 50), 50), min_favour, max_favour,
        )

        return cls(
            favour_mode=mode,
            group_sort_by=sort_by,
            min_favour_value=min_favour,
            max_favour_value=max_favour,
            default_favour=default_favour,
            llm_provider=str(config.get("llm_provider", "") or "").strip(),
            relationship_mode=relationship_mode,
            relationship_simple_list=simple_list,
            relationship_advance_raw=rel_conf.get("advance_config", []),
            admin_default_favour=admin_default,
            admin_default_relationship=str(moved(access, "admin_default_relationship", "") or "").strip(),
            favour_envoys=frozenset(
                str(value).strip()
                for value in (moved(access, "favour_envoys", []) or [])
                if str(value).strip()
            ),
            favour_change_min=favour_min,
            favour_change_max=favour_max,
            emotion_change_min=emotion_min,
            emotion_change_max=emotion_max,
            mention_favour_enabled=moved_bool(mention, "mention_favour_enabled", True),
            mention_scope=_mention_scope(
                moved(mention, "mention_scope", "all"), warnings
            ),
            mention_favour_max=_clamp(
                _as_int(moved(mention, "mention_favour_max", 20), 20), 1, 50,
            ),
            mention_name_match=moved_bool(mention, "mention_name_match", False),
            history_rounds=_clamp(_as_int(moved(runtime, "history_rounds", 0), 0), 0, 10),
            use_chat_memory=moved_bool(runtime, "use_chat_memory", False),
            emotion_decay_enabled=moved_bool(decay, "emotion_decay_enabled", True),
            emotion_decay_rate_volatile=volatile,
            emotion_decay_rate_standard=standard,
            emotion_decay_rate_sticky=sticky,
            emotion_decay_min_hours=max(
                0.1, _as_float(moved(decay, "emotion_decay_min_hours", 1.0), 1.0)
            ),
            favour_decay_enabled=moved_bool(decay, "favour_decay_enabled", True),
            favour_decay_anchor=_clamp(
                _as_int(moved(decay, "favour_decay_anchor", 50), 50), min_favour, max_favour,
            ),
            backup_retention_days=max(
                0, _as_int(moved(runtime, "backup_retention_days", 0), 0),
            ),
            scar_enabled=moved_bool(settlement, "scar_enabled", False),
            # k 就是满痕时的伤害倍率上限（1~4）：单轮最大伤害天然是「每轮额度 × k」，
            # 不需要另设绝对上限。
            scar_penalty_k=_clamp(_as_float(moved(settlement, "scar_penalty_k", 2.0), 2.0), 1.0, 4.0),
            # k2 是满痕时的修复倍率下限（0~1）：0 表示伤痕满时善意完全无法修复。
            scar_repair_k2=_clamp(_as_float(moved(settlement, "scar_repair_k2", 0.5), 0.5), 0.0, 1.0),
            settlement_timeout_seconds=_clamp(_as_float(moved(runtime, "settlement_timeout_seconds", 60.0), 60.0), 5.0, 300.0),
            terminate_flush_timeout_seconds=_clamp(_as_float(moved(runtime, "terminate_flush_timeout_seconds", 8.0), 8.0), 1.0, 60.0),
            llm_request_max_retries=_clamp(
                _as_int(moved(runtime, "llm_request_max_retries", 5), 5), 1, 10,
            ),
            tier_script_enabled=moved_bool(config, "tier_script_enabled", True),
            log_with_bot_id=bool(config.get("log_with_bot_id", False)),
            warnings=tuple(warnings),
        )


# ==================== v3.6.0 配置分组迁移 ====================
# 旧 advanced_config 的键 → 新位置（键名 "__top__" 表示提到顶层）。
CONFIG_GROUP_MOVES: dict[str, tuple[str, ...]] = {
    "settlement_config": (
        "favour_change_min", "favour_change_max",
        "emotion_change_min", "emotion_change_max",
        "scar_enabled", "scar_penalty_k", "scar_repair_k2",
        "emotion_decay_enabled", "emotion_decay_rate_volatile",
        "emotion_decay_rate_standard", "emotion_decay_rate_sticky",
        "emotion_decay_min_hours", "favour_decay_enabled", "favour_decay_anchor",
    ),
    "access_config": (
        "admin_default_favour", "admin_default_relationship", "favour_envoys",
    ),
    "runtime_config": (
        "history_rounds", "use_chat_memory", "backup_retention_days",
        "settlement_timeout_seconds", "terminate_flush_timeout_seconds",
        "llm_request_max_retries",
    ),
    "__top__": ("tier_script_enabled",),
}

LEGACY_CONFIG_GROUP = "advanced_config"

# 配置类型 → 缺失 `default` 时 AstrBot 用的类型零值（与 core 的 DEFAULT_VALUE_MAP 一致）
_SCHEMA_TYPE_ZERO = {
    "int": 0, "float": 0.0, "bool": False, "string": "", "text": "",
    "list": [], "object": {}, "template_list": [], "dict": {},
}


class ConfigMigrationReport(NamedTuple):
    """迁移结果：搬了哪些键、删了哪些旧键、是否需要写盘。"""

    moved: tuple[str, ...]
    removed: tuple[str, ...]
    changed: bool


def schema_defaults(schema: Mapping[str, Any]) -> dict:
    """把 schema 解析成默认值表（object 递归）。"""
    out: dict[str, Any] = {}
    for key, item in schema.items():
        if not isinstance(item, Mapping):
            continue
        if item.get("type") == "object":
            out[key] = schema_defaults(item.get("items", {}) or {})
        elif "default" in item:
            out[key] = item["default"]
        else:
            out[key] = _SCHEMA_TYPE_ZERO.get(str(item.get("type")), "")
    return out


def load_schema_defaults(schema_path) -> dict:
    """读插件自己的 `_conf_schema.json` 解析默认值。

    不能用 `config.default_config`：AstrBotConfig 在构造时先把该属性设成"传入的
    default_config"（插件路径下就是全局配置），之后才用 schema 解析结果覆盖局部变量，
    所以属性上拿不到本插件的默认值——之前据此判断，结果一条都搬不动。
    """
    try:
        raw = Path(schema_path).read_text(encoding="utf-8-sig")
        parsed = json.loads(raw)
    except (OSError, ValueError):
        return {}
    return schema_defaults(parsed) if isinstance(parsed, Mapping) else {}


def migrate_config_layout(
    config,
    *,
    defaults: Mapping[str, Any],
    legacy_group: str = LEGACY_CONFIG_GROUP,
) -> ConfigMigrationReport:
    """把旧分组的取值搬进新分组并删掉旧键（幂等）。

    AstrBot 会按 schema 给每个键补默认值，所以"新分组里有值"不代表用户填过——
    只有「新值等于默认值」时才认为该键还没被用户在新位置设置过，此时以旧值为准。
    `defaults` 必须是本插件 schema 解析出的默认值表；拿不到就什么都不做（不敢删，
    删了可能连值一起丢）。
    """
    legacy = config.get(legacy_group)
    if not isinstance(legacy, Mapping) or not legacy:
        return ConfigMigrationReport((), (), False)
    default_group = defaults.get(legacy_group) if isinstance(defaults, Mapping) else None
    if not isinstance(default_group, Mapping):
        return ConfigMigrationReport((), (), False)

    moved: list[str] = []
    removed: list[str] = []
    changed = False
    for group_name, keys in CONFIG_GROUP_MOVES.items():
        target = config if group_name == "__top__" else config.get(group_name)
        if not isinstance(target, dict):
            continue
        for key in keys:
            if key not in legacy:
                continue
            legacy_value = legacy[key]
            default_value = default_group.get(key)
            if legacy_value != default_value:
                changed = True
                current = target.get(key, default_value)
                if current in (None, default_value):
                    target[key] = legacy_value
                    moved.append(key)
            legacy.pop(key, None)
            removed.append(key)

    if not legacy:
        config.pop(legacy_group, None)
    return ConfigMigrationReport(tuple(moved), tuple(removed), changed)


def validate_advance_tiers(items: list[dict]) -> tuple[list[dict], list[str]]:
    """排序并校验 advance tier；重叠项被忽略，避免列表顺序未定义行为。"""
    normalized: list[dict] = []
    warnings: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            warnings.append(f"advance_config 第 {index + 1} 项不是对象，已忽略")
            continue
        try:
            low = int(item.get("min_value", 0))
            high = int(item.get("max_value", 0))
        except (TypeError, ValueError):
            warnings.append(f"advance_config 第 {index + 1} 项区间不是整数，已忽略")
            continue
        if low > high:
            warnings.append(f"advance_config 第 {index + 1} 项 min_value > max_value，已忽略")
            continue
        copied = dict(item)
        copied["min_value"] = low
        copied["max_value"] = high
        normalized.append(copied)

    normalized.sort(key=lambda item: (item["min_value"], item["max_value"]))
    accepted: list[dict] = []
    previous_high: int | None = None
    for item in normalized:
        if previous_high is not None and item["min_value"] <= previous_high:
            warnings.append(
                f"关系区间 [{item['min_value']}, {item['max_value']}] 与前一区间重叠，已忽略"
            )
            continue
        accepted.append(item)
        previous_high = item["max_value"]
    return accepted, warnings
