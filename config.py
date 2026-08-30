"""emotion_favour 类型化配置读取与运行时校验。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


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


@dataclass(frozen=True)
class PluginSettings:
    favour_mode: str
    group_sort_by: str
    min_favour_value: int
    max_favour_value: int
    default_favour: int
    judge_provider: str

    relationship_mode: str
    relationship_simple_list: list[str]
    relationship_advance_raw: Any

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

    settlement_timeout_seconds: float
    terminate_flush_timeout_seconds: float
    judge_request_max_retries: int

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

        adv = config.get("advanced_config", {}) or {}
        if not isinstance(adv, Mapping):
            adv = {}
            warnings.append("advanced_config 不是对象，已回退默认配置")

        favour_min = _as_int(adv.get("favour_change_min"), -5)
        favour_max = _as_int(adv.get("favour_change_max"), 5)
        if favour_min > favour_max:
            warnings.append("favour_change_min 大于 favour_change_max，已交换两者")
            favour_min, favour_max = favour_max, favour_min

        emotion_min = _as_int(adv.get("emotion_change_min"), -10)
        emotion_max = _as_int(adv.get("emotion_change_max"), 5)
        if emotion_min > emotion_max:
            warnings.append("emotion_change_min 大于 emotion_change_max，已交换两者")
            emotion_min, emotion_max = emotion_max, emotion_min

        legacy_rate = adv.get("emotion_decay_rate")
        if legacy_rate is not None and "emotion_decay_rate_volatile" not in adv:
            volatile = standard = sticky = _clamp(_as_float(legacy_rate, 0.85), 0.0, 1.0)
        else:
            volatile = _clamp(_as_float(adv.get("emotion_decay_rate_volatile"), 0.7), 0.0, 1.0)
            standard = _clamp(_as_float(adv.get("emotion_decay_rate_standard"), 0.85), 0.0, 1.0)
            sticky = _clamp(_as_float(adv.get("emotion_decay_rate_sticky"), 0.93), 0.0, 1.0)

        default_favour = _clamp(
            _as_int(config.get("default_favour"), 0), min_favour, max_favour,
        )
        admin_default = _clamp(
            _as_int(adv.get("admin_default_favour"), 50), min_favour, max_favour,
        )

        return cls(
            favour_mode=mode,
            group_sort_by=sort_by,
            min_favour_value=min_favour,
            max_favour_value=max_favour,
            default_favour=default_favour,
            judge_provider=str(config.get("judge_provider", "") or "").strip(),
            relationship_mode=relationship_mode,
            relationship_simple_list=simple_list,
            relationship_advance_raw=rel_conf.get("advance_config", []),
            admin_default_favour=admin_default,
            admin_default_relationship=str(adv.get("admin_default_relationship", "") or "").strip(),
            favour_envoys=frozenset(
                str(value).strip()
                for value in (adv.get("favour_envoys", []) or [])
                if str(value).strip()
            ),
            favour_change_min=favour_min,
            favour_change_max=favour_max,
            emotion_change_min=emotion_min,
            emotion_change_max=emotion_max,
            history_rounds=_clamp(_as_int(adv.get("history_rounds"), 0), 0, 10),
            use_chat_memory=bool(adv.get("use_chat_memory", False)),
            emotion_decay_enabled=bool(adv.get("emotion_decay_enabled", True)),
            emotion_decay_rate_volatile=volatile,
            emotion_decay_rate_standard=standard,
            emotion_decay_rate_sticky=sticky,
            emotion_decay_min_hours=max(0.1, _as_float(adv.get("emotion_decay_min_hours"), 1.0)),
            favour_decay_enabled=bool(adv.get("favour_decay_enabled", True)),
            favour_decay_anchor=_clamp(
                _as_int(adv.get("favour_decay_anchor"), 50), min_favour, max_favour,
            ),
            backup_retention_days=max(
                0, _as_int(adv.get("backup_retention_days"), 0),
            ),
            settlement_timeout_seconds=_clamp(_as_float(adv.get("settlement_timeout_seconds"), 60.0), 5.0, 300.0),
            terminate_flush_timeout_seconds=_clamp(_as_float(adv.get("terminate_flush_timeout_seconds"), 8.0), 1.0, 60.0),
            judge_request_max_retries=_clamp(
                _as_int(adv.get("judge_request_max_retries"), 5), 1, 10,
            ),
            log_with_bot_id=bool(config.get("log_with_bot_id", False)),
            warnings=tuple(warnings),
        )


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
