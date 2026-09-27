"""配置读取里「改错了一眼看不出」的几处：重试次数钳制（0 会让上游不重试）、
备份保留期不被上界钳制（静默删用户备份）、档位重叠被拒（静默改变档位匹配），
以及 v3.6.0 分组重排后的新路径/旧路径读取。

运行：
    python3 -m unittest tests.test_config -v
"""
import ast
import importlib.util
import json
import os
import re
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
    schema_defaults,
    validate_advance_tiers,
)
from domain import utc_now  # noqa: E402

PLUGIN_DIR = Path(__file__).resolve().parent.parent


class TestPluginSettings(unittest.TestCase):
    def test_judge_retries_clamped_to_valid_range(self):
        """守重试次数的静默改写：区间内原样通过，越界与非法值收敛到 1 / 10 / 默认 5。
        0 或负值会让上游完全不重试（表现为偶发失败直接判失败），99 则长时间卡住。"""
        cases = {3: 3, 0: 1, 99: 10, "abc": 5}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                settings = PluginSettings.from_mapping({
                    "runtime_config": {"llm_request_max_retries": raw},
                })
                self.assertEqual(settings.llm_request_max_retries, expected)

    def test_backup_retention_accepts_large_values(self):
        """回归：保留期曾被上界钳制，用户填大值会被悄悄改小，过期备份被提前删除，
        且没有任何提示——只有在需要恢复时才发现备份没了。"""
        retention_days = 10 ** 100
        settings = PluginSettings.from_mapping({
            "runtime_config": {"backup_retention_days": retention_days},
        })
        self.assertEqual(settings.backup_retention_days, retention_days)

    def test_legacy_advanced_config_is_read_as_fallback(self):
        """守旧路径兜底：分组重排后，老部署的非默认值仍写在 advanced_config 里，
        兜底一旦失效，升级后所有设置会静默回退成默认（用户只看到"设置自己变回去了"）。
        锚点要连同好感范围一起给：它会被夹进 [min_favour_value, max_favour_value]。"""
        settings = PluginSettings.from_mapping({
            "min_favour_value": 0,
            "max_favour_value": 800,
            "advanced_config": {
                "favour_change_max": 10,
                "favour_decay_anchor": 600,
                "use_chat_memory": True,
                "emotion_decay_min_hours": 2.0,
            },
        })
        self.assertEqual(settings.favour_change_max, 10)
        self.assertEqual(settings.favour_decay_anchor, 600)
        self.assertTrue(settings.use_chat_memory)
        self.assertEqual(settings.emotion_decay_min_hours, 2.0)

    def test_upgrade_shape_keeps_legacy_values(self):
        """守升级当场的真实形态：AstrBot 会按 schema 递归给新分组补默认值，于是新键必然
        存在且等于默认值——这时若还"新路径优先"，老部署的取值会被默认值盖掉（用户只会
        发现设置全变回默认）。这里按真实 schema 补一遍默认值再读。"""
        schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
        zero_by_type = {
            "int": 0, "float": 0.0, "bool": False, "string": "", "text": "",
            "list": [], "object": {}, "template_list": [], "dict": {},
        }

        def fill(node, conf):
            filled = {}
            for key, item in node.items():
                if item["type"] == "object":
                    filled[key] = fill(item["items"], conf.get(key) or {})
                else:
                    filled[key] = conf.get(key, item.get("default", zero_by_type[item["type"]]))
            return filled

        settings = PluginSettings.from_mapping(fill(schema, {
            "min_favour_value": 0,
            "max_favour_value": 800,
            "advanced_config": {
                "favour_change_max": 10,
                "favour_decay_anchor": 600,
                "admin_default_favour": 600,
                "use_chat_memory": True,
                "emotion_decay_min_hours": 2.0,
            },
        }))
        self.assertEqual(settings.favour_change_max, 10)
        self.assertEqual(settings.favour_decay_anchor, 600)
        self.assertEqual(settings.admin_default_favour, 600)
        self.assertTrue(settings.use_chat_memory)
        self.assertEqual(settings.emotion_decay_min_hours, 2.0)

    def test_new_group_wins_over_legacy_value(self):
        """守优先级：新旧路径同时有值时必须以新分组为准，否则用户在面板上改完不生效。"""
        settings = PluginSettings.from_mapping({
            "settlement_config": {"favour_change_max": 8},
            "advanced_config": {"favour_change_max": 10},
        })
        self.assertEqual(settings.favour_change_max, 8)

    def test_every_moved_key_exists_in_its_new_group(self):
        """守搬组时漏键/打错键/默认值不一致：插件按新分组读取，而 AstrBot 会给新键补
        schema 默认值，所以兜底判定依赖「代码默认值 == schema 默认值」——两边一旦不一致，
        旧配置的取值就不再被认作"非默认"，老部署的设置会静默回退。逐条比对调用与 schema。"""
        source = (PLUGIN_DIR / "config.py").read_text(encoding="utf-8")
        schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
        calls = re.findall(
            r'moved(?:_bool)?\(\s*(\w+),\s*"([^"]+)"(?:,\s*([^)]+))?\)', source
        )
        self.assertTrue(calls, "没找到任何 moved(...) 调用，正则或实现已变")
        groups = {
            "settlement": "settlement_config",
            "decay": "settlement_config",   # v3.6.0 起衰减并入结算组（别名读取）
            "access": "access_config",
            "runtime": "runtime_config",
            "rel_conf": "__top__",
            "config": "__top__",
        }
        for variable, key, default_literal in calls:
            self.assertIn(variable, groups, f"未知分组变量 {variable}")
            group = groups[variable]
            if group == "__top__":
                self.assertIn(key, schema, f"{key} 不在顶层配置里")
                self.assertNotEqual(schema[key].get("type"), "object", f"{key} 不是顶层标量项")
            else:
                self.assertIn(key, schema[group]["items"], f"{key} 不在 {group} 里")
            self.assertTrue(default_literal, f"{key} 没把默认值传给 moved()，兜底判定会失效")
            node = schema[key] if group == "__top__" else schema[group]["items"][key]
            self.assertEqual(
                ast.literal_eval(default_literal.strip()),
                node.get("default"),
                f"{key} 的代码默认值与 schema 默认值不一致",
            )


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

    def test_is_idempotent(self):
        """守幂等：第二次启动不该再搬一次，也不该写回第二次（否则每次加载都改配置文件）。"""
        config = self.make_config({"favour_change_max": 10})
        first = migrate_config_layout(config, defaults=self.DEFAULTS)
        self.assertEqual(first.moved, ("favour_change_max",))
        self.assertTrue(first.changed)
        second = migrate_config_layout(config, defaults=self.DEFAULTS)
        self.assertEqual(second.moved, ())
        self.assertFalse(second.changed, "第二次启动不该再写盘")
        self.assertNotIn("advanced_config", config)

    def test_does_not_clobber_values_already_set_in_new_group(self):
        """守优先级：用户已经在新分组里填过（值不等于默认值）时，旧键不许覆盖它，
        但旧键仍要删掉——否则这份旧值会在以后某次判定里冒出来。"""
        config = self.make_config(
            {"favour_change_max": 10}, new_values={"settlement_config": {"favour_change_max": 8}}
        )
        report = migrate_config_layout(config, defaults=self.DEFAULTS)
        self.assertEqual(report.moved, ())
        self.assertTrue(report.changed, "旧值虽被新值盖住，仍要清理并写盘")
        self.assertEqual(config["settlement_config"]["favour_change_max"], 8)
        self.assertNotIn("advanced_config", config)

    def test_missing_defaults_is_a_no_op(self):
        """守"拿不到默认值就不敢删"：默认值缺失时若照旧删键，旧值会在没有搬运的情况下
        被抹掉（真机上就是这么丢的）——此时应当原样返回、一个键都不动。"""
        config = self.make_config({"favour_change_max": 10})
        report = migrate_config_layout(config, defaults={})
        self.assertEqual(report.moved, ())
        self.assertFalse(report.changed)
        self.assertEqual(config["advanced_config"], {"favour_change_max": 10})

    def test_schema_defaults_covers_nested_groups(self):
        """守默认值解析：object 必须递归，缺 default 的项按类型零值——两者错了都会让
        "新值是否等于默认值"判错，于是该搬的不搬、或把用户填的值当成没填过。"""
        parsed = schema_defaults({
            "flag": {"type": "bool"},
            "num": {"type": "int", "default": 7},
            "group": {"type": "object", "items": {"inner": {"type": "string", "default": "x"}}},
        })
        self.assertEqual(parsed, {"flag": False, "num": 7, "group": {"inner": "x"}})

    def test_load_schema_defaults_reads_the_real_schema(self):
        """守启动路径实际用的那份 schema：读不到或解析错就退回"什么都不做"，
        所以这里必须确认它能从仓库的 _conf_schema.json 读出新分组的默认值。"""
        defaults = load_schema_defaults(PLUGIN_DIR / "_conf_schema.json")
        self.assertEqual(defaults["settlement_config"]["favour_change_max"], 5)
        self.assertIn("access_config", defaults)
        self.assertEqual(load_schema_defaults(PLUGIN_DIR / "no_such_file.json"), {})

    def test_legacy_container_survives_when_it_holds_unknown_keys(self):
        """守"只动认识的键"：旧容器里出现未知键时保留容器，不能顺手把别人写的配置删掉。"""
        config = self.make_config({"favour_change_max": 10, "unknown_key": 1})
        migrate_config_layout(config, defaults=self.DEFAULTS)
        self.assertEqual(config["advanced_config"], {"unknown_key": 1})


class TestAdvanceTierValidation(unittest.TestCase):
    def test_sorts_and_rejects_overlap(self):
        """守档位校验的静默改变匹配：区间必须先排序再判重叠，重叠项被剔除并告警。
        排序错乱会让 _find_tier 命中错误档位，重叠未拒则两条档位都能匹配。"""
        items, warnings = validate_advance_tiers([
            {"describe": "高", "min_value": 31, "max_value": 60},
            {"describe": "低", "min_value": 0, "max_value": 30},
            {"describe": "重叠", "min_value": 30, "max_value": 40},
        ])
        self.assertEqual([item["describe"] for item in items], ["低", "高"])
        self.assertEqual(len(warnings), 1)

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

    def test_startup_migration_backs_up_saves_and_drops_old_keys(self):
        """守启动迁移的接线：必须"先备份原文，再写回"，且写回后文件里不再有旧键。
        备份缺失时写坏了没退路；写回缺失则旧键留在文件里、兜底判定会继续压住新值。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data_dir = Path(tmp.name)
        config_path = data_dir / "emotion_favour_config.json"
        original = {
            "advanced_config": {"favour_change_max": 10, "use_chat_memory": True},
            "settlement_config": {"favour_change_max": 5},
            "runtime_config": {"use_chat_memory": False},
        }
        config_path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
        config = _FakeConfig(json.loads(json.dumps(original)), None, str(config_path))

        _StartupStub(data_dir)._migrate_config_if_needed(config)

        self.assertEqual(config.saved, 1, "必须写回一次")
        saved = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertNotIn("advanced_config", saved, "旧键必须从文件里删除")
        self.assertEqual(saved["settlement_config"]["favour_change_max"], 10)
        self.assertTrue(saved["runtime_config"]["use_chat_memory"])

        backups = sorted(data_dir.glob("config_backup_*.json"))
        self.assertEqual(len(backups), 1, "必须留下升级前的备份")
        self.assertEqual(
            json.loads(backups[0].read_text(encoding="utf-8")), original,
            "备份必须是升级前那份原文，否则出问题没法回退",
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

    def test_nothing_to_migrate_means_no_write(self):
        """守"无迁移不写盘"：配置本来就干净时不该动文件（否则每次启动都改一次配置）。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data_dir = Path(tmp.name)
        config_path = data_dir / "emotion_favour_config.json"
        clean = {"settlement_config": {"favour_change_max": 5}}
        config_path.write_text(json.dumps(clean), encoding="utf-8")
        config = _FakeConfig(json.loads(json.dumps(clean)), None, str(config_path))
        _StartupStub(data_dir)._migrate_config_if_needed(config)
        self.assertEqual(config.saved, 0)
        self.assertEqual(list(data_dir.glob("config_backup_*.json")), [])
