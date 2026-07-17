"""emotion_favour main.py 的 _get_max_tier / _get_tier_extras / _get_relationship_range 测试。

main.py 顶部 import 了 astrbot / sqlmodel 等本地未装的库，
按 plugin_local_testing 记忆，把目标方法从源码里以字符串提取出来 exec 到
独立命名空间，绕过模块顶部 import。方法内部调用的 self 字段（admin_default_relationship /
_special_user_ids / relationship_advance_raw 等）用 SimpleNamespace stub 提供。

运行：
    python3 tests/test_tier_extras.py
    python3 -m unittest tests.test_tier_extras -v
"""
import json
import logging
import os
import re
import textwrap
import types
import unittest
from typing import Optional, Tuple

from config import validate_advance_tiers

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN_PY = os.path.join(HERE, "..", "main.py")


def _extract_method(src: str, name: str) -> str:
    """从 main.py 源码提取指定类方法的源（含 def 行），按 4 空格缩进 dedent 到顶层。
    扫描 `    def name(`（class body 里的方法）起到下一个同缩进 def 或类外结构止。"""
    lines = src.splitlines(keepends=True)
    start = None
    indent = ""
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)def " + re.escape(name) + r"\(", line)
        if m and m.group(1):  # 必须非零缩进（类内方法）
            start = i
            indent = m.group(1)
            break
    if start is None:
        raise ValueError(f"未找到类方法 {name}")
    end = len(lines)
    pad_len = len(indent)
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if not line.strip():
            continue  # 空行不算结构边界
        m = re.match(r"^(\s*)def ", line)
        if m and len(m.group(1)) == pad_len:
            end = j
            break
        # 走到类外（顶层 def / class / 装饰器）也停
        if not (line.startswith(" ") or line.startswith("\t")):
            end = j
            break
    method_block = "".join(lines[start:end])
    return textwrap.dedent(method_block)


def _load_methods():
    """提取 main.py 的 _advance_items / _find_tier / _find_next_tier / _get_max_tier /
    _is_special_override / _get_tier_extras / _get_relationship_range，exec 到共享命名空间。"""
    with open(MAIN_PY, encoding="utf-8") as f:
        src = f.read()
    # from __future__ import annotations 让函数签名里的 Optional[...] / Tuple[...] 等
    # 类型注解延迟为字符串不求值，避免需要 stub 真实 typing 对象
    prefix = "from __future__ import annotations\n\n"
    ns = {
        "json": json,
        "logger": logging.getLogger("test_tier_extras"),
        "Optional": Optional,
        "Tuple": Tuple,
        "validate_advance_tiers": validate_advance_tiers,
    }
    for name in [
        "_advance_items",
        "_find_tier",
        "_find_next_tier",
        "_get_max_tier",
        "_is_special_override",
        "_get_tier_extras",
        "_get_relationship_range",
    ]:
        exec(prefix + _extract_method(src, name), ns)
    return ns


def _make_plugin(items, *, admin_default_relationship="", special_user_ids=None,
                 relationship_mode="advance", min_value=-100, max_value=100):
    """构造 stub plugin：用 SimpleNamespace 提供方法所需的 self 字段，把提取出的方法绑上去。"""
    methods = _load_methods()
    p = types.SimpleNamespace()
    p.relationship_advance_raw = items
    p.relationship_mode = relationship_mode
    p.min_favour_value = min_value
    p.max_favour_value = max_value
    p.admin_default_relationship = admin_default_relationship
    p._special_user_ids = set(special_user_ids or [])
    # 绑方法（self 显式作为第一参数传入）
    p._advance_items = lambda: methods["_advance_items"](p)
    p._find_tier = lambda f: methods["_find_tier"](p, f)
    p._find_next_tier = lambda f: methods["_find_next_tier"](p, f)
    p._get_max_tier = lambda: methods["_get_max_tier"](p)
    p._is_special_override = lambda uid: methods["_is_special_override"](p, uid)
    p._get_tier_extras = lambda f, uid="": methods["_get_tier_extras"](p, f, uid)
    p._get_relationship_range = lambda f, uid="": methods["_get_relationship_range"](p, f, uid)
    return p


# 5 级配置样本（与文档示例对齐）：失望 / 陌生 / 友好 / 喜欢 / 挚爱
SAMPLE_ITEMS = [
    {"describe": "失望", "min_value": -100, "max_value": -30,
     "boundary": "回避：所有身体接触", "preview": "", "rule": "R-disappointed"},
    {"describe": "陌生", "min_value": -29, "max_value": 30,
     "boundary": "回避：亲密接触；允许：礼貌性接触", "preview": "可流露轻度友善", "rule": "R-stranger"},
    {"describe": "友好", "min_value": 31, "max_value": 60,
     "boundary": "允许：朋友式接触（拍肩、击掌）", "preview": "可流露轻度亲密（牵手）", "rule": "R-friendly"},
    {"describe": "喜欢", "min_value": 61, "max_value": 100,
     "boundary": "允许：牵手、摸头；临界：拥抱", "preview": "可流露深度亲密", "rule": "R-like"},
    {"describe": "挚爱", "min_value": 101, "max_value": 300,
     "boundary": "允许：深度亲密接触", "preview": "", "rule": "R-love"},
]


# ======================= _get_max_tier =======================


class TestGetMaxTier(unittest.TestCase):
    def setUp(self):
        self.plugin = _make_plugin(SAMPLE_ITEMS)

    def test_returns_highest_min_value_tier(self):
        """5 级配置中 max_tier 应是 min_value 最高的「挚爱」"""
        tier = self.plugin._get_max_tier()
        self.assertEqual(tier["describe"], "挚爱")
        self.assertEqual(tier["min_value"], 101)

    def test_empty_advance_returns_none(self):
        p = _make_plugin([])
        self.assertIsNone(p._get_max_tier())

    def test_unsorted_input_still_picks_max(self):
        """打乱顺序的配置仍应按 min_value 而非列表顺序选最高"""
        shuffled = list(reversed(SAMPLE_ITEMS))
        p = _make_plugin(shuffled)
        tier = p._get_max_tier()
        self.assertEqual(tier["describe"], "挚爱")


# ======================= _get_tier_extras（特殊用户覆盖） =======================


class TestTierExtrasSpecialOverride(unittest.TestCase):
    """特殊用户应走「最高等级」分支：boundary/rule/preview 用最高等级，
    is_max_tier=True，next_describe=""。"""

    def setUp(self):
        self.special_uid = "10001"
        self.plugin = _make_plugin(
            SAMPLE_ITEMS,
            admin_default_relationship="特殊",
            special_user_ids={self.special_uid},
        )

    def test_special_user_uses_max_tier_boundary(self):
        """特殊用户的 boundary 应来自最高等级「挚爱」，而非 favour 实际所处等级"""
        extras = self.plugin._get_tier_extras(50, self.special_uid)
        self.assertEqual(extras["boundary"], "允许：深度亲密接触")  # 来自「挚爱」
        self.assertEqual(extras["rule"], "R-love")

    def test_special_user_is_max_tier(self):
        extras = self.plugin._get_tier_extras(50, self.special_uid)
        self.assertTrue(extras["is_max_tier"])

    def test_special_user_next_describe_empty(self):
        """特殊用户走最高等级，没有「下一阶段」"""
        extras = self.plugin._get_tier_extras(50, self.special_uid)
        self.assertEqual(extras["next_describe"], "")

    def test_special_user_preview_from_max_tier_but_skipped_by_is_max(self):
        """特殊用户的 preview 字段值取自最高等级（这里挚爱的 preview 为空字符串）；
        消费方 (build_injection_prompt) 会因 is_max_tier=True 跳过 preview 注入"""
        extras = self.plugin._get_tier_extras(50, self.special_uid)
        self.assertEqual(extras["preview"], "")  # 挚爱的 preview 本就为空

    def test_special_user_high_favour_still_uses_max_tier(self):
        """即使特殊用户 favour 落在中间等级，boundary 仍取最高等级"""
        extras = self.plugin._get_tier_extras(80, self.special_uid)
        self.assertEqual(extras["boundary"], "允许：深度亲密接触")
        self.assertEqual(extras["rule"], "R-love")

    def test_regular_user_uses_actual_tier(self):
        """普通用户走原 _find_tier 逻辑"""
        extras = self.plugin._get_tier_extras(50, "regular-uid")
        # favour=50 → 落在「陌生」(-29~30)? 不对，50 在 31~60 → 「友好」
        self.assertEqual(extras["boundary"], "允许：朋友式接触（拍肩、击掌）")
        self.assertEqual(extras["rule"], "R-friendly")
        self.assertFalse(extras["is_max_tier"])
        self.assertEqual(extras["next_describe"], "喜欢")

    def test_regular_user_at_max_tier(self):
        """普通用户 favour 落在最高等级时也返回 is_max_tier=True"""
        extras = self.plugin._get_tier_extras(150, "regular-uid")
        self.assertEqual(extras["boundary"], "允许：深度亲密接触")
        self.assertTrue(extras["is_max_tier"])
        self.assertEqual(extras["next_describe"], "")

    def test_special_override_requires_relationship(self):
        """admin_default_relationship 为空时不走特殊覆盖"""
        p = _make_plugin(SAMPLE_ITEMS, admin_default_relationship="",
                         special_user_ids={self.special_uid})
        extras = p._get_tier_extras(50, self.special_uid)
        # 走原逻辑：favour=50 → 「友好」
        self.assertEqual(extras["rule"], "R-friendly")

    def test_special_override_requires_uid_in_explicit_list(self):
        """uid 不在显式列表时不走特殊覆盖"""
        p = _make_plugin(SAMPLE_ITEMS, admin_default_relationship="特殊",
                         special_user_ids={"other-uid"})
        extras = p._get_tier_extras(50, self.special_uid)
        self.assertEqual(extras["rule"], "R-friendly")

    def test_special_override_with_empty_advance_config(self):
        """advance 配置为空时特殊用户路径返回空 extras"""
        p = _make_plugin([], admin_default_relationship="特殊",
                         special_user_ids={self.special_uid})
        extras = p._get_tier_extras(50, self.special_uid)
        self.assertEqual(extras["boundary"], "")
        self.assertEqual(extras["rule"], "")
        self.assertFalse(extras["is_max_tier"])


# ======================= _get_relationship_range（特殊用户覆盖） =======================


class TestRelationshipRangeSpecialOverride(unittest.TestCase):
    """特殊用户在 advance 模式下应返回最高等级的 (x, y)。"""

    def setUp(self):
        self.special_uid = "10001"
        self.plugin = _make_plugin(
            SAMPLE_ITEMS,
            admin_default_relationship="特殊",
            special_user_ids={self.special_uid},
        )

    def test_special_user_returns_max_tier_range(self):
        """特殊用户的 range 应是最高等级「挚爱」(101, 300)"""
        r = self.plugin._get_relationship_range(50, self.special_uid)
        self.assertEqual(r, (101, 300))

    def test_special_user_range_high_favour_unchanged(self):
        """favour 高低不影响特殊用户的 range 取值"""
        r1 = self.plugin._get_relationship_range(0, self.special_uid)
        r2 = self.plugin._get_relationship_range(200, self.special_uid)
        self.assertEqual(r1, (101, 300))
        self.assertEqual(r2, (101, 300))

    def test_regular_user_uses_actual_favour_range(self):
        """普通用户 favour=50 → 落在「友好」(31, 60)"""
        r = self.plugin._get_relationship_range(50, "regular-uid")
        self.assertEqual(r, (31, 60))

    def test_special_user_simple_mode_returns_none(self):
        """simple 模式下特殊用户也返回 None（无 tier 概念）"""
        p = _make_plugin(SAMPLE_ITEMS, admin_default_relationship="特殊",
                         special_user_ids={self.special_uid},
                         relationship_mode="simple")
        r = p._get_relationship_range(50, self.special_uid)
        self.assertIsNone(r)

    def test_special_user_advance_empty_config_returns_none(self):
        """advance 配置为空时特殊用户返回 None"""
        p = _make_plugin([], admin_default_relationship="特殊",
                         special_user_ids={self.special_uid})
        r = p._get_relationship_range(50, self.special_uid)
        self.assertIsNone(r)


# ======================= _is_special_override =======================


class TestIsSpecialOverride(unittest.TestCase):
    def test_both_conditions_required(self):
        p = _make_plugin(SAMPLE_ITEMS, admin_default_relationship="特殊",
                         special_user_ids={"A"})
        self.assertTrue(p._is_special_override("A"))
        self.assertFalse(p._is_special_override("B"))
        self.assertFalse(p._is_special_override(""))

        p2 = _make_plugin(SAMPLE_ITEMS, admin_default_relationship="",
                          special_user_ids={"A"})
        self.assertFalse(p2._is_special_override("A"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
