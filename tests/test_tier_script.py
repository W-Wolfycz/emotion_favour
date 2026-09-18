"""档位描述的提示词、响应解析与缓存新鲜度测试（domain 纯函数）。

数据转换：结构化档位 → 提示词；模型输出 → {档位: 描述}；缓存是否仍然可用。
与出图无关，渲染只负责把它显示成 blockquote。

运行：
    python3 -m unittest tests.test_tier_script -v
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import PLUGIN_DIR  # noqa: E402

sys.path.insert(0, str(PLUGIN_DIR))
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
        plain = parse_tier_script_response(
            json.dumps({"tiers": [{"min_value": 0, "script": "保持距离。"}]}), TIERS
        )
        self.assertEqual(plain["0"], {"label": "失望", "script": "保持距离。"})

        fenced = '好的：\n```json\n{"tiers": [{"min_value": 31, "script": "有点印象了。"}]}\n```'
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
            "空缓存": {},
        }
        for label, cache in cases.items():
            self.assertFalse(tier_scripts_fresh(cache, TIERS, "h"), label)

if __name__ == "__main__":
    unittest.main()
