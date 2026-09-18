"""domain.build_injection_prompt / build_system_prompt_extra 注入内容的静默失效点。

domain.py 只依赖标准库，直接按插件目录 import。只保留「改错了一眼看不出」的四类：
simple 模式不该带档位内容、满级不该再给解锁预告、有效好感度与 <情感/> 属性形式、
`<禁止>` 的禁令分层（thinking 泄漏是真实问题）。进度分档语义与条件组合属于
读代码/看输出即可确认，不再重复覆盖。

运行：
    python3 -m unittest tests.test_storage_injection -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import PLUGIN_DIR  # noqa: E402

sys.path.insert(0, str(PLUGIN_DIR))
from domain import (  # noqa: E402
    build_injection_prompt,
    build_system_prompt_extra,
)

class _Rec:
    """FavourRecord stub。build_injection_prompt 直接读 12 维属性构造 <情感 .../>，
    所以这里要把所有维度都列出来（默认 0）。"""
    favour = 0
    joy = 0
    trust = 0
    fear = 0
    surprise = 0
    sadness = 0
    disgust = 0
    anger = 0
    anticipation = 0
    pride = 0
    guilt = 0
    shame = 0
    envy = 0

class TestBuildInjectionPrompt(unittest.TestCase):
    def setUp(self):
        self.build = build_injection_prompt
        self.build_system = build_system_prompt_extra

    @staticmethod
    def _extras(**kw):
        defaults = {"boundary": "", "preview": "", "next_describe": "", "is_max_tier": False}
        defaults.update(kw)
        return defaults

    def test_preview_appears_only_when_progress_is_high(self):
        """非满级 + 进度 ≥80% + preview/next_describe 齐全时必须出现解锁预告。

        这一条盯的是唯一的进度门：把 `percentage >= 80` 放宽或收紧都不会报错，
        只会让角色该预告时沉默、或没到火候就剧透。
        """
        out = self.build(
            _Rec(), "喜欢", (61, 100), 95,
            tier_extras=self._extras(
                boundary="允许：牵手", preview="可流露深度亲密", next_describe="挚爱",
            ),
        )
        self.assertIn("<临近解锁>", out)
        self.assertIn("过渡到「挚爱」", out)
        self.assertIn("可流露深度亲密", out)

        # 同一构造下进度不足则必须沉默
        low = self.build(
            _Rec(), "喜欢", (61, 100), 70,
            tier_extras=self._extras(
                boundary="允许：牵手", preview="可流露深度亲密", next_describe="挚爱",
            ),
        )
        self.assertNotIn("<临近解锁>", low)

    def test_no_tier_extras_simple_mode(self):
        """simple 模式（无 tier_extras）不注入 <边界>/<临近解锁>"""
        out = self.build(_Rec(), "友好", (61, 100), 80)
        self.assertNotIn("<边界>", out)
        self.assertNotIn("<临近解锁>", out)

    def test_max_tier_marks_progress_and_blocks_preview(self):
        """is_max_tier=True 时进度行标「已达最高等级」，且 preview 条件齐备也不注入。

        favour=290 → 进度 93%（≥ 80%），boundary/preview/next_describe 都满足，
        负向断言才真正隔离 is_max_tier。
        """
        for favour in (200, 290):
            with self.subTest(favour=favour):
                out = self.build(
                    _Rec(), "挚爱", (151, 300), favour,
                    tier_extras=self._extras(
                        boundary="允许：深度亲密", preview="预告", next_describe="喜欢",
                        is_max_tier=True),
                )
                self.assertIn("已达最高等级", out)
                self.assertNotIn("<临近解锁>", out)

    def test_dynamic_ban_pointer_vs_static_full_bans(self):
        """动态标签只留就近指针；完整机制禁令（含 thinking 禁令）归静态 SystemPrompt。"""
        out = self.build(_Rec(), "友好", (61, 100), 80)
        self.assertIn("<禁止>", out)
        self.assertIn("隐藏情境提示", out)
        # 负向断言用禁令里真会出现的词：静态禁令若被复制回动态标签，这里会失败
        # （原「机制字眼」在两条文案里都不存在，永远通过）
        self.assertNotIn("解锁", out)
        self.assertNotIn("受等级限制", out)

        static = self.build_system()
        self.assertIn("好感度数值", static)
        self.assertIn("解锁", static)
        self.assertIn("受等级限制", static)
        self.assertIn("不得输出 thinking", static)

    def test_effective_favour_and_emotion_attribute_form(self):
        """<好感度> 显示 effective_favour（record.favour=0 却显示 80）；<情感> 用属性形式。"""
        out = self.build(_Rec(), "友好", (61, 100), 80)
        self.assertIn("<好感度>80</好感度>", out)
        self.assertIn("<情感 ", out)
        self.assertIn('喜悦="', out)
        self.assertIn('嫉妒="', out)

if __name__ == "__main__":
    unittest.main(verbosity=2)
