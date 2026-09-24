"""档位描述的响应解析与缓存新鲜度测试（domain 纯函数）。

模型输出 → {档位: 描述}；缓存是否仍然可用。两者出错都不抛异常：解析失败只是
白烧一次 LLM 调用，新鲜度判错则要么让描述永不更新，要么每次查询都重复生成。

运行：
    python3 -m unittest tests.test_tier_script -v
"""
import importlib.util
import json
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
    parse_tier_script_response,
    tier_scripts_fresh,
)

TIERS = [
    {"min_value": 0, "max_value": 30, "describe": "失望"},
    {"min_value": 31, "max_value": 100, "describe": "陌生"},
]


class TierScriptParseTest(unittest.TestCase):
    def test_valid_payload_variants(self):
        """守「模型输出被静默丢弃」：带解说词/代码块包裹、min_value 写成 0.0 或 "31"、
        整句带引号都必须照常解析；丢一条不报错，只是该档位永远没有描述。
        代码块后还带一段含花括号的说明，用来确认剥代码块这步真的在起作用。"""
        plain = parse_tier_script_response(
            json.dumps({"tiers": [{"min_value": 0, "script": "保持距离。"}]}), TIERS
        )
        self.assertEqual(plain["0"], {"label": "失望", "script": "保持距离。"})

        fenced = (
            "好的：\n```json\n"
            '{"tiers": [{"min_value": 31, "script": "有点印象了。"}]}\n'
            "```\n补充：不要写 {占位符} 这类内容。"
        )
        self.assertEqual(
            parse_tier_script_response(fenced, TIERS)["31"]["script"], "有点印象了。"
        )

        # 模型把 min_value 写成 0.0 / "31" 时不能整条丢弃；整句引号要剥掉
        normalized = parse_tier_script_response(
            json.dumps({"tiers": [
                {"min_value": 0.0, "script": '"别多想。"'},
                {"min_value": "31", "script": "字符串键。"},
            ]}),
            TIERS,
        )
        self.assertEqual(sorted(normalized), ["0", "31"])
        self.assertEqual(normalized["0"], {"label": "失望", "script": "别多想。"})


class TierScriptFreshnessTest(unittest.TestCase):
    def test_stale_reasons(self):
        """守「缓存永不失效 / 该重生成时不重生成」：缺档、档位名变化、人设指纹变化、
        描述为空四种情况都必须判为不新鲜。判漏不会报错，只会一直展示旧人设的文案。"""
        complete = {
            "0": {"script": "保持距离。", "hash": "h", "label": "失望"},
            "31": {"script": "有点印象。", "hash": "h", "label": "陌生"},
        }
        cases = {
            "缺档": {"0": complete["0"]},
            "档位名变化": {**complete, "31": {**complete["31"], "label": "改过的档位名"}},
            "人设指纹变化": {
                "0": {**complete["0"], "hash": "旧"},
                "31": {**complete["31"], "hash": "旧"},
            },
            "描述为空": {**complete, "0": {**complete["0"], "script": ""}},
        }
        for label, cache in cases.items():
            self.assertFalse(tier_scripts_fresh(cache, TIERS, "h"), label)

if __name__ == "__main__":
    unittest.main()
