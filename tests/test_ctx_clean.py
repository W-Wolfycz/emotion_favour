"""emotion_favour main.py 的 _clean_history_contexts 测试。

main.py 顶部 import 了 astrbot / sqlmodel 等本地未装的库，
按 plugin_local_testing 记忆，把目标方法与清洗正则从源码里以字符串提取出来
exec 到独立命名空间，绕过模块顶部 import。

覆盖点：
- 清洗后为空的普通消息被丢弃；
- 携带 tool_calls 的 assistant 消息即使正文清洗后为空也必须保留，
  保证后续 role=tool 输出的 call_id 能找到对应的 function_call；
- role=tool 消息必须保留，避免 function_call 失去配对输出；
- 清洗后非空的内容正常保留并移除旧注入标记。

运行：
    python3 tests/test_ctx_clean.py
    python3 -m unittest tests.test_ctx_clean -v
"""
import os
import re
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN_PY = os.path.join(HERE, "..", "main.py")


def _extract_clean_fn(src: str) -> str:
    """提取 _clean_history_contexts 静态方法源码（含 def 行），dedent 到顶层。
    停止边界：下一个同缩进 def、同缩进装饰器行或类外顶层结构。"""
    lines = src.splitlines(keepends=True)
    start = None
    indent = ""
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)def _clean_history_contexts\(", line)
        if m and m.group(1):
            start = i
            indent = m.group(1)
            break
    if start is None:
        raise ValueError("未找到 _clean_history_contexts")
    pad_len = len(indent)
    end = len(lines)
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if not line.strip():
            continue
        m = re.match(r"^(\s*)def ", line)
        if m and len(m.group(1)) == pad_len:
            end = j
            break
        dm = re.match(r"^(\s*)@", line)
        if dm and len(dm.group(1)) == pad_len:
            end = j
            break
        if not (line.startswith(" ") or line.startswith("\t")):
            end = j
            break
    return textwrap.dedent("".join(lines[start:end]))


def _extract_clean_pattern(src: str) -> str:
    """提取 _CTX_CLEAN_PATTERN 的 re.compile(...) 赋值块。"""
    lines = src.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^\s*_CTX_CLEAN_PATTERN = re\.compile\(", line):
            start = i
            break
    if start is None:
        raise ValueError("未找到 _CTX_CLEAN_PATTERN")
    end = None
    for j in range(start, len(lines)):
        if lines[j].strip() == ")":
            end = j + 1
            break
    if end is None:
        raise ValueError("_CTX_CLEAN_PATTERN 赋值块不完整")
    return textwrap.dedent("".join(lines[start:end]))


def _load():
    with open(MAIN_PY, encoding="utf-8") as f:
        src = f.read()
    ns = {"re": re}
    prefix = "from __future__ import annotations\n\n"
    exec(prefix + _extract_clean_pattern(src), ns)
    exec(prefix + _extract_clean_fn(src), ns)
    return ns["_clean_history_contexts"], ns["_CTX_CLEAN_PATTERN"]


_clean_history_contexts, _CTX_CLEAN_PATTERN = _load()

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
    def test_drops_message_cleaned_to_empty(self):
        contexts = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "<thought>内部思考</thought>"},
        ]
        cleaned = _clean_history_contexts(contexts, _CTX_CLEAN_PATTERN)
        self.assertEqual(cleaned, [{"role": "user", "content": "hello"}])

    def test_keeps_assistant_with_tool_calls_when_content_cleaned_empty(self):
        contexts = [
            _assistant_toolcall("<thought>内部思考</thought>"),
            _tool_msg("SKILL 全文输出"),
        ]
        cleaned = _clean_history_contexts(contexts, _CTX_CLEAN_PATTERN)
        self.assertEqual(len(cleaned), 2)
        self.assertEqual(cleaned[0]["role"], "assistant")
        self.assertEqual(cleaned[0]["content"], "")
        self.assertEqual(cleaned[0]["tool_calls"][0]["id"], TOOL_CALL_ID)
        # tool 消息必须紧跟保留的 assistant，call_id 配对完整
        self.assertEqual(cleaned[1]["role"], "tool")
        self.assertEqual(cleaned[1]["tool_call_id"], TOOL_CALL_ID)

    def test_keeps_tool_message_with_normal_content(self):
        contexts = [
            _assistant_toolcall("发起技能调用"),
            _tool_msg("SKILL 全文输出"),
        ]
        cleaned = _clean_history_contexts(contexts, _CTX_CLEAN_PATTERN)
        self.assertEqual(len(cleaned), 2)
        self.assertEqual(cleaned[1]["content"], "SKILL 全文输出")

    def test_keeps_tool_message_even_if_cleaned_empty(self):
        contexts = [
            _assistant_toolcall("发起技能调用"),
            _tool_msg("<情感好感>旧面板</情感好感>"),
        ]
        cleaned = _clean_history_contexts(contexts, _CTX_CLEAN_PATTERN)
        self.assertEqual(len(cleaned), 2)
        self.assertEqual(cleaned[1]["role"], "tool")
        self.assertEqual(cleaned[1]["content"], "")

    def test_strips_markers_but_keeps_rest(self):
        contexts = [
            {
                "role": "assistant",
                "content": "前缀<thought>内部思考</thought>后缀",
            },
        ]
        cleaned = _clean_history_contexts(contexts, _CTX_CLEAN_PATTERN)
        self.assertEqual(cleaned[0]["content"], "前缀后缀")

    def test_strings_and_unknown_types(self):
        contexts = [
            "system 提示",
            "<情感好感>旧面板</情感好感>",
            object(),
        ]
        cleaned = _clean_history_contexts(contexts, _CTX_CLEAN_PATTERN)
        self.assertEqual(len(cleaned), 2)
        self.assertEqual(cleaned[0], "system 提示")
        self.assertIs(cleaned[1], contexts[2])

    def test_empty_tool_calls_list_is_not_treated_as_call(self):
        contexts = [
            {"role": "assistant", "content": "<thought>思考</thought>", "tool_calls": []},
        ]
        cleaned = _clean_history_contexts(contexts, _CTX_CLEAN_PATTERN)
        self.assertEqual(cleaned, [])

    def test_non_string_content_is_preserved(self):
        """多模态 list content 与 None content 不被 str() 破坏/污染。"""
        multimodal = [
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
        ]
        contexts = [
            {"role": "user", "content": multimodal},
            {"role": "assistant", "content": None},
        ]
        cleaned = _clean_history_contexts(contexts, _CTX_CLEAN_PATTERN)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["content"], multimodal)

    def test_full_skill_roundtrip_keeps_pairing(self):
        """模拟真实故障轮：assistant 正文只有 thought 块 + 技能 tool_calls，
        其后 tool 消息为 SKILL 全文。清洗后必须完整保留配对。"""
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


if __name__ == "__main__":
    unittest.main()
