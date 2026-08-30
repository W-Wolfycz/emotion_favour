import unittest

from config import PluginSettings, validate_advance_tiers


class TestPluginSettings(unittest.TestCase):
    def test_runtime_default_matches_schema(self):
        settings = PluginSettings.from_mapping({})
        self.assertEqual(settings.favour_mode, "normal")
        self.assertEqual(settings.backup_retention_days, 0)
        self.assertEqual(settings.judge_request_max_retries, 5)

    def test_judge_retries_clamped_to_valid_range(self):
        settings = PluginSettings.from_mapping({
            "advanced_config": {
                "judge_request_max_retries": 0,
            },
        })
        self.assertEqual(settings.judge_request_max_retries, 1)
        settings = PluginSettings.from_mapping({
            "advanced_config": {
                "judge_request_max_retries": 99,
            },
        })
        self.assertEqual(settings.judge_request_max_retries, 10)
        settings = PluginSettings.from_mapping({
            "advanced_config": {
                "judge_request_max_retries": "abc",
            },
        })
        self.assertEqual(settings.judge_request_max_retries, 5)

    def test_judge_retries_accept_positive_values(self):
        settings = PluginSettings.from_mapping({
            "advanced_config": {
                "judge_request_max_retries": 3,
            },
        })
        self.assertEqual(settings.judge_request_max_retries, 3)

    def test_log_with_bot_id_reads_top_level(self):
        self.assertFalse(PluginSettings.from_mapping({}).log_with_bot_id)
        settings = PluginSettings.from_mapping({"log_with_bot_id": True})
        self.assertTrue(settings.log_with_bot_id)
        # 旧 log_config 组不再被读取（一次性迁移已废弃，升级后需手动配置）
        settings = PluginSettings.from_mapping({
            "log_config": {"log_with_bot_id": True},
        })
        self.assertFalse(settings.log_with_bot_id)

    def test_ids_and_ranges_are_normalized(self):
        settings = PluginSettings.from_mapping({
            "min_favour_value": 100,
            "max_favour_value": -100,
            "advanced_config": {
                "favour_envoys": [10001, "10002"],
                "favour_change_min": 5,
                "favour_change_max": -5,
            },
        })
        self.assertEqual((settings.min_favour_value, settings.max_favour_value), (-100, 100))
        self.assertEqual(settings.favour_envoys, frozenset({"10001", "10002"}))
        self.assertEqual((settings.favour_change_min, settings.favour_change_max), (-5, 5))
        self.assertTrue(settings.warnings)

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
