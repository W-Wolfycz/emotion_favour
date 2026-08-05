"""好感度 Prompt 的幂等注入与 Provider 兼容回退。"""

from __future__ import annotations

from typing import Callable


SYSTEM_PROMPT_MARKER = "[EMOTION_FAVOUR_SYSTEM_PROMPT]"
RUNTIME_PROMPT_MARKER = "[EMOTION_FAVOUR_RUNTIME_PROMPT]"


def append_system_prompt_once(request: object, text: str, marker: str) -> bool:
    current = str(getattr(request, "system_prompt", "") or "")
    if marker in current:
        return False
    addition = f"{marker}\n{str(text or '').strip()}".strip()
    if not addition:
        return False
    setattr(request, "system_prompt", "\n\n".join(part for part in (current.strip(), addition) if part))
    return True


def _extra_contains_marker(parts: list, marker: str) -> bool:
    for part in parts:
        if marker in str(getattr(part, "text", "") or ""):
            return True
    return False


def inject_runtime_prompt(
    request: object,
    runtime_text: str,
    text_part_factory: Callable[..., object],
) -> str:
    """优先注入临时 user part；字段不可用时回退到 system_prompt。"""
    marked_text = f"{RUNTIME_PROMPT_MARKER}\n{str(runtime_text or '').strip()}".strip()
    parts = getattr(request, "extra_user_content_parts", None)
    if isinstance(parts, list):
        if _extra_contains_marker(parts, RUNTIME_PROMPT_MARKER):
            return "extra_user_content_parts(existing)"
        part = text_part_factory(text=marked_text)
        mark_as_temp = getattr(part, "mark_as_temp", None)
        if callable(mark_as_temp):
            marked_part = mark_as_temp()
            if marked_part is not None:
                part = marked_part
        parts.append(part)
        return "extra_user_content_parts"
    append_system_prompt_once(request, marked_text, RUNTIME_PROMPT_MARKER)
    return "system_prompt"
