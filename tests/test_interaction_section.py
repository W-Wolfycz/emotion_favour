"""结算互动段落的角色对应：用户的话不能被算到角色头上。

运行：
    python3 -m unittest tests.test_interaction_section -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import PLUGIN_DIR, load_module_constant, load_top_function  # noqa: E402

build_interaction_section = load_top_function(
    PLUGIN_DIR / "domain.py",
    "build_interaction_section",
    namespace={
        "MAX_INTERACTION_CHARS": load_module_constant(
            PLUGIN_DIR / "domain.py", "MAX_INTERACTION_CHARS"
        )
    },
)

class TestBuildInteractionSection(unittest.TestCase):
    def test_user_and_bot_lines_keep_their_roles(self):
        section = build_interaction_section("你好", "你好呀")
        self.assertEqual(section, "【互动】\n用户: 你好\n角色: 你好呀\n\n")

if __name__ == "__main__":
    unittest.main()
