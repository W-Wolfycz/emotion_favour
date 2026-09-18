"""关系档位与特殊用户覆盖测试。

只留静默失效点：特殊用户走最高档、普通用户的档位配置不被忽略、空 ID 不放行。

运行：
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

from support import extract_method as _extract_method  # noqa: E402

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

    def test_special_user_always_uses_max_tier(self):
        """特殊用户固定走「最高等级」分支：boundary/rule 取挚爱，is_max_tier=True 且无下一级。

        该分支不读 favour，favour=50（实际落在「友好」）与 80 走的是同一条路径。
        """
        for favour in (50, 80):
            with self.subTest(favour=favour):
                extras = self.plugin._get_tier_extras(favour, self.special_uid)
                self.assertEqual(extras["boundary"], "允许：深度亲密接触")  # 来自「挚爱」
                self.assertEqual(extras["rule"], "R-love")
                self.assertTrue(extras["is_max_tier"])
                self.assertEqual(extras["next_describe"], "")

    def test_regular_user_uses_actual_tier(self):
        """普通用户走原 _find_tier 逻辑"""
        extras = self.plugin._get_tier_extras(50, "regular-uid")
        # favour=50 → 落在「陌生」(-29~30)? 不对，50 在 31~60 → 「友好」
        self.assertEqual(extras["boundary"], "允许：朋友式接触（拍肩、击掌）")
        self.assertEqual(extras["rule"], "R-friendly")
        self.assertFalse(extras["is_max_tier"])
        self.assertEqual(extras["next_describe"], "喜欢")

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
