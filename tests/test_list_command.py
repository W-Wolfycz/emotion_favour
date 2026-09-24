"""`/emotion list` 昵称模式的取数分支。

昵称不是数据库列，误走 list_records 会静默查到错误数据（storage.list_records
对未知排序列回落到 favour，不报错）。

运行：
    python3 -m unittest tests.test_list_command -v
"""
import asyncio
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace


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

from domain import (  # noqa: E402
    EMOTION_DIMENSIONS,
    EMOTION_DISPLAY_NAMES,
    build_list_html,
    get_dominant_emotions,
)


def _stub_escape_markdown(text) -> str:
    """只需要可判定的文本；转义本身不在本用例的判据内。"""
    return str(text)


def _record(user_id, **overrides):
    values = dict(
        joy=52, trust=30, fear=12, surprise=16, sadness=8, disgust=4,
        anger=2, anticipation=18, pride=28, guilt=6, shame=22, envy=5,
        favour=440,
    )
    values.update(overrides)
    record = SimpleNamespace(user_id=user_id, **values)
    for dimension in EMOTION_DIMENSIONS:
        setattr(record, dimension, values[dimension])
    return record


async def _display_name(_event, uid):
    return f"用户{uid}"


class _Stub:
    group_sort_by = "favour"

    def __init__(self, **overrides):
        self.permitted = overrides.get("permitted", True)
        self.total = overrides.get("total", 20)
        self.records = overrides.get("records") or [
            _record(f"1000{index}") for index in range(self.total)
        ]
        self.calls = []
        self.rendered = []
        self.db = SimpleNamespace(
            count_records=self._count_records,
            list_records=self._list_records,
            get_global_records=self._get_global_records,
        )

    async def _check_command_permission(self, _event, _command):
        return self.permitted

    async def _get_persona_id(self, _event):
        return "persona_demo"

    async def _count_records(self, _persona_id):
        return self.total

    async def _list_records(
        self, persona_id, offset=0, limit=20, sort_by="", sort_order=""
    ):
        self.calls.append({
            "offset": offset, "limit": limit,
            "sort_by": sort_by, "sort_order": sort_order,
        })
        return self.records[offset:offset + limit]

    async def _get_global_records(self, _persona_id):
        return list(self.records)

    async def _sort_records(self, _event, records):
        return list(records)

    def _decay_favour_value(self, record):
        return record.favour

    def _get_relationship(self, _favour, _user_id=""):
        return "喜欢"

    async def _render_t2i(self, md_text, width=800):
        self.rendered.append(md_text)
        return "image.png"

    def _tag(self, _event):
        return "[EmotionFavour]"


class ListCommandTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_methods(
            ("query_global_favour",),
            namespace={
                "asyncio": asyncio,
                "logger": make_logger("test_list_command"),
                "get_user_display_name": _display_name,
                "escape_markdown": _stub_escape_markdown,
                "EMOTION_DISPLAY_NAMES": EMOTION_DISPLAY_NAMES,
                "get_dominant_emotions": get_dominant_emotions,
                "build_list_html": build_list_html,
                "AstrMessageEvent": object,
            },
            stub_cls=_Stub,
        )

    def _run(self, page=1, stub=None, **kwargs):
        stub = stub or _Stub(**kwargs)

        async def collect():
            return [item async for item in stub.query_global_favour(EventStub(), page)]

        return stub, asyncio.run(collect())

    def test_nickname_sort_mode_does_not_query_by_column(self):
        """守「昵称模式误走数据库排序列」：list_records 对未知列静默回落 favour，
        出图照常成功但顺序与页码内容都是错的，不看数据看不出来。"""
        stub = _Stub(total=3)
        stub.group_sort_by = "nickname"
        stub, results = self._run(stub=stub)
        self.assertEqual(results[0][0], "image")
        self.assertEqual(stub.calls, [])

if __name__ == "__main__":
    unittest.main()
