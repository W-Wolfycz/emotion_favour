"""`/emotion help` 的权限分支：普通成员不该看到管理员命令。

命令列表本身跑一次就能看见；但「普通成员多看到一段管理命令」只有在用非管理员
账号查看时才会暴露，属于静默泄漏，值得一条断言锁住。

运行：
    python3 -m unittest tests.test_help_menu -v
"""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import EventStub, load_methods, make_logger  # noqa: E402

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
        admin_text = self._render(admin=True)
        self.assertIn("## Bot 管理员", admin_text)
        self.assertIn("/emotion regenerate", admin_text)

        member_text = self._render(admin=False)
        self.assertIn("## 个人查询", member_text)
        for hidden in ("Bot 管理员", "regenerate", "/emotion set", "/emotion clear"):
            self.assertNotIn(hidden, member_text)

if __name__ == "__main__":
    unittest.main()
