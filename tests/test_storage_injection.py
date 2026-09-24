"""domain.build_injection_prompt / build_system_prompt_extra 注入内容的静默失效点。

注入文本只进模型上下文，出错不抛异常、界面也不显示，只能靠断言盯住三类：
进度门（≥80% 才预告解锁）、满级不再给解锁预告、有效好感度与 <情感/> 属性形式与
`<禁止>` 的禁令分层（thinking 禁令若退回动态标签就是真实泄漏）。

运行：
    python3 -m unittest tests.test_storage_injection -v
"""
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

_load_shared()  # 只为把插件目录放进 sys.path，下面的 domain 直接 import

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
        """守唯一的进度门 `percentage >= 80`：放宽/收紧都不会报错，只会让角色
        该预告解锁时沉默、或没到火候就剧透后续关系。"""
        extras = self._extras(
            boundary="允许：牵手", preview="可流露深度亲密", next_describe="挚爱",
        )
        high = self.build(_Rec(), "喜欢", (61, 100), 95, tier_extras=extras)
        self.assertIn("<临近解锁>", high)
        self.assertIn("过渡到「挚爱」", high)
        self.assertIn("可流露深度亲密", high)

        # 同一构造下进度不足则必须沉默
        low = self.build(_Rec(), "喜欢", (61, 100), 70, tier_extras=extras)
        self.assertNotIn("<临近解锁>", low)

    def test_max_tier_marks_progress_and_blocks_preview(self):
        """守满级口径：is_max_tier=True 时进度行标「已达最高等级」且不再预告解锁。
        favour=290 → 进度 93%（≥80%），其余预告条件齐备，负向断言才真正隔离
        is_max_tier；换更低的 favour 会因为进度不足而假通过。"""
        out = self.build(
            _Rec(), "挚爱", (151, 300), 290,
            tier_extras=self._extras(
                boundary="允许：深度亲密", preview="预告", next_describe="喜欢",
                is_max_tier=True),
        )
        self.assertIn("已达最高等级", out)
        self.assertNotIn("<临近解锁>", out)

    def test_dynamic_ban_pointer_vs_static_full_bans(self):
        """守禁令分层：动态标签只留就近指针，完整机制禁令（含 thinking 禁令）归静态
        SystemPrompt。静态禁令被复制回动态标签不会报错，只会让模型更容易说出机制。"""
        out = self.build(_Rec(), "友好", (61, 100), 80)
        self.assertIn("<禁止>", out)
        self.assertIn("隐藏情境提示", out)
        # 负向断言用禁令里真会出现的词：静态禁令若被复制回动态标签，这里会失败
        self.assertNotIn("解锁", out)
        self.assertNotIn("受等级限制", out)

        static = self.build_system()
        self.assertIn("好感度数值", static)
        self.assertIn("解锁", static)
        self.assertIn("受等级限制", static)
        self.assertIn("不得输出 thinking", static)

    def test_effective_favour_and_emotion_attribute_form(self):
        """守注入数值与属性协议：<好感度> 必须是衰减后的 effective_favour
        （record.favour=0 却显示 80），<情感> 用属性形式——写错不报错，
        模型只会照着错的数值/形状表演。"""
        out = self.build(_Rec(), "友好", (61, 100), 80)
        self.assertIn("<好感度>80</好感度>", out)
        self.assertIn("<情感 ", out)
        self.assertIn('喜悦="', out)
        self.assertIn('嫉妒="', out)

if __name__ == "__main__":
    unittest.main(verbosity=2)
