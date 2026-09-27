"""用户侧非文本消息的占位映射（utils.placeholder_for_message）。

守的静默失效：用户只发图片/表情/语音时 `message_str` 为空，补不出占位就
**整轮不会进入结算**——少加一轮好感度完全看不出来，也不会报任何错。

运行：
    python3 -m unittest tests.test_message_placeholder -v
"""
import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _shared import PLUGIN_DIR  # noqa: E402


def _load_symbols(names: set) -> dict:
    """按需抽取 utils.py 里的顶层常量与函数（该模块顶部 import 了 astrbot，不能整份导入）。"""
    source = (PLUGIN_DIR / "utils.py").read_text(encoding="utf-8")
    picked = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            picked.append(node)
        elif isinstance(node, ast.Assign):
            targets = {getattr(t, "id", "") for t in node.targets}
            if targets & names:
                picked.append(node)
    scope: dict = {}
    exec(compile(ast.Module(body=picked, type_ignores=[]), "utils_subset", "exec"), scope)
    return scope


_symbols = _load_symbols({"MEDIA_PLACEHOLDERS", "placeholder_for_message"})
placeholder_for_message = _symbols["placeholder_for_message"]


def _comp(class_name: str):
    """造一个类名可控的假组件（占位映射按组件类名分发）。"""
    return type(class_name, (), {})()


class PlaceholderTest(unittest.TestCase):
    def test_non_text_components_map_to_placeholders(self):
        cases = {
            ("Image",): "[图片]",
            ("Face",): "[表情]",
            ("Record",): "[语音]",
            ("Video",): "[视频]",
            ("Poke",): "[戳一戳]",
            ("Forward",): "[转发消息]",
            ("Image", "Image"): "[图片][图片]",
            ("Unknown",): "[非文本消息]",
            (): "[非文本消息]",
        }
        for class_names, expected in cases.items():
            with self.subTest(class_names=class_names):
                chain = [_comp(name) for name in class_names]
                self.assertEqual(placeholder_for_message(chain), expected)


if __name__ == "__main__":
    unittest.main()
