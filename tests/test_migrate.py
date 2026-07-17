"""emotion_favour 配置迁移测试。

migrate.py 只依赖标准库（json/logging/typing），可直接 import，
按 plugin_local_testing 记忆无需走字符串 exec 方案。

运行：
    python3 tests/test_migrate.py
    python3 -m unittest tests.test_migrate -v
"""
import json
import os
import sys
import unittest

# 让脚本能找到 emotion_favour 包根目录（migrate.py 所在目录）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from migrate import migrate, CURRENT_CONFIG_VERSION, _MIGRATIONS


class TestMigrateV0ToV1(unittest.TestCase):
    """v0 → v1: advance_config 从 JSON 字符串迁移到 template_list (list)。"""

    def test_v0_json_string_to_list(self):
        """v0 老格式（JSON 字符串）应迁移到 v1 list，原字段保留 + __template_key=custom 注入"""
        cfg = {
            "relationship_config": {
                "mode": "advance",
                "advance_config": json.dumps([
                    {"describe": "失望", "min_value": 0, "max_value": 30},
                    {"describe": "友好", "min_value": 31, "max_value": 100, "rule": "..."},
                ], ensure_ascii=False),
            }
        }
        migrate(cfg)
        self.assertEqual(cfg["config_version"], 1)
        ac = cfg["relationship_config"]["advance_config"]
        self.assertIsInstance(ac, list)
        self.assertEqual(ac[0]["__template_key"], "custom")
        self.assertEqual(ac[0]["describe"], "失望")
        self.assertEqual(ac[1]["rule"], "...")

    def test_v1_already_migrated_noop(self):
        """已是 v1 配置应保持完全不变（字节级）"""
        cfg = {
            "config_version": 1,
            "relationship_config": {"mode": "advance", "advance_config": [{"describe": "X"}]},
        }
        before = json.dumps(cfg, sort_keys=True)
        migrate(cfg)
        self.assertEqual(before, json.dumps(cfg, sort_keys=True))

    def test_broken_json_does_not_bump(self):
        """JSON 损坏时不 bump 版本号——下次启动可重试"""
        cfg = {
            "config_version": 0,
            "relationship_config": {"mode": "advance", "advance_config": "{not valid"},
        }
        migrate(cfg)
        self.assertEqual(cfg.get("config_version"), 0)
        self.assertIsInstance(cfg["relationship_config"]["advance_config"], str)

    def test_missing_version_treated_as_v0(self):
        """缺 config_version 字段视为 v0"""
        cfg = {"relationship_config": {"mode": "advance", "advance_config": "[]"}}
        migrate(cfg)
        self.assertEqual(cfg["config_version"], 1)
        self.assertEqual(cfg["relationship_config"]["advance_config"], [])

    def test_non_dict_safe(self):
        """非 dict 输入应安全返回原值"""
        self.assertIsNone(migrate(None))
        self.assertEqual(migrate("str"), "str")
        self.assertEqual(migrate(123), 123)

    def test_already_list_does_not_force_template_key(self):
        """已是 list 不强制注入 __template_key，但版本号正常 bump"""
        cfg = {
            "config_version": 0,
            "relationship_config": {
                "mode": "advance",
                "advance_config": [{"describe": "喜欢", "min_value": 50}],
            },
        }
        migrate(cfg)
        self.assertEqual(cfg["config_version"], 1)
        self.assertNotIn("__template_key", cfg["relationship_config"]["advance_config"][0])

    def test_missing_relationship_config(self):
        """relationship_config 整节缺失视为符合新版本"""
        cfg = {"config_version": 0, "favour_mode": "normal"}
        migrate(cfg)
        self.assertEqual(cfg["config_version"], 1)


class TestMigrateFramework(unittest.TestCase):
    """迁移框架本身的健康度检查。"""

    def test_current_version_matches_registry(self):
        """CURRENT_CONFIG_VERSION 应等于 _MIGRATIONS 注册表的最大 key"""
        self.assertEqual(CURRENT_CONFIG_VERSION, max(_MIGRATIONS.keys()))

    def test_each_version_has_step(self):
        """v1 到 CURRENT_CONFIG_VERSION 之间每个版本都应有注册的迁移函数"""
        for v in range(1, CURRENT_CONFIG_VERSION + 1):
            self.assertIn(v, _MIGRATIONS, f"缺少 v{v} 的迁移函数")

    def test_idempotent_on_current_version(self):
        """已是 CURRENT_CONFIG_VERSION 时无变化"""
        cfg = {"config_version": CURRENT_CONFIG_VERSION, "anything": "preserved"}
        before = json.dumps(cfg, sort_keys=True)
        migrate(cfg)
        self.assertEqual(before, json.dumps(cfg, sort_keys=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
