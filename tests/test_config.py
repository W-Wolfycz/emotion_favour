"""配置读取里「改错了一眼看不出」的三处：重试次数钳制（0 会让上游不重试）、
备份保留期不被上界钳制（静默删用户备份）、档位重叠被拒（静默改变档位匹配）。

运行：
    python3 -m unittest tests.test_config -v
"""
import unittest

from config import PluginSettings, validate_advance_tiers

class TestPluginSettings(unittest.TestCase):
    def test_judge_retries_clamped_to_valid_range(self):
        """区间内原样通过，越界与非法值收敛到 1 / 10 / 默认 5（0 会让上游不重试）。"""
        cases = {3: 3, 0: 1, 99: 10, "abc": 5}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                settings = PluginSettings.from_mapping({
                    "advanced_config": {"llm_request_max_retries": raw},
                })
                self.assertEqual(settings.llm_request_max_retries, expected)

    def test_backup_retention_accepts_large_values(self):
        retention_days = 10 ** 100
        settings = PluginSettings.from_mapping({
            "advanced_config": {"backup_retention_days": retention_days},
        })
        self.assertEqual(settings.backup_retention_days, retention_days)

class TestAdvanceTierValidation(unittest.TestCase):
    def test_sorts_and_rejects_overlap(self):
        items, warnings = validate_advance_tiers([
            {"describe": "高", "min_value": 31, "max_value": 60},
            {"describe": "低", "min_value": 0, "max_value": 30},
            {"describe": "重叠", "min_value": 30, "max_value": 40},
        ])
        self.assertEqual([item["describe"] for item in items], ["低", "高"])
        self.assertEqual(len(warnings), 1)

if __name__ == "__main__":
    unittest.main()
