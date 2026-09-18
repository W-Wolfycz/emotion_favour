"""权限策略与「命令登记」不变量测试。

- 公开命令不走管理员判定、管理命令必须 Bot 管理员、特殊 ID 才拿特殊初始值；
- 两个命令集合与权限检查点都从 `main.py` 现取（不是测试自造副本），并断言每个
  会做权限检查的命令名都已登记：`_check_command_permission` 对未登记命令是
  fail-closed，漏登记的表现是「命令静默不可用」，只在真正用到时才发现。

运行：
    python3 -m unittest tests.test_permission_policy -v
"""
import asyncio
import re
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import extract_class_attribute, extract_method, read_main_source  # noqa: E402

CHECKED_COMMAND_PATTERN = re.compile(r'_check_command_permission\(event,\s*"([^"]+)"')


class _Event:
    def __init__(self, sender_id):
        self.sender_id = sender_id

    def get_sender_id(self):
        return self.sender_id


def _command_sets(src: str) -> tuple:
    def value(name):
        block = extract_class_attribute(src, name)
        return eval(block.split("=", 1)[1].strip(), {"frozenset": frozenset})

    return value("_PUBLIC_COMMANDS"), value("_BOT_ADMIN_COMMANDS")


def _checked_command_names(src: str) -> list:
    return sorted(set(CHECKED_COMMAND_PATTERN.findall(src)))


class TestPermissionPolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = read_main_source()
        cls.public, cls.admin = _command_sets(cls.src)
        prefix = "from __future__ import annotations\n"
        namespace = {}
        exec(prefix + extract_method(cls.src, "_check_command_permission"), namespace)
        exec(prefix + extract_method(cls.src, "_get_initial_favour"), namespace)
        # 提取出的是未绑定函数（首参 self），存成 staticmethod 免得再被绑一次
        cls.check = staticmethod(namespace["_check_command_permission"])
        cls.initial = staticmethod(namespace["_get_initial_favour"])

    def _plugin(self, *, is_admin):
        return types.SimpleNamespace(
            _PUBLIC_COMMANDS=self.public,
            _BOT_ADMIN_COMMANDS=self.admin,
            _is_bot_admin=lambda _event: is_admin,
            favour_envoys=frozenset({"10002"}),
            admin_default_favour=50,
            default_favour=0,
            min_favour_value=-100,
            max_favour_value=100,
        )

    def test_public_commands_are_available_to_everyone(self):
        plugin = self._plugin(is_admin=False)
        self.assertTrue(self.public, "公开命令集合不应为空")
        for name in sorted(self.public):
            with self.subTest(command=name):
                self.assertTrue(asyncio.run(self.check(plugin, _Event("10002"), name)))

    def test_management_commands_require_bot_admin(self):
        non_admin = self._plugin(is_admin=False)
        bot_admin = self._plugin(is_admin=True)
        self.assertTrue(self.admin, "管理命令集合不应为空")
        for name in sorted(self.admin):
            with self.subTest(command=name):
                self.assertFalse(asyncio.run(self.check(non_admin, _Event("10002"), name)))
                self.assertTrue(asyncio.run(self.check(bot_admin, _Event("10001"), name)))
        self.assertFalse(
            asyncio.run(self.check(non_admin, _Event("10002"), "not-registered")),
            "未登记的命令必须 fail-closed",
        )

    def test_checked_commands_are_registered(self):
        """漏登记 = 命令静默不可用：新增命令只在真正被调用时才发现没权限。"""
        checked = _checked_command_names(self.src)
        self.assertTrue(checked, "应从 main.py 提取到权限检查点")
        registered = set(self.public) | set(self.admin)
        self.assertEqual(sorted(set(checked) - registered), [])

    def test_explicit_special_ids_only_get_special_initial_favour(self):
        plugin = self._plugin(is_admin=False)
        self.assertEqual(asyncio.run(self.initial(plugin, _Event("10002"))), 50)
        self.assertEqual(asyncio.run(self.initial(plugin, _Event("10001"))), 0)


if __name__ == "__main__":
    unittest.main()
