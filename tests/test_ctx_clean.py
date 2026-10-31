"""emotion_favour main.py 的 _clean_history_contexts 回归测试。

只保留 400 事故的回归点：清洗后为空的 role=tool 消息必须保留、完整技能轮的
assistant + tool 配对不能被拆散。提取工具统一来自 tests/_shared.py。

运行：
    python3 -m unittest tests.test_ctx_clean -v
"""
import importlib.util
import re
import unittest
from pathlib import Path


def _load_shared():
    """按路径加载 tests/_shared.py（pytest 下不能按包名 import）。"""
    path = Path(__file__).resolve().parent / "_shared.py"
    spec = importlib.util.spec_from_file_location("ef_tests_shared", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

_shared = _load_shared()
extract_class_attribute = _shared.extract_class_attribute
extract_method = _shared.extract_method
read_main_source = _shared.read_main_source


def _load():
    src = read_main_source()
    ns = {"re": re}
    prefix = "from __future__ import annotations\n\n"
    exec(prefix + extract_class_attribute(src, "_CTX_CLEAN_PATTERN"), ns)
    exec(prefix + extract_method(src, "_clean_history_contexts"), ns)
    return ns["_clean_history_contexts"], ns["_CTX_CLEAN_PATTERN"]

_clean_history_contexts, _CTX_CLEAN_PATTERN = _load()

def test_legacy_chinese_blocks_are_still_stripped():
    """守升级路径：3.6.2 之前注入的是中文标签（<情感好感> / <他人关系>），
    升级后这些块仍躺在历史上下文里。清洗正则只认新标签的话，它们会一直堆到
    上下文上限——不报错、只是每轮都白白多带几百字。"""
    legacy = (
        "<情感好感>\n  <状态>\n    <好感度>120</好感度>\n"
        "    <关系>友好</关系>\n  </状态>\n</情感好感>\n"
        "<他人关系>\n  <对象 昵称=\"小蓝\" 关系=\"喜欢\"/>\n</他人关系>"
    )
    contexts = [{"role": "user", "content": f"你好\n\n{legacy}"}]
    cleaned = _clean_history_contexts(contexts, _CTX_CLEAN_PATTERN)
    assert cleaned[0]["content"] == "你好"


TOOL_CALL_ID = "call_00_l7pEfPNSfm8E3OJOrfsw0372"


def _assistant_toolcall(content):
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": TOOL_CALL_ID,
                "type": "function",
                "function": {"name": "skill", "arguments": "{}"},
            }
        ],
    }


def _tool_msg(content, call_id=TOOL_CALL_ID):
    return {"role": "tool", "content": content, "tool_call_id": call_id}


class TestCleanHistoryContexts(unittest.TestCase):
    def test_full_skill_roundtrip_keeps_pairing(self):
        """同一回归的完整形态：assistant 正文只剩 thought 块（清洗后为空）但带
        tool_calls，必须与后面的 tool 输出一起保留，call_id 不能断。"""
        contexts = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "帮我查一下"},
            _assistant_toolcall("<thought>需要调用技能</thought>"),
            _tool_msg("SKILL 全文输出（非空）"),
            {"role": "assistant", "content": "查到了，结果是……"},
        ]
        cleaned = _clean_history_contexts(contexts, _CTX_CLEAN_PATTERN)
        self.assertEqual(len(cleaned), 5)
        self.assertEqual(cleaned[2]["role"], "assistant")
        self.assertEqual(cleaned[2]["content"], "")
        self.assertEqual(cleaned[2]["tool_calls"][0]["id"], TOOL_CALL_ID)
        self.assertEqual(cleaned[3]["role"], "tool")
        self.assertEqual(cleaned[3]["tool_call_id"], TOOL_CALL_ID)
        self.assertEqual(cleaned[4]["content"], "查到了，结果是……")

    def test_keeps_tool_message_even_if_cleaned_empty(self):
        """400 事故回归：旧注入面板整段落在 role=tool 消息里，清洗后正文为空。
        若按「空内容即丢弃」处理，配对丢失 → 上游把孤儿 function_call_output 拒掉。"""
        contexts = [
            _assistant_toolcall("发起技能调用"),
            _tool_msg("<情感好感>旧面板</情感好感>"),
        ]
        cleaned = _clean_history_contexts(contexts, _CTX_CLEAN_PATTERN)
        self.assertEqual(len(cleaned), 2)
        self.assertEqual(cleaned[1]["role"], "tool")
        self.assertEqual(cleaned[1]["content"], "")

if __name__ == "__main__":
    unittest.main()
