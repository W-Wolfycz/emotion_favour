"""`/emotion help` 的权限分支：普通成员不该看到管理员命令。

命令列表本身跑一次就能看见；但「普通成员多看到一段管理命令」只有在用非管理员
账号查看时才会暴露，属于静默泄漏，值得一条断言锁住。

运行：
    python3 -m unittest tests.test_help_menu -v
"""
import asyncio
import importlib.util
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
EventStub = _shared.EventStub
load_methods = _shared.load_methods
make_logger = _shared.make_logger


class HelpMenuTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.method = staticmethod(load_methods(
            ("help_menu",),
            namespace={
                "logger": make_logger("test_help_menu"),
                "AstrMessageEvent": object,
            },
        )["help_menu"])

    def _render(self, *, admin):
        class Stub:
            def _is_bot_admin(self, _event):
                return admin

            def _tag(self, _event):
                return "[EmotionFavour]"

            async def _render_t2i(self, _md_text):
                # 走纯文本回退分支，避免依赖出图
                raise RuntimeError("T2I 不可用")

        async def collect():
            return [item async for item in self.method(Stub(), EventStub())]

        return asyncio.run(collect())[0][1]

    def test_permission_controls_visible_commands(self):
        """安全边界：非管理员看帮助时不能出现管理命令（越权信息面）。
        管理员那一侧同时断言，避免用「一律不显示管理段」蒙对。"""
        admin_text = self._render(admin=True)
        self.assertIn("## Bot 管理员", admin_text)
        self.assertIn("/emotion regenerate", admin_text)

        member_text = self._render(admin=False)
        self.assertIn("## 个人查询", member_text)
        for hidden in ("Bot 管理员", "regenerate", "/emotion set", "/emotion clear"):
            self.assertNotIn(hidden, member_text)

if __name__ == "__main__":
    unittest.main()
