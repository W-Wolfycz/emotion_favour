# utils.py
# 通用工具函数
import re
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
    except Exception:
        # 只吞普通异常：裸 except 会把 CancelledError 一起吃掉，破坏任务取消语义
        return user_id


# 非文本消息组件 → 结算用占位文本。用户只发图片/表情/语音时 message_str 为空，
# 不补占位的话整轮不会进入结算（回复侧早有同样的占位处理）。
MEDIA_PLACEHOLDERS = {
    "Image": "[图片]",
    "Face": "[表情]",
    "Record": "[语音]",
    "Video": "[视频]",
    "File": "[文件]",
    "Forward": "[转发消息]",
    "Node": "[转发消息]",
    "Poke": "[戳一戳]",
    "Share": "[分享]",
    "Location": "[位置]",
    "Music": "[音乐]",
    "Json": "[卡片]",
}


def placeholder_for_message(chain) -> str:
    """把非文本消息链映射成占位文本；认不出类型时给通用占位。"""
    names = [type(comp).__name__ for comp in (chain or [])]
    parts = [MEDIA_PLACEHOLDERS[name] for name in names if name in MEDIA_PLACEHOLDERS]
    return "".join(parts) or "[非文本消息]"


def strip_reply_markers(text: str, pattern) -> str:
    """剥掉正文里的思考块与旧注入标记，围栏代码块内的内容原样保留。

    模型偶尔会把 <thinking> 直接写进正文（core 的 result_decorate 只处理独立的
    reasoning_content 字段），泄露给用户；但它也可能在示例代码里引用这些标签，
    那种情况必须原样发出。整条回复都是思考块时返回原文——发空消息比露出异常更糟。
    """
    stashed: list[str] = []

    def _stash(match: "re.Match") -> str:
        stashed.append(match.group(0))
        return f"\x00block{len(stashed) - 1}\x00"

    protected = re.sub(r"```[\s\S]*?```", _stash, text)
    cleaned = pattern.sub("", protected).strip()
    for index, block in enumerate(stashed):
        cleaned = cleaned.replace(f"\x00block{index}\x00", block)
    return cleaned or text
