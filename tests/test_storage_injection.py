"""emotion_favour domain.py 的 build_injection_prompt / compute_relationship_progress 测试。

storage.py 依赖 sqlmodel/sqlalchemy/aiofiles 等本地未装的库，
按 plugin_local_testing 记忆，把目标函数从源文件里以字符串提取出来 exec 到
独立命名空间，绕过模块顶部 import。依赖函数（build_emotion_panel /
build_tone_instruction）用 stub 替换。

运行：
    python3 tests/test_storage_injection.py
    python3 -m unittest tests.test_storage_injection -v
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DOMAIN_PY = os.path.join(HERE, "..", "domain.py")


def _extract_fn(src: str, fn_name: str) -> str:
    """从 domain.py 源码里提取指定顶层函数的完整源代码字符串。
    扫描从 `def fn_name(` 起到下一个顶层 def（行首无缩进的 def）或 EOF。"""
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
        if re.match(r"^def ", lines[j]):
            end = j
            break
    return "".join(lines[start:end])


def _load_fns():
    """提取 build_injection_prompt + compute_relationship_progress，
    stub 掉它俩调用的依赖函数，返回 (build_injection_prompt, compute_relationship_progress)。"""
    with open(DOMAIN_PY, encoding="utf-8") as f:
        src = f.read()
    # build_injection_prompt XML 版直接读 EMOTION_DIMENSIONS / EMOTION_DISPLAY_NAMES 构造属性形式，
    # 不再走 build_emotion_panel；这里把两常量 stub 出来（顺序与维度名按真实定义）。
    EMOTION_DIMENSIONS = (
        "joy", "trust", "fear", "surprise", "sadness",
        "disgust", "anger", "anticipation", "pride", "guilt", "shame", "envy",
    )
    EMOTION_DISPLAY_NAMES = {
        "joy": "喜悦", "trust": "信任", "fear": "恐惧", "surprise": "惊讶",
        "sadness": "悲伤", "disgust": "厌恶", "anger": "愤怒", "anticipation": "期待",
        "pride": "得意", "guilt": "内疚", "shame": "害羞", "envy": "嫉妒",
    }
    ns = {
        "EMOTION_DIMENSIONS": EMOTION_DIMENSIONS,
        "EMOTION_DISPLAY_NAMES": EMOTION_DISPLAY_NAMES,
        # stub 依赖函数（build_injection_prompt 内部调用 build_tone_instruction；
        # build_emotion_panel 已被 XML 属性构造取代，但留 stub 防御未来回退）
        "build_emotion_panel": lambda rec: "[喜悦:5]",
        "build_tone_instruction": lambda rec: "略带撒娇",
    }
    # from __future__ import annotations 让函数签名里的 Optional[...] / Tuple[...] 等
    # 类型注解延迟为字符串不求值，避免需要 stub 真实 typing 对象
    prefix = "from __future__ import annotations\n\n"
    exec(prefix + _extract_fn(src, "compute_relationship_progress"), ns)
    exec(prefix + _extract_fn(src, "build_system_prompt_extra"), ns)
    exec(prefix + _extract_fn(src, "build_injection_prompt"), ns)
    return (
        ns["build_injection_prompt"],
        ns["compute_relationship_progress"],
        ns["build_system_prompt_extra"],
    )


class _Rec:
    """FavourRecord stub。XML 版 build_injection_prompt 直接读 12 维属性构造 <情感 .../>，
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


# ======================= compute_relationship_progress =======================


class TestComputeProgress(unittest.TestCase):
    def setUp(self):
        _, self.fn, _ = _load_fns()

    def test_below_20_pct(self):
        pct, sem = self.fn(63, 61, 100)
        self.assertLess(pct, 20)
        self.assertEqual(sem, "刚进入此关系阶段")

    def test_mid_range_stable(self):
        pct, sem = self.fn(80, 61, 100)
        self.assertGreaterEqual(pct, 20)
        self.assertLess(pct, 80)
        self.assertEqual(sem, "稳定在此关系阶段")

    def test_above_80_below_95_approaching(self):
        pct, sem = self.fn(93, 61, 100)
        self.assertGreaterEqual(pct, 80)
        self.assertLess(pct, 95)
        self.assertEqual(sem, "接近下一阶段")

    def test_peak_95_plus(self):
        pct, sem = self.fn(99, 61, 100)
        self.assertGreaterEqual(pct, 95)
        self.assertEqual(sem, "处于此关系巅峰")

    def test_degenerate_range(self):
        """max<=min 时返回 (100, 巅峰)"""
        pct, sem = self.fn(50, 50, 50)
        self.assertEqual((pct, sem), (100, "处于此关系巅峰"))

    def test_clamp_overflow(self):
        """favour 超过 max 应 clamp 到 100"""
        pct, _ = self.fn(999, 61, 100)
        self.assertEqual(pct, 100)

    def test_clamp_underflow(self):
        """favour 低于 min 应 clamp 到 0"""
        pct, _ = self.fn(-999, 61, 100)
        self.assertEqual(pct, 0)


# ======================= build_injection_prompt =======================


class TestBuildInjectionPrompt(unittest.TestCase):
    def setUp(self):
        self.build, _, self.build_system = _load_fns()

    @staticmethod
    def _extras(**kw):
        defaults = {"boundary": "", "preview": "", "next_describe": "", "is_max_tier": False}
        defaults.update(kw)
        return defaults

    def test_no_tier_extras_simple_mode(self):
        """simple 模式（无 tier_extras）不注入 <边界>/<临近解锁>"""
        out = self.build(_Rec(), "友好", (61, 100), 80)
        self.assertNotIn("<边界>", out)
        self.assertNotIn("<临近解锁>", out)

    def test_boundary_injected(self):
        out = self.build(_Rec(), "友好", (61, 100), 80, tier_extras=self._extras(boundary="允许：A"))
        self.assertIn("<边界>允许：A</边界>", out)

    def test_boundary_empty_skips_line(self):
        """boundary 字段空时无 <边界> 标签"""
        out = self.build(_Rec(), "友好", (61, 100), 80, tier_extras=self._extras(boundary=""))
        self.assertNotIn("<边界>", out)

    def test_preview_below_80_not_triggered(self):
        """进度 < 80% 不触发 preview（favour=92 → pct=79%）"""
        out = self.build(_Rec(), "友好", (61, 100), 92, tier_extras=self._extras(
            boundary="...", preview="预告内容", next_describe="喜欢"))
        self.assertNotIn("<临近解锁>", out)

    def test_preview_at_80_plus_triggered(self):
        """进度 ≥ 80% 触发 preview，且包含下一级名称（favour=93 → pct=82%）"""
        out = self.build(_Rec(), "友好", (61, 100), 93, tier_extras=self._extras(
            boundary="...", preview="预告内容", next_describe="喜欢"))
        self.assertIn("<临近解锁>", out)
        self.assertIn("过渡到「喜欢」", out)
        self.assertIn("预告内容", out)

    def test_preview_empty_skips_even_if_high_progress(self):
        """preview 字段空时即使高进度也不注入"""
        out = self.build(_Rec(), "友好", (61, 100), 95, tier_extras=self._extras(
            boundary="...", preview="", next_describe="喜欢"))
        self.assertNotIn("<临近解锁>", out)

    def test_preview_with_empty_next_describe_skips(self):
        """next_describe 空时也跳过（防御）"""
        out = self.build(_Rec(), "友好", (61, 100), 95, tier_extras=self._extras(
            boundary="...", preview="...", next_describe=""))
        self.assertNotIn("<临近解锁>", out)

    def test_max_tier_marks_in_progress(self):
        """最高等级时 <进度> 附加「已达最高等级」"""
        out = self.build(_Rec(), "挚爱", (151, 300), 200, tier_extras=self._extras(
            boundary="允许：深度亲密", is_max_tier=True))
        self.assertIn("已达最高等级", out)
        # 最高等级不触发 preview
        self.assertNotIn("<临近解锁>", out)

    def test_max_tier_with_preview_field_still_skips(self):
        """最高等级即使 preview 字段非空也不触发（is_max_tier 优先）"""
        out = self.build(_Rec(), "挚爱", (151, 300), 290, tier_extras=self._extras(
            boundary="...", preview="预告", next_describe="", is_max_tier=True))
        self.assertNotIn("<临近解锁>", out)

    def test_dynamic_ban_is_compact_pointer(self):
        """动态标签仅保留就近指针，完整机制禁令归到 SystemPrompt。"""
        out = self.build(_Rec(), "友好", (61, 100), 80)
        self.assertIn("<禁止>", out)
        self.assertIn("隐藏情境提示", out)
        self.assertNotIn("机制字眼", out)

    def test_static_system_prompt_contains_full_bans(self):
        out = self.build_system()
        self.assertIn("好感度数值", out)
        self.assertIn("解锁", out)
        self.assertIn("受等级限制", out)
        self.assertIn("不得输出 thinking", out)

    def test_xml_tag_well_formed(self):
        """注入 prompt 应有完整的 <情感好感>...</情感好感> 外层标签"""
        out = self.build(_Rec(), "友好", (61, 100), 80)
        self.assertTrue(out.startswith("<情感好感>"))
        self.assertTrue(out.rstrip().endswith("</情感好感>"))

    def test_xml_nested_state_block(self):
        """<状态> 块应包含 <好感度> / <关系> / <情感 .../> 三个子元素"""
        out = self.build(_Rec(), "友好", (61, 100), 80)
        self.assertIn("<状态>", out)
        self.assertIn("</状态>", out)
        self.assertIn("<好感度>80</好感度>", out)
        self.assertIn("<关系>友好</关系>", out)
        # 情感用属性形式
        self.assertIn("<情感 ", out)
        self.assertIn('喜悦="', out)
        self.assertIn('嫉妒="', out)

    def test_xml_subtags_present(self):
        """基础子标签应出现（<进度>/<行为>/<语气>/<禁止>），与 tier_extras 无关"""
        out = self.build(_Rec(), "友好", (61, 100), 80)
        for tag in ["<进度>", "<行为>", "<语气>", "<禁止>"]:
            self.assertIn(tag, out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
