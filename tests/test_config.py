import unittest

from config import PluginSettings, validate_advance_tiers


class TestPluginSettings(unittest.TestCase):
    def test_runtime_default_matches_schema(self):
        settings = PluginSettings.from_mapping({})
        self.assertEqual(settings.favour_mode, "normal")
        self.assertEqual(settings.settlement_max_concurrency, 3)

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
