"""关系档位与特殊用户覆盖测试。

只留静默失效点：特殊用户走最高档、普通用户的档位配置不被忽略、空 ID 不放行。
这些判错都不会报错，只会让某些用户悄悄拿到错档位的边界与规则。

运行：
    python3 -m unittest tests.test_tier_extras -v
"""
import importlib.util
import json
import logging
import types
import unittest
from pathlib import Path


def _load_shared():
    """按路径加载 tests/_shared.py（pytest 下不能按包名 import）。"""
    path = Path(__file__).resolve().parent / "_shared.py"
    spec = importlib.util.spec_from_file_location("ef_tests_shared", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

_shared = _load_shared()
extract_method = _shared.extract_method
read_main_source = _shared.read_main_source

from config import validate_advance_tiers  # noqa: E402

TIER_METHODS = (
    "_advance_items",
    "_find_tier",
    "_find_next_tier",
    "_get_max_tier",
    "_is_special_override",
    "_get_tier_extras",
)


def _load_methods():
    """提取 main.py 的档位方法，exec 到同一命名空间供桩对象调用。"""
    src = read_main_source()
    # from __future__ import annotations 让签名里的 Optional[...] 等注解延迟为字符串
    prefix = "from __future__ import annotations\n\n"
    ns = {
        "json": json,
        "logger": logging.getLogger("test_tier_extras"),
        "validate_advance_tiers": validate_advance_tiers,
    }
    for name in TIER_METHODS:
        exec(prefix + extract_method(src, name), ns)
    return ns


def _make_plugin(items, *, admin_default_relationship="", special_user_ids=None,
                 relationship_mode="advance", min_value=-100, max_value=100):
    """构造 stub plugin：用 SimpleNamespace 提供方法所需的 self 字段，把提取出的方法绑上去。"""
    methods = _load_methods()
    plugin = types.SimpleNamespace()
    plugin.relationship_advance_raw = items
    plugin.relationship_mode = relationship_mode
    plugin.min_favour_value = min_value
    plugin.max_favour_value = max_value
    plugin.admin_default_relationship = admin_default_relationship
    plugin._special_user_ids = set(special_user_ids or [])
    plugin._advance_items = lambda: methods["_advance_items"](plugin)
    plugin._find_tier = lambda f: methods["_find_tier"](plugin, f)
    plugin._find_next_tier = lambda f: methods["_find_next_tier"](plugin, f)
    plugin._get_max_tier = lambda: methods["_get_max_tier"](plugin)
    plugin._is_special_override = lambda uid: methods["_is_special_override"](plugin, uid)
    plugin._get_tier_extras = lambda f, uid="": methods["_get_tier_extras"](plugin, f, uid)
    return plugin

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


class TestTierExtras(unittest.TestCase):
    def test_special_override_uses_max_tier_and_leaves_regular_users_alone(self):
        """守特殊关系覆盖的两个方向：显式特殊 ID 固定取最高等级（boundary/rule 用
        挚爱、is_max_tier=True、无下一级），普通用户仍按实际档位取（favour=50 在
        「友好」）。判宽了所有人都拿最高档边界，判窄了特殊用户悄悄失去覆盖。"""
        plugin = _make_plugin(
            SAMPLE_ITEMS,
            admin_default_relationship="特殊",
            special_user_ids={"10001"},
        )
        special = plugin._get_tier_extras(50, "10001")
        self.assertEqual(special["boundary"], "允许：深度亲密接触")
        self.assertEqual(special["rule"], "R-love")
        self.assertTrue(special["is_max_tier"])
        self.assertEqual(special["next_describe"], "")

        regular = plugin._get_tier_extras(50, "regular-uid")
        self.assertEqual(regular["boundary"], "允许：朋友式接触（拍肩、击掌）")
        self.assertEqual(regular["rule"], "R-friendly")
        self.assertFalse(regular["is_max_tier"])
        self.assertEqual(regular["next_describe"], "喜欢")


class TestIsSpecialOverride(unittest.TestCase):
    def test_requires_explicit_id_and_configured_relationship(self):
        """安全边界：特殊覆盖要「配了特殊关系名 + ID 在显式列表 + ID 非空」三个条件
        同时成立。任一条件被放宽都会静默把普通用户升级成特殊待遇。空 ID 单列一条：
        配置真混进空串时（构造处现在会过滤，但这里是最后一道防线），任何空 sender_id
        都会被当成特殊用户，所以把空串放进列表再断言它不生效。"""
        plugin = _make_plugin(SAMPLE_ITEMS, admin_default_relationship="特殊",
                              special_user_ids={"A", ""})
        self.assertTrue(plugin._is_special_override("A"))
        self.assertFalse(plugin._is_special_override("B"))
        self.assertFalse(plugin._is_special_override(""))

        no_default = _make_plugin(SAMPLE_ITEMS, admin_default_relationship="",
                                  special_user_ids={"A"})
        self.assertFalse(no_default._is_special_override("A"))

if __name__ == "__main__":
    unittest.main(verbosity=2)
