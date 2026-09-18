"""出图 HTML 的用户文本转义。

昵称、关系名来自用户，漏转义时出图照样成功，只有被注入才暴露，所以留一条盯住。
其余结构、类名、柱高属于「跑一次出图就看得见」，不在此处断言。

运行：
    python3 -m unittest tests.test_emotion_html -v
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import PLUGIN_DIR  # noqa: E402

sys.path.insert(0, str(PLUGIN_DIR))
from domain import build_list_html, build_report_html  # noqa: E402

EMPTY_RECORD = SimpleNamespace(**{dim: 0 for dim in (
    "joy", "trust", "fear", "surprise", "sadness", "disgust",
    "anger", "anticipation", "pride", "guilt", "shame", "envy",
)})

def record(**overrides):
    values = dict(
        joy=52, trust=30, fear=12, surprise=16, sadness=8, disgust=4,
        anger=2, anticipation=18, pride=28, guilt=6, shame=22, envy=5,
    )
    values.update(overrides)
    return SimpleNamespace(**values)

class OutputEscapingTest(unittest.TestCase):
    def test_user_controlled_text_is_escaped(self):
        """昵称/关系名来自用户；漏转义时出图仍然成功，只有被注入才暴露。"""
        report_html = build_report_html(
            record(), favour=440, max_favour=800, relationship='<b>喜欢</b>',
            who='甲 "乙" · 10001', percent=44.7, hint="距下一级还需 111 点",
        )
        self.assertNotIn("<b>喜欢</b>", report_html)
        self.assertIn("&lt;b&gt;喜欢&lt;/b&gt;", report_html)
        self.assertIn("&quot;乙&quot;", report_html)
        # 结构本身要保留成真 HTML：它经 marked 透传，不能被整体转义
        self.assertIn('<section class="ef-hero">', report_html)

        list_html = build_list_html([{
            "name": "<script>alert(1)</script>", "user_id": "10001",
            "favour": 440, "relationship": "喜欢", "record": record(),
        }])
        self.assertNotIn("<script>", list_html)
        self.assertIn("&lt;script&gt;", list_html)

if __name__ == "__main__":
    unittest.main()
