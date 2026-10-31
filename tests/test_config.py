"""配置读取里「改错了一眼看不出」的两处：备份保留期被上界钳制要等到需要恢复时
才发现备份没了；分组迁移判错会让旧配置静默丢失或被默认值盖掉。

运行：
    python3 -m unittest tests.test_config -v
"""
import importlib.util
import json
import os
import shutil
import tempfile
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
LOGGER = _shared.make_logger("test_config")

from config import (
    PluginSettings,
    load_schema_defaults,
    migrate_config_layout,
)
from domain import utc_now  # noqa: E402

PLUGIN_DIR = Path(__file__).resolve().parent.parent


class TestPluginSettings(unittest.TestCase):
    def test_backup_retention_accepts_large_values(self):
        """回归：保留期曾被上界钳制，用户填大值会被悄悄改小，过期备份被提前删除，
        且没有任何提示——只有在需要恢复时才发现备份没了。"""
        retention_days = 10 ** 100
        settings = PluginSettings.from_mapping({
            "runtime_config": {"backup_retention_days": retention_days},
        })
        self.assertEqual(settings.backup_retention_days, retention_days)


class _FakeConfig(dict):
    """AstrBotConfig 的最小替身：dict + default_config + 写回计数。

    `default_config` 刻意设成"全局配置"那种与插件无关的内容：真实 AstrBotConfig 在构造
    时先把该属性设成传入的 default_config（插件路径下就是全局配置），之后才用 schema
    解析结果覆盖局部变量，所以属性上**拿不到**本插件的默认值——照它写迁移会一条都搬不动。
    """

    def __init__(self, values, defaults=None, config_path=""):
        super().__init__(values)
        self.default_config = defaults if defaults is not None else {
            "platform_settings": {}, "provider_settings": {},
        }
        self.config_path = config_path
        self.saved = 0

    def save_config(self):
        self.saved += 1
        if self.config_path:
            Path(self.config_path).write_text(
                json.dumps(dict(self), ensure_ascii=False, indent=2), encoding="utf-8"
            )


class TestConfigLayoutMigration(unittest.TestCase):
    DEFAULTS = {
        "advanced_config": {"favour_change_max": 5, "tier_script_enabled": True,
                            "use_chat_memory": False},
        "settlement_config": {"favour_change_max": 5},
        "runtime_config": {"use_chat_memory": False},
        "tier_script_enabled": True,
    }

    def make_config(self, legacy, *, new_values=None):
        defaults = self.DEFAULTS
        values = {
            "advanced_config": dict(legacy),
            "settlement_config": {"favour_change_max": 5},
            "access_config": {},
            "runtime_config": {"use_chat_memory": False},
            "tier_script_enabled": True,
        }
        for group, items in (new_values or {}).items():
            values[group].update(items)
        # 刻意不把 defaults 塞进假对象：迁移必须靠显式传入的 defaults 工作
        return _FakeConfig(values)

    def test_moves_legacy_values_and_deletes_old_keys(self):
        """守迁移本体：旧值必须搬进新分组、旧键必须删掉。只搬不删会让旧值继续被兜底
        读到（用户在面板上改新键反而不生效）；删而不搬则直接丢配置。"""
        config = self.make_config(
            {"favour_change_max": 10, "use_chat_memory": True, "tier_script_enabled": False}
        )
        report = migrate_config_layout(config, defaults=self.DEFAULTS)
        self.assertCountEqual(report.moved, ["favour_change_max", "use_chat_memory", "tier_script_enabled"])
        self.assertEqual(config["settlement_config"]["favour_change_max"], 10)
        self.assertTrue(config["runtime_config"]["use_chat_memory"])
        self.assertFalse(config["tier_script_enabled"])
        self.assertNotIn("advanced_config", config)

    def test_missing_defaults_is_a_no_op(self):
        """守"拿不到默认值就不敢删"：默认值缺失时若照旧删键，旧值会在没有搬运的情况下
        被抹掉（真机上就是这么丢的）——此时应当原样返回、一个键都不动。"""
        config = self.make_config({"favour_change_max": 10})
        report = migrate_config_layout(config, defaults={})
        self.assertEqual(report.moved, ())
        self.assertFalse(report.changed)
        self.assertEqual(config["advanced_config"], {"favour_change_max": 10})

    def test_legacy_container_survives_when_it_holds_unknown_keys(self):
        """守"只动认识的键"：旧容器里出现未知键时保留容器，不能顺手把别人写的配置删掉。"""
        config = self.make_config({"favour_change_max": 10, "unknown_key": 1})
        migrate_config_layout(config, defaults=self.DEFAULTS)
        self.assertEqual(config["advanced_config"], {"unknown_key": 1})

if __name__ == "__main__":
    unittest.main()


class _StartupStub:
    """只提供 `_migrate_config_if_needed` 用到的 data_dir。"""

    def __init__(self, data_dir):
        self.data_dir = data_dir


class TestStartupConfigMigration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = _shared.load_methods(
            ("_migrate_config_if_needed",),
            namespace={
                "os": os,
                "shutil": shutil,
                "utc_now": utc_now,
                "Path": Path,
                "load_schema_defaults": load_schema_defaults,
                "migrate_config_layout": migrate_config_layout,
                "logger": LOGGER,
                "AstrBotConfig": object,
                "__file__": str(PLUGIN_DIR / "main.py"),
            },
            stub_cls=_StartupStub,
        )

    def test_reload_with_default_only_legacy_keys_does_not_write(self):
        """守"迁移后不再反复写盘"：第一次迁移之后 AstrBot 会按 schema 把旧键补回默认值，
        此时必须判定为无需写盘——否则每次启动都重写一遍配置文件，还会和面板保存互相打架。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data_dir = Path(tmp.name)
        config_path = data_dir / "emotion_favour_config.json"
        values = {
            "advanced_config": {"favour_change_max": 5, "use_chat_memory": False},
            "settlement_config": {"favour_change_max": 5},
            "runtime_config": {"use_chat_memory": False},
        }
        config_path.write_text(json.dumps(values), encoding="utf-8")
        config = _FakeConfig(json.loads(json.dumps(values)), None, str(config_path))
        _StartupStub(data_dir)._migrate_config_if_needed(config)
        self.assertEqual(config.saved, 0, "旧键全为默认值时不该写盘")
        self.assertEqual(list(data_dir.glob("config_backup_*.json")), [])
