"""`/emotion me` / `query` 的档位描述展示范围。

档位描述是「角色对这段关系的自述」，查他人时多带一句不会报错，属于静默泄漏。

运行：
    python3 -m unittest tests.test_query_impl -v
"""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import EventStub, PLUGIN_DIR, load_methods, make_logger  # noqa: E402

sys.path.insert(0, str(PLUGIN_DIR))
from domain import (  # noqa: E402
    EMOTION_DIMENSIONS,
    build_report_html,
    compute_tier_progress,
    format_emotion_detail,
)

TIER_LOW = {"min_value": 351, "max_value": 550, "describe": "喜欢"}
TIER_MAX = {"min_value": 551, "max_value": 800, "describe": "挚爱"}

class _FavourRecord:
    def __init__(self, persona_id="", user_id="", favour=0, **_kwargs):
        self.persona_id = persona_id
        self.user_id = user_id
        self.favour = favour
        for dimension in EMOTION_DIMENSIONS:
            setattr(self, dimension, 0)

def _record(**overrides):
    values = dict(
        joy=52, trust=30, fear=12, surprise=16, sadness=8, disgust=4,
        anger=2, anticipation=18, pride=28, guilt=6, shame=22, envy=5,
        favour=440,
    )
    values.update(overrides)
    return SimpleNamespace(**values)

async def _display_name(_event, uid):
    return f"用户{uid}"

class _Stub:
    min_favour_value = 0
    max_favour_value = 800

    def __init__(self, **overrides):
        self.persona_id = "persona_demo"
        self.record = overrides.get("record", _record())
        self.initial_favour = overrides.get("initial_favour", 128)
        self.relationship = overrides.get("relationship", "喜欢")
        self.relationship_range = overrides.get("relationship_range")
        self.special = overrides.get("special", False)
        self.tier_script = overrides.get("tier_script", "别多想，我只是有点习惯了。")
        self.tier_script_enabled = overrides.get("tier_script_enabled", True)
        self.tier_calls = []
        self.rendered = []
        self.initial_calls = []
        self.db = SimpleNamespace(get_favour=self._get_favour)

    async def _get_favour(self, _persona_id, _user_id):
        return self.record

    async def _get_persona_id(self, _event):
        return self.persona_id

    def _decay_favour_value(self, record):
        return record.favour

    async def _get_initial_favour_for(self, _event, user_id):
        self.initial_calls.append(user_id)
        return self.initial_favour

    def _get_relationship(self, _favour, _user_id=""):
        return self.relationship

    def _get_relationship_range(self, _favour, _user_id=""):
        return self.relationship_range

    def _is_special_override(self, _user_id):
        return self.special

    def _get_max_tier(self):
        return TIER_MAX

    def _find_tier(self, favour):
        return TIER_LOW if favour <= 550 else TIER_MAX

    def _find_next_tier(self, favour):
        return TIER_MAX if favour <= 550 else None

    async def _tier_script_for(self, _event, _persona_id, _tier):
        self.tier_calls.append(_tier)
        return self.tier_script

    async def _render_t2i(self, md_text, width=800):
        self.rendered.append(md_text)
        return "image.png"

    def _tag(self, _event):
        return "[EmotionFavour]"

class QueryImplTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_methods(
            ("_query_favour_impl",),
            namespace={
                "logger": make_logger("test_query_impl"),
                "get_user_display_name": _display_name,
                "FavourRecord": _FavourRecord,
                "compute_tier_progress": compute_tier_progress,
                "build_report_html": build_report_html,
                "format_emotion_detail": format_emotion_detail,
            },
            stub_cls=_Stub,
        )

    def _run(self, stub=None, call_kwargs=None, **kwargs):
        stub = stub or _Stub(**kwargs)

        async def collect():
            return [
                item async for item in stub._query_favour_impl(
                    EventStub(), "10001", **(call_kwargs or {})
                )
            ]

        return stub, asyncio.run(collect())

    def test_simple_mode_does_not_always_report_max_tier(self):
        """simple 模式（默认）没有 advance 档位，不能因此显示「已满级」与实际区间脱节。"""
        stub = _Stub(relationship_range=(31, 100))
        stub._find_tier = lambda _favour: None
        stub._find_next_tier = lambda _favour: None
        stub, results = self._run(stub=stub, record=_record(favour=50))
        md_text = stub.rendered[0]
        self.assertNotIn("已满级", md_text)
        self.assertIn("距下一级还需", md_text)

    def test_tier_script_can_be_disabled_by_config(self):
        """开关关掉后既不显示也不该后台生成（配置被静默忽略会白烧 LLM 调用）。"""
        stub, _ = self._run(
            call_kwargs={"include_tier_script": True}, tier_script_enabled=False
        )
        self.assertEqual(stub.tier_calls, [])
        self.assertNotIn("<blockquote>", stub.rendered[0])

    def test_tier_script_only_for_self_query(self):
        """查他人时多带一句角色自述不会报错，属于静默泄漏，必须盯住。"""
        without, _ = self._run()
        self.assertEqual(without.tier_calls, [])
        self.assertNotIn("<blockquote>", without.rendered[0])

        with_script, _ = self._run(call_kwargs={"include_tier_script": True})
        self.assertEqual(len(with_script.tier_calls), 1)
        self.assertIn(
            "<blockquote>别多想，我只是有点习惯了。</blockquote>", with_script.rendered[0]
        )

if __name__ == "__main__":
    unittest.main()
