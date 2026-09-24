"""权限策略与「命令登记」不变量测试。

- 管理命令必须 Bot 管理员（含未登记命令 fail-closed）；
- 特殊 ID 才拿特殊初始好感度；
- 命令集合与权限检查点都从 `main.py` 现取（不是测试自造副本），并断言每个会做
  权限检查的命令名都已登记：漏登记的表现是「命令静默不可用」。

运行：
    python3 -m unittest tests.test_permission_policy -v
"""
import asyncio
import importlib.util
import re
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
extract_class_attribute = _shared.extract_class_attribute
extract_method = _shared.extract_method
read_main_source = _shared.read_main_source

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

    def test_management_commands_require_bot_admin(self):
        """安全边界：管理命令对非管理员必须拒绝、对管理员必须放行，未登记命令
        fail-closed。任一方向判错都没有运行期报错，只会静默越权或静默失效。"""
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
        """守「新增命令带权限检查但漏登记」：fail-closed 会让该命令静默不可用，
        只有真被人用到时才暴露；检查点与登记表都从源码现取，不维护测试副本。"""
        checked = _checked_command_names(self.src)
        self.assertTrue(checked, "应从 main.py 提取到权限检查点")
        registered = set(self.public) | set(self.admin)
        self.assertEqual(sorted(set(checked) - registered), [])

    def test_explicit_special_ids_only_get_special_initial_favour(self):
        """守「初始好感度被静默改写」：显式特使拿 admin_default_favour，其他人拿
        default_favour；判错只会让新用户的起算值悄悄变成另一个数。"""
        plugin = self._plugin(is_admin=False)
        self.assertEqual(asyncio.run(self.initial(plugin, _Event("10002"))), 50)
        self.assertEqual(asyncio.run(self.initial(plugin, _Event("10001"))), 0)

if __name__ == "__main__":
    unittest.main()
