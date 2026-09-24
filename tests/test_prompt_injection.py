"""注入通道的幂等与标记协议。

`SYSTEM_PROMPT_MARKER` / `RUNTIME_PROMPT_MARKER` 是注入的协议标记：重复注入不会
抛错，只会让 system prompt 每轮膨胀；动态状态若走错通道则会长期留在系统提示里
变成过期快照。两者都属改错一眼看不出的静默失效。

运行：
    python3 -m unittest tests.test_prompt_injection -v
"""
import unittest

from prompt_injection import (
    RUNTIME_PROMPT_MARKER,
    SYSTEM_PROMPT_MARKER,
    append_system_prompt_once,
    inject_runtime_prompt,
)


class _Request:
    def __init__(self, parts=None):
        self.system_prompt = "基础系统提示"
        if parts is not None:
            self.extra_user_content_parts = parts


class _Part:
    def __init__(self, text):
        self.text = text
        self.marked_temp = False

    def mark_as_temp(self):
        self.marked_temp = True
        return self


class TestPromptInjection(unittest.TestCase):
    def test_system_prompt_marker_is_idempotent(self):
        """守「元规则块重复追加」：每轮请求都会走这个入口，标记失效会让 system prompt
        无限增长（不报错），挤占上下文且破坏 prompt cache 命中。"""
        request = _Request()
        self.assertTrue(append_system_prompt_once(request, "规则", SYSTEM_PROMPT_MARKER))
        self.assertFalse(append_system_prompt_once(request, "规则", SYSTEM_PROMPT_MARKER))
        self.assertEqual(request.system_prompt.count(SYSTEM_PROMPT_MARKER), 1)

    def test_runtime_uses_temporary_extra_part(self):
        """守「动态状态走错注入通道」：必须进本轮临时 user part 并标记 temp。
        退回 system_prompt 不会报错，但当前好感度/情感会以过期快照长期留在系统提示里。"""
        request = _Request([])
        channel = inject_runtime_prompt(request, "动态状态", _Part)
        self.assertEqual(channel, "extra_user_content_parts")
        self.assertEqual(len(request.extra_user_content_parts), 1)
        self.assertIn(RUNTIME_PROMPT_MARKER, request.extra_user_content_parts[0].text)
        self.assertTrue(request.extra_user_content_parts[0].marked_temp)

if __name__ == "__main__":
    unittest.main()
