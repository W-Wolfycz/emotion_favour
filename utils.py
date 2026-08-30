# utils.py
# 通用工具函数
import string
import time
from pathlib import Path
from typing import Optional

from astrbot.core.message.components import At
from astrbot.core.platform.astr_message_event import AstrMessageEvent


def get_target_uid(event: AstrMessageEvent, text_arg: str) -> str | None:
    bot_self_id = None
    if hasattr(event, 'messageObj') and hasattr(event.messageObj, 'self_id'):
        bot_self_id = str(event.messageObj.self_id)

    if hasattr(event, 'messageObj') and hasattr(event.messageObj, 'message'):
        for component in event.messageObj.message:
            if isinstance(component, At):
                uid = str(component.qq)
                if bot_self_id and uid == bot_self_id:
                    continue
                return uid

    if text_arg:
        cleaned_arg = text_arg.strip()
        if _is_valid_uid(cleaned_arg):
            return cleaned_arg

    return None


def _is_valid_uid(userid: str) -> bool:
    if not userid or len(userid.strip()) == 0:
        return False
    userid = userid.strip()
    if len(userid) > 64:
        return False
    allowed_chars = string.ascii_letters + string.digits + "_-:@."
    return all(c in allowed_chars for c in userid)


def escape_markdown(text: str) -> str:
    if not text:
        return ""
    mapping = {
        "|": "&#124;", "`": "&#96;", "*": "&#42;", "~": "&#126;",
        "_": "&#95;", "[": "&#91;", "]": "&#93;", "\n": " "
    }
    for char, entity in mapping.items():
        text = text.replace(char, entity)
    return text


def cleanup_old_files(
    directory: Path,
    *,
    suffix: str,
    max_age_days: int = 7,
    now: Optional[float] = None,
) -> int:
    """删除目录中修改时间早于 max_age_days 天的匹配文件，返回删除数。

    用于 T2I 渲染缓存等持续累积的临时产物；单个文件删除失败静默跳过。
    """
    if not directory.is_dir():
        return 0
    cutoff = (now if now is not None else time.time()) - max_age_days * 86400
    removed = 0
    for path in directory.glob(f"*{suffix}"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


async def get_user_display_name(event: AstrMessageEvent, user_id: str) -> str:
    try:
        group_id = event.get_group_id()
        if group_id:
            info = await event.bot.get_group_member_info(group_id=int(group_id), user_id=int(user_id), no_cache=True)
            return info.get("card") or info.get("nickname") or user_id
        else:
            info = await event.bot.get_stranger_info(user_id=int(user_id))
            return info.get("nickname") or user_id
    except:
        return user_id
