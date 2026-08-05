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
        request = _Request()
        self.assertTrue(append_system_prompt_once(request, "规则", SYSTEM_PROMPT_MARKER))
        self.assertFalse(append_system_prompt_once(request, "规则", SYSTEM_PROMPT_MARKER))
        self.assertEqual(request.system_prompt.count(SYSTEM_PROMPT_MARKER), 1)

    def test_runtime_uses_temporary_extra_part(self):
        request = _Request([])
        channel = inject_runtime_prompt(request, "动态状态", _Part)
        self.assertEqual(channel, "extra_user_content_parts")
        self.assertEqual(len(request.extra_user_content_parts), 1)
        self.assertIn(RUNTIME_PROMPT_MARKER, request.extra_user_content_parts[0].text)
        self.assertTrue(request.extra_user_content_parts[0].marked_temp)

    def test_runtime_falls_back_to_system_prompt(self):
        request = _Request()
        channel = inject_runtime_prompt(request, "动态状态", _Part)
        self.assertEqual(channel, "system_prompt")
        self.assertIn(RUNTIME_PROMPT_MARKER, request.system_prompt)

    def test_existing_runtime_part_is_not_duplicated(self):
        request = _Request([_Part(f"{RUNTIME_PROMPT_MARKER}\n旧状态")])
        channel = inject_runtime_prompt(request, "新状态", _Part)
        self.assertEqual(channel, "extra_user_content_parts(existing)")
        self.assertEqual(len(request.extra_user_content_parts), 1)


if __name__ == "__main__":
    unittest.main()
