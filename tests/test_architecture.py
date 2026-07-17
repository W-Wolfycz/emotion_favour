"""3.4.0 架构边界与 AstrBot 集成约束的静态回归测试。"""

import ast
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TestAstrBotLifecycleShape(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "main.py").read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)
        cls.plugin = next(
            node for node in cls.tree.body
            if isinstance(node, ast.ClassDef) and node.name == "EmotionFavourPlugin"
        )

    def test_terminate_is_defined_on_final_plugin_class(self):
        methods = {
            node.name for node in self.plugin.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertIn("initialize", methods)
        self.assertIn("terminate", methods)

    def test_settlement_uses_after_message_sent(self):
        method = next(
            node for node in self.plugin.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "evaluate_favour"
        )
        decorators = [ast.unparse(item) for item in method.decorator_list]
        self.assertTrue(any("filter.after_message_sent" in item for item in decorators))
        self.assertFalse(any("filter.on_decorating_result" in item for item in decorators))

    def test_initialize_failure_is_propagated(self):
        method = next(
            node for node in self.plugin.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "initialize"
        )
        self.assertTrue(any(isinstance(node, ast.Raise) for node in ast.walk(method)))

    def test_chat_memory_history_is_persona_isolated(self):
        history_method = next(
            node for node in self.plugin.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_get_recent_history"
        )
        argument_names = [arg.arg for arg in history_method.args.args]
        self.assertIn("persona_id", argument_names)

        query_call = next(
            node for node in ast.walk(history_method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "query_rounds"
        )
        keyword_names = {item.arg for item in query_call.keywords}
        self.assertIn("llm_status", keyword_names)
        self.assertIn("persona_id", keyword_names)

        settlement_method = next(
            node for node in self.plugin.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_calculate_favour_bg"
        )
        history_call = next(
            node for node in ast.walk(settlement_method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_get_recent_history"
        )
        self.assertIn("persona_id", {item.arg for item in history_call.keywords})


class TestModuleBoundaries(unittest.TestCase):
    def test_domain_has_no_astrbot_or_database_imports(self):
        tree = ast.parse((ROOT / "domain.py").read_text(encoding="utf-8"))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        self.assertFalse(any(name.startswith("astrbot") for name in imported))
        self.assertFalse(any(name.startswith(("sqlmodel", "sqlalchemy", "aiosqlite")) for name in imported))

    def test_persuasion_and_roster_runtime_are_removed(self):
        combined = "\n".join(
            (ROOT / name).read_text(encoding="utf-8")
            for name in ("main.py", "storage.py", "config.py", "requirements.txt")
        ).casefold()
        self.assertNotIn("persuasion", combined)
        self.assertNotIn("roster", combined)
        self.assertNotIn("rapidfuzz", combined)
        self.assertFalse((ROOT / "roster_matcher.py").exists())


class TestWebUiBridge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "pages" / "webui" / "app.js").read_text(encoding="utf-8")
        cls.html = (ROOT / "pages" / "webui" / "index.html").read_text(encoding="utf-8")

    def test_bridge_only_no_manual_asset_token_or_fetch(self):
        self.assertIn("AstrBotPluginPage", self.source)
        self.assertNotIn("asset_token", self.source)
        self.assertNotIn("fetch(", self.source)

    def test_persona_switchers_are_searchable_and_validated(self):
        self.assertIn('id="persona-select" type="search"', self.html)
        self.assertIn('id="create-persona" type="search"', self.html)
        self.assertIn("state.personas.includes(personaId)", self.source)

    def test_record_management_controls_are_present(self):
        for element_id in (
            "user-search",
            "page-size",
            "page-jump",
            "edit-zero-emotions",
            "edit-restore",
            "edit-stored-favour",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn("beforeunload", self.source)
        self.assertIn("isEditDirty", self.source)


class TestWebApiDto(unittest.TestCase):
    def test_record_dto_contains_persona_id(self):
        source = (ROOT / "web_api.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_record_to_dict"
        )
        literals = {
            node.value for node in ast.walk(function)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        self.assertIn("persona_id", literals)


class TestSchemaDefaults(unittest.TestCase):
    def test_new_runtime_defaults_match_settings(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        advanced = schema["advanced_config"]["items"]
        self.assertEqual(advanced["settlement_max_concurrency"]["default"], 3)
        self.assertEqual(advanced["settlement_timeout_seconds"]["default"], 60)
        self.assertNotIn("persona_summary_timeout_seconds", advanced)
        self.assertEqual(advanced["terminate_flush_timeout_seconds"]["default"], 8)
        self.assertNotIn("persuasion_config", schema)
        advanced_fields = schema["advanced_config"]["items"]
        for removed in (
            "level_threshold",
            "member_commands",
            "high_commands",
            "admin_commands",
            "owner_commands",
            "superuser_commands",
            "query_others_favour_level",
        ):
            self.assertNotIn(removed, advanced_fields)
        self.assertFalse((ROOT / "permissions.py").exists())


if __name__ == "__main__":
    unittest.main()
