"""固定权限策略与特殊用户初始值的纯 Python 回归测试。"""

import asyncio
import re
import textwrap
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "main.py"


def _extract_method(name: str) -> str:
    lines = MAIN.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if re.match(rf"^    (?:async )?def {name}\(", line))
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if re.match(r"^    (?:async )?def ", lines[i]):
            end = i
            break
    return "from __future__ import annotations\n\n" + textwrap.dedent("".join(lines[start:end]))


class _Event:
    def __init__(self, sender_id: str):
        self.sender_id = sender_id

    def get_sender_id(self):
        return self.sender_id


class TestPermissionPolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.permission_ns = {}
        exec(_extract_method("_check_command_permission"), cls.permission_ns)
        cls.initial_ns = {}
        exec(_extract_method("_get_initial_favour"), cls.initial_ns)

    def test_public_commands_are_available_to_everyone(self):
        method = self.permission_ns["_check_command_permission"]
        plugin = types.SimpleNamespace(
            _PUBLIC_COMMANDS=frozenset({"me", "help"}),
            _BOT_ADMIN_COMMANDS=frozenset({"set"}),
            _is_bot_admin=lambda event: False,
        )
        self.assertTrue(asyncio.run(method(plugin, _Event("10002"), "me")))
        self.assertTrue(asyncio.run(method(plugin, _Event("10002"), "help")))

    def test_management_commands_require_bot_admin(self):
        method = self.permission_ns["_check_command_permission"]
        non_admin = types.SimpleNamespace(
            _PUBLIC_COMMANDS=frozenset({"me", "help"}),
            _BOT_ADMIN_COMMANDS=frozenset({"set", "clear-all"}),
            _is_bot_admin=lambda event: False,
        )
        bot_admin = types.SimpleNamespace(
            _PUBLIC_COMMANDS=frozenset({"me", "help"}),
            _BOT_ADMIN_COMMANDS=frozenset({"set", "clear-all"}),
            _is_bot_admin=lambda event: True,
        )
        self.assertFalse(asyncio.run(method(non_admin, _Event("10002"), "set")))
        self.assertFalse(asyncio.run(method(non_admin, _Event("10002"), "clear-all")))
        self.assertTrue(asyncio.run(method(bot_admin, _Event("10001"), "set")))

    def test_explicit_special_ids_only_get_special_initial_favour(self):
        method = self.initial_ns["_get_initial_favour"]
        plugin = types.SimpleNamespace(
            favour_envoys=frozenset({"10002"}),
            admin_default_favour=50,
            default_favour=0,
            min_favour_value=-100,
            max_favour_value=100,
        )
        self.assertEqual(asyncio.run(method(plugin, _Event("10002"))), 50)
        self.assertEqual(asyncio.run(method(plugin, _Event("10001"))), 0)


if __name__ == "__main__":
    unittest.main()
