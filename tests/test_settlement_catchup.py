"""emotion_favour domain.py 的 build_interaction_section（互动段落构造）测试。

domain.py 顶部 import 了 astrbot 等本地未装的库，
按 plugin_local_testing 记忆，把目标函数从源码里以字符串提取出来 exec 到
独立命名空间，绕过模块顶部 import。

运行：
    python3 tests/test_settlement_catchup.py
    python3 -m unittest tests.test_settlement_catchup -v
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DOMAIN_PY = os.path.join(HERE, "..", "domain.py")


def _extract_top_fn(src: str, fn_name: str) -> str:
    lines = src.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^def {fn_name}\(", line):
            start = i
            break
    if start is None:
        raise ValueError(f"未找到顶层函数 {fn_name}")
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^(def |class )", lines[j]):
            end = j
            break
    return "".join(lines[start:end])


def _load():
    prefix = "from __future__ import annotations\n\n"
    with open(DOMAIN_PY, encoding="utf-8") as f:
        src = f.read()
        match = re.search(r"^MAX_INTERACTION_CHARS = (\d+)$", src, re.MULTILINE)
        globals()["MAX_INTERACTION_CHARS"] = int(match.group(1))
        exec(prefix + _extract_top_fn(src, "build_interaction_section"), globals())


_load()


class TestBuildInteractionSection(unittest.TestCase):
    def test_legacy_format(self):
        section = build_interaction_section("你好", "你好呀")
        self.assertEqual(section, "【互动】\n用户: 你好\n角色: 你好呀\n\n")

    def test_none_inputs_do_not_crash(self):
        section = build_interaction_section(None, "")
        self.assertIn("用户: ", section)
        self.assertIn("角色: ", section)

    def test_long_text_is_clipped(self):
        long_text = "x" * 3000
        section = build_interaction_section(long_text, "短回复")
        self.assertIn("…(截断)", section)
        self.assertNotIn("x" * 2001, section)


if __name__ == "__main__":
    unittest.main()
