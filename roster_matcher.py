# roster_matcher.py
"""群名单模糊匹配工具：给定查询字符串（"小明"/"明哥"/"星球（上班版）"），
从群名单中按相似度返回 Top-N 候选 (user_id, display_name, score)。

打分综合三种 fuzz 指标取最大值：
- token_sort_ratio：词序无关的整体相似度（应对"小明 上班版" vs "上班版 小明"）
- partial_ratio * 0.95：子串匹配（应对"小明" vs "小明（上班版）"），轻微惩罚以避免短查询误高分
- ratio * 0.9：原始相似度，作为兜底
"""
from rapidfuzz import fuzz, process

DEFAULT_THRESHOLD = 70
DEFAULT_LIMIT = 5


def _scorer(s1: str, s2: str, **_) -> float:
    return max(
        fuzz.token_sort_ratio(s1, s2),
        fuzz.partial_ratio(s1, s2) * 0.95,
        fuzz.ratio(s1, s2) * 0.9,
    )


def match_member(
    roster: list[dict],
    query: str,
    threshold: int = DEFAULT_THRESHOLD,
    limit: int = DEFAULT_LIMIT,
) -> list[tuple[str, str, float]]:
    """在群名单中模糊匹配查询字符串。

    Args:
        roster: [{"user_id": "...", "display_name": "..."}, ...]
            display_name 由调用方按 "card 优先，否则 nickname，都无则跳过" 的统一规则预计算。
            本函数只对单个 display_name 评分，不会同时拿 card 和 nickname 算两次取高分——
            即"群昵称 60 分 / QQ 昵称 100 分"的场景，分数仍取 60（card 非空时）。
        query: 待匹配的查询字符串（用户输入或裁决模型抽取的目标名）
        threshold: 0-100，低于此分数的候选不返回
        limit: 返回候选数上限

    Returns:
        [(user_id, display_name, score), ...] 按分数降序
    """
    if not roster or not query or not query.strip():
        return []
    names = {
        e["display_name"]: e["user_id"]
        for e in roster
        if e.get("display_name") and e.get("user_id")
    }
    if not names:
        return []

    results = process.extract(
        query, list(names.keys()), scorer=_scorer, limit=limit
    )
    # rapidfuzz 3.x: results 是 (match, score, index) 三元组
    return [
        (names[name], name, float(score))
        for name, score, _ in results
        if score >= threshold
    ]
