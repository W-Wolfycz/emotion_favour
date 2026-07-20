# main.py
import re
import json
import asyncio
import traceback
import hashlib
from pathlib import Path
from typing import List, Optional, Tuple
from datetime import datetime

from astrbot.api.star import Star, Context, StarTools
from astrbot.api import AstrBotConfig
from astrbot.api.provider import ProviderRequest
from astrbot.api.event import filter
from astrbot.core.message.components import Plain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.agent.message import TextPart
from astrbot.core.utils.session_waiter import session_waiter, SessionController, SessionFilter

from .log import logger, configure as configure_log
from .config import PluginSettings, validate_advance_tiers
from .runtime import KeyedLockPool, TaskSupervisor
from .playwright_support import install_chromium, is_missing_chromium_error
from .storage import FavourDBManager, FavourRecord
from .domain import (
    EMOTION_DIMENSIONS,
    EMOTION_DISPLAY_NAMES,
    build_emotion_panel,
    build_injection_prompt,
    build_system_prompt_extra,
    format_emotion_detail,
    get_dominant_emotions,
    diminish_delta,
    apply_emotion_decay,
    compute_favour_decay,
)
from .utils import (
    get_target_uid,
    escape_markdown,
    get_user_display_name,
)


class SenderSessionFilter(SessionFilter):
    """只让同一发送者的消息触发 waiter，避免群聊他人消息 / 平台 lifecycle 事件干扰。"""

    def __init__(self, sender_id: str) -> None:
        self.sender_id = str(sender_id)

    def filter(self, event: AstrMessageEvent) -> str:
        return f"{event.unified_msg_origin}|sender={event.get_sender_id()}"


class EmotionFavourPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        # 配置迁移：把旧版本配置升级到 CURRENT_CONFIG_VERSION
        # 必须在读取任何配置字段之前完成——原地修改 config dict
        from .migrate import migrate
        migrated_version_before = (
            config.get("config_version", 0) if isinstance(config, dict) else 0
        )
        migrate(config)
        if (
            isinstance(config, dict)
            and config.get("config_version", 0) != migrated_version_before
        ):
            # 真的发生迁移了 → 持久化到磁盘
            try:
                global_config = self._get_context_config()
                if global_config is not None and hasattr(
                    global_config, "save_config"
                ):
                    global_config.save_config()
            except Exception as e:
                logger.warning(
                    f"[emotion_favour] 迁移后持久化配置失败（内存中已迁移，下次启动会重跑）: {e}"
                )

        self.settings = PluginSettings.from_mapping(config)
        configure_log(self.settings.debug_to_info)
        for warning in self.settings.warnings:
            logger.warning(f"[EmotionFavour] 配置校验: {warning}")

        # 保留现有属性名，Hook/命令只从类型化 settings 初始化一次。
        for field_name in self.settings.__dataclass_fields__:
            if field_name != "warnings":
                setattr(self, field_name, getattr(self.settings, field_name))

        # chat_memory 集成（运行时按需解析，启动顺序无关）
        self._chat_memory = None
        # AstrBot Bot 管理员；插件管理命令统一只认此列表。
        self.admins_id = {str(value) for value in context.get_config().get("admins_id", [])}

        # 特殊关系用户只认配置中明确填写的 ID，不自动包含任何管理员。
        self._special_user_ids = set(self.favour_envoys)

        # 人设管理器
        self.persona_mgr = context.persona_manager

        # 数据库
        self.data_dir = StarTools.get_data_dir("emotion_favour")
        timezone_name = str(context.get_config().get("timezone", "Asia/Shanghai") or "Asia/Shanghai")
        self.db = FavourDBManager(
            self.data_dir,
            self.min_favour_value,
            self.max_favour_value,
            local_timezone=timezone_name,
        )

        # 运行期并发与生命周期
        self._state = "CREATED"
        self._tasks = TaskSupervisor(logger)
        self._record_locks = KeyedLockPool()
        self._blocked_personas: set[str] = set()
        self._blocked_records: set[tuple[str, str, str]] = set()
        self._llm_semaphore = asyncio.Semaphore(self.settlement_max_concurrency)
        self._browser_lock = asyncio.Lock()

        # Playwright T2I
        self._pw_instance = None
        self._pw_browser = None
        self._chromium_install_attempted = False
        self._t2i_output_dir = self.data_dir / "t2i_output"
        self._t2i_output_dir.mkdir(parents=True, exist_ok=True)
        plugin_dir = Path(__file__).parent
        html_path = plugin_dir / "custom_t2i.html"
        self._html_template = html_path.read_text(encoding="utf-8") if html_path.exists() else ""
        meta_path = plugin_dir / "metadata.yaml"
        meta_text = meta_path.read_text(encoding="utf-8") if meta_path.exists() else ""
        ver_match = re.search(r'version:\s*["\']?(.+?)["\']?\s*$', meta_text, re.MULTILINE)
        self._plugin_version = ver_match.group(1) if ver_match else ""

        # 本地化 CDN 脚本缓存
        self._local_scripts: dict[str, str] = {}

    async def initialize(self):
        """AstrBot 受管初始化入口。"""
        self._state = "STARTING"
        from .web_api import register_web_apis
        try:
            await self.db.init_db()
            await self._ensure_local_scripts()
            try:
                await self._ensure_browser(auto_install=False)
                logger.info("[EmotionFavour] Playwright 浏览器预热完成")
            except Exception as e:
                if is_missing_chromium_error(e):
                    logger.info(
                        "[EmotionFavour] 未检测到 Playwright Chromium，"
                        "将在首次 T2I 渲染时自动安装"
                    )
                else:
                    logger.warning(f"[EmotionFavour] Playwright 浏览器预热失败，将按需重试: {e}")
            register_web_apis(self.context, self)
            self._state = "READY"
            logger.info("[EmotionFavour] ✅ 初始化完成，Web API 已注册")
        except Exception as e:
            self._state = "FAILED"
            logger.error(f"[EmotionFavour] 初始化失败: {e}\n{traceback.format_exc()}")
            await self._close_resources()
            raise

    async def _close_resources(self) -> None:
        """释放可重复关闭的底层资源，供初始化回滚和 terminate 共用。"""
        async with self._browser_lock:
            if self._pw_browser is not None:
                try:
                    await self._pw_browser.close()
                except Exception as e:
                    logger.warning(f"[EmotionFavour] Playwright browser.close 失败: {e}")
                self._pw_browser = None
            if self._pw_instance is not None:
                try:
                    await self._pw_instance.stop()
                except Exception as e:
                    logger.warning(f"[EmotionFavour] Playwright stop 失败: {e}")
                self._pw_instance = None
        try:
            await self.db.close()
        except Exception as e:
            logger.warning(f"[EmotionFavour] 数据库关闭失败: {e}")

    async def terminate(self) -> None:
        """AstrBot 禁用/重载时停止任务并释放浏览器、数据库资源。"""
        self._state = "STOPPING"
        await self._tasks.close(flush_timeout=self.terminate_flush_timeout_seconds)
        await self._close_resources()
        self._state = "STOPPED"
        logger.info("[EmotionFavour] 已终止")

    def is_ready(self) -> bool:
        return self._state == "READY"

    def _resolve_chat_memory(self):
        """定位 chat_memory 插件实例。成功后缓存到 self._chat_memory；失败不缓存以便下次重试。"""
        if self._chat_memory is not None:
            return self._chat_memory
        try:
            star = self.context.get_registered_star("chat_memory")
            if star is not None:
                for candidate in (star, getattr(star, "star", None), getattr(star, "star_cls", None)):
                    if candidate is not None and hasattr(candidate, "query_rounds"):
                        self._chat_memory = candidate
                        return candidate
        except Exception:
            pass
        return None

    def _tag(self, event=None) -> str:
        if self.log_with_bot_id and event is not None:
            try:
                return f"[EmotionFavour:{event.get_platform_id()}]"
            except Exception:
                pass
        return "[EmotionFavour]"

    def _record_key(self, persona_id: str, user_id: str) -> tuple[str, str, str]:
        return ("record", str(persona_id), str(user_id))

    async def _judge_generate(
        self,
        *,
        provider_id: str,
        prompt: str,
        timeout: Optional[float] = None,
    ):
        """统一限制后台裁决 LLM 并发并施加超时。"""
        effective_timeout = timeout or self.settlement_timeout_seconds
        async with self._llm_semaphore:
            return await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                ),
                timeout=effective_timeout,
            )

    async def set_record_fields(
        self,
        persona_id: str,
        user_id: str,
        *,
        favour: Optional[int] = None,
        emotions_absolute: Optional[dict] = None,
    ) -> bool:
        key = self._record_key(persona_id, user_id)
        if persona_id in self._blocked_personas or key in self._blocked_records:
            return False
        async with self._record_locks.hold(key):
            if persona_id in self._blocked_personas or key in self._blocked_records:
                return False
            return await self.db.set_record_fields(
                persona_id,
                user_id,
                favour=favour,
                emotions_absolute=emotions_absolute,
            )

    async def create_record(
        self,
        persona_id: str,
        user_id: str,
        *,
        favour: int,
        emotions_absolute: Optional[dict] = None,
    ) -> bool:
        key = self._record_key(persona_id, user_id)
        if persona_id in self._blocked_personas or key in self._blocked_records:
            return False
        async with self._record_locks.hold(key):
            if persona_id in self._blocked_personas or key in self._blocked_records:
                return False
            return await self.db.create_record(
                persona_id,
                user_id,
                favour=favour,
                emotions_absolute=emotions_absolute,
            )

    async def update_record(
        self,
        persona_id: str,
        user_id: str,
        *,
        favour: Optional[int] = None,
        emotion_updates: Optional[dict] = None,
    ) -> bool:
        key = self._record_key(persona_id, user_id)
        if persona_id in self._blocked_personas or key in self._blocked_records:
            return False
        async with self._record_locks.hold(key):
            if persona_id in self._blocked_personas or key in self._blocked_records:
                return False
            return await self.db.update_favour(
                persona_id,
                user_id,
                favour=favour,
                emotion_updates=emotion_updates,
            )

    async def delete_record(self, persona_id: str, user_id: str):
        key = self._record_key(persona_id, user_id)
        async with self._record_locks.hold(key):
            return await self.db.delete_favour(persona_id, user_id)

    # ================= 对话历史提取 =================

    def _ts_tag(self, record: dict) -> str:
        """从记录中提取时间戳，返回 <time>...</time> 标签；无 created_at 或解析失败时返回空串。

        用 XML 标签包裹而非圆括号前缀——避免 LLM 把时间戳当成正文格式模仿。
        """
        created = record.get("created_at")
        if not created:
            return ""
        try:
            dt = datetime.strptime(str(created)[:19], "%Y-%m-%d %H:%M:%S")
            return dt.strftime("<time>%m-%d %H:%M</time>")
        except (ValueError, TypeError):
            return ""

    async def _get_recent_history(
        self,
        umo: str,
        user_id: str,
        conversation_id: str,
        current_bot_reply: str = "",
        rounds: Optional[int] = None,
        persona_id: Optional[str] = None,
    ) -> str:
        """获取最近 N 轮对话历史。

        启用 chat_memory 时从 chat_memory 读取（按 user_id 隔离）；
        否则从 AstrBot 自带上下文 conv.history 读取（群聊共享）。

        Args:
            rounds: 显式指定轮数。None 时用 self.history_rounds（默认好感度结算用）。
        """
        if rounds is None:
            rounds = self.history_rounds
        if rounds <= 0 or not conversation_id:
            return ""

        chat_memory = self._resolve_chat_memory() if self.use_chat_memory else None
        if self.use_chat_memory and chat_memory is None:
            logger.warning("[EmotionFavour] 未找到 chat_memory 插件，本次回退到 AstrBot 自带上下文")

        if chat_memory is not None:
            try:
                # chat_memory 1.0.0：只取当前人格下走完 LLM 的成功配对，避免切换
                # persona 后把其他人格的历史交给当前裁决模型。
                paired = await chat_memory.query_rounds(
                    umo, conversation_id, user_id,
                    limit_rounds=rounds,
                    llm_status="llm_success",
                    persona_id=persona_id,
                )
                records = []
                for pair in paired:
                    if len(pair) >= 2:
                        records.append(pair[0])
                        records.append(pair[1])
            except Exception as e:
                logger.warning(
                    f"[EmotionFavour] chat_memory 查询失败，本次回退到 AstrBot 自带上下文: {e}"
                )
                records = await self._read_astrbot_history(umo, conversation_id)
        else:
            records = await self._read_astrbot_history(umo, conversation_id)

        if not records:
            return ""

        # 去重：若末尾 assistant 等于当前 bot_reply，丢弃最后一对（避免当前轮次重复）
        if current_bot_reply and len(records) >= 2 and records[-1].get("role") == "assistant":
            if records[-1].get("content", "").strip() == current_bot_reply.strip():
                records = records[:-2]

        # 只保留最近 N 轮
        records = records[-(rounds * 2):]

        lines = []
        for i in range(0, len(records) - 1, 2):
            idx = i // 2 + 1
            user_ts = self._ts_tag(records[i])
            bot_ts = self._ts_tag(records[i + 1])
            user_prefix = f"  [{idx}] {user_ts} 用户" if user_ts else f"  [{idx}] 用户"
            bot_prefix = f"  [{idx}] {bot_ts} 角色" if bot_ts else f"  [{idx}] 角色"
            lines.append(f"{user_prefix}: {records[i].get('content', '')}")
            lines.append(f"{bot_prefix}: {records[i + 1].get('content', '')}")
        return "\n".join(lines)

    async def _read_astrbot_history(self, umo: str, conversation_id: str) -> list[dict]:
        """从 AstrBot 自带上下文读取历史，返回 [{role, content}, ...]。"""
        try:
            conv = await self.context.conversation_manager.get_conversation(umo, conversation_id)
            if not conv or not conv.history:
                return []
            raw = json.loads(conv.history) if isinstance(conv.history, str) else conv.history
            if not isinstance(raw, list):
                return []
            result = []
            for msg in raw:
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role", "")
                if role not in ("user", "assistant"):
                    continue
                content = str(msg.get("content", ""))
                content = self._CTX_CLEAN_PATTERN.sub("", content).strip()
                if content:
                    result.append({"role": role, "content": content})
            return result
        except Exception as e:
            logger.warning(f"[EmotionFavour] 读取 AstrBot 上下文失败: {e}")
            return []

    # ================= 人格 / 权限 / 关系 =================

    async def _get_persona_id(self, event: AstrMessageEvent) -> str:
        """获取当前 LLM 请求实际生效的 persona_id。

        与 chat_memory 同源：通过 persona_manager.resolve_selected_persona 按
        session 规则 > conversation.persona_id > config 默认 优先级解析，
        保证 favour 记录的 persona 与 LLM 实际使用的 persona 一致。
        旧实现只取 config 的 default_personality，会忽略 /persona 切换与
        conversation 级 persona，导致切人格后 favour 数据错位。
        """
        try:
            umo = event.unified_msg_origin
            conv_persona_id = None
            try:
                conv_mgr = self.context.conversation_manager
                cid = await conv_mgr.get_curr_conversation_id(umo)
                if cid:
                    conv = await conv_mgr.get_conversation(umo, cid)
                    conv_persona_id = getattr(conv, "persona_id", None) or None
            except Exception:
                pass

            cfg = self.context.get_config(umo=umo).get("provider_settings", {})
            resolved, _, _, _ = await self.persona_mgr.resolve_selected_persona(
                umo=umo,
                conversation_persona_id=conv_persona_id,
                platform_name=event.get_platform_name(),
                provider_settings=cfg,
            )
            if resolved and resolved != "[%None]":
                return resolved
            return "default"
        except Exception as e:
            logger.debug(f"[EmotionFavour] resolve_selected_persona 失败，回退 default: {e}")
            return "default"

    _PUBLIC_COMMANDS = frozenset({"me", "help"})
    _BOT_ADMIN_COMMANDS = frozenset({
        "query",
        "list",
        "set",
        "mood",
        "clear",
        "clear-all",
        "persona",
        "persona-clear",
    })

    def _is_bot_admin(self, event: AstrMessageEvent) -> bool:
        return str(event.get_sender_id()) in self.admins_id

    async def _check_command_permission(
        self,
        event: AstrMessageEvent,
        command_name: str,
    ) -> bool:
        if command_name in self._PUBLIC_COMMANDS:
            return True
        if command_name in self._BOT_ADMIN_COMMANDS:
            return self._is_bot_admin(event)
        return False

    def _get_relationship(self, favour: int, user_id: str = "") -> str:
        if self.admin_default_relationship and user_id and user_id in self._special_user_ids:
            return self.admin_default_relationship
        favour = max(self.min_favour_value, min(self.max_favour_value, favour))
        if self.relationship_mode == "simple":
            items = self.relationship_simple_list
            if not items:
                return "未知"
            n = len(items)
            total = self.max_favour_value - self.min_favour_value
            if total <= 0:
                return items[0]
            idx = int((favour - self.min_favour_value) * n / total)
            return items[max(0, min(idx, n - 1))]
        else:
            tier = self._find_tier(favour)
            return tier.get("describe", "未知") if tier else "未知"

    def _get_relationship_range(self, favour: int, user_id: str = "") -> Optional[Tuple[int, int]]:
        """返回 favour 所处关系区间的 (x, y)，用于进度计算。
        返回 None 表示无法确定区间（特殊关系覆盖、配置异常、空列表等）。
        与 _get_relationship 用同一套 idx 逻辑，保证区间名与范围对齐。
        """
        if self._is_special_override(user_id):
            # 特殊用户按最高等级处理：advance 模式下用最高等级的 (x, y)，让进度行进入「已达最高等级」分支；
            # simple 模式无 tier 概念，仍返回 None（进度行不注入）
            if self.relationship_mode == "advance":
                max_tier = self._get_max_tier()
                if max_tier is not None:
                    result = (
                        max_tier.get("min_value", self.min_favour_value),
                        max_tier.get("max_value", self.max_favour_value),
                    )
                    logger.debug(
                        f"[EmotionFavour] _get_relationship_range special override user={user_id} "
                        f"-> max_tier range={result}"
                    )
                    return result
            logger.debug(f"[EmotionFavour] _get_relationship_range skip: special override user={user_id}")
            return None
        clamped = max(self.min_favour_value, min(self.max_favour_value, favour))
        if clamped != favour:
            logger.debug(f"[EmotionFavour] _get_relationship_range clamp: {favour} -> {clamped}")
        favour = clamped

        result: Optional[Tuple[int, int]] = None
        reason = ""
        if self.relationship_mode == "simple":
            items = self.relationship_simple_list
            if not items:
                reason = "empty simple_list"
            else:
                n = len(items)
                total = self.max_favour_value - self.min_favour_value
                if total <= 0 or n == 0:
                    reason = f"invalid span total={total} n={n}"
                else:
                    idx = int((favour - self.min_favour_value) * n / total)
                    idx = max(0, min(idx, n - 1))
                    x = self.min_favour_value + round(idx * total / n)
                    if idx == n - 1:
                        y = self.max_favour_value
                    else:
                        y = self.min_favour_value + round((idx + 1) * total / n) - 1
                    result = (x, y)
        else:
            tier = self._find_tier(favour)
            if tier is not None:
                result = (
                    tier.get("min_value", self.min_favour_value),
                    tier.get("max_value", self.max_favour_value),
                )
            else:
                reason = "no advance interval matched"

        logger.debug(
            f"[EmotionFavour] _get_relationship_range favour={favour} mode={self.relationship_mode} "
            f"-> range={result}{' (' + reason + ')' if reason else ''}"
        )
        return result

    def _advance_items(self) -> list:
        """读取 advance_config，兼容老 JSON 字符串格式与新 template_list 格式。
        返回排序、去重叠后的 dict 列表，过滤掉 __template_key 等元字段。"""
        cached = getattr(self, "_advance_items_cache", None)
        if cached is not None:
            return cached
        raw = self.relationship_advance_raw
        if not raw:
            self._advance_items_cache = []
            return []
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return []
            if not isinstance(parsed, list):
                return []
            items = parsed
        elif isinstance(raw, list):
            items = raw
        else:
            return []
        result = []
        for r in items:
            if not isinstance(r, dict):
                continue
            result.append({k: v for k, v in r.items() if not k.startswith("__")})
        result, warnings = validate_advance_tiers(result)
        for warning in warnings:
            logger.warning(f"[EmotionFavour] 关系区间配置: {warning}")
        self._advance_items_cache = result
        return result

    def _find_tier(self, favour: int) -> Optional[dict]:
        """返回 favour 落在哪个 advance 区间（首个匹配），找不到返回 None。"""
        for item in self._advance_items():
            x = item.get("min_value", self.min_favour_value)
            y = item.get("max_value", self.max_favour_value)
            if x <= favour <= y:
                return item
        return None

    def _find_next_tier(self, favour: int) -> Optional[dict]:
        """返回 min_value > 当前 favour 的下一个更高等级（按 min_value 升序首个）。
        用于「临近解锁」预告。"""
        items = sorted(
            [i for i in self._advance_items() if i.get("min_value", 0) > favour],
            key=lambda i: i.get("min_value", 0),
        )
        return items[0] if items else None

    def _get_max_tier(self) -> Optional[dict]:
        """返回 advance 配置中 min_value 最高的等级（用于特殊关系覆盖）。无配置返回 None。"""
        items = self._advance_items()
        if not items:
            return None
        return max(items, key=lambda i: i.get("min_value", self.min_favour_value))

    def _is_special_override(self, user_id: str) -> bool:
        """是否走特殊关系覆盖路径（特殊关系非空 + ID 在显式列表中）。"""
        return (
            bool(self.admin_default_relationship)
            and bool(user_id)
            and str(user_id) in self._special_user_ids
        )

    def _get_tier_extras(self, favour: int, user_id: str = "") -> dict:
        """取当前好感度对应等级的 boundary / rule / preview / next_describe / is_max_tier。
        供对话注入与后台结算共用。返回 dict，缺字段为空字符串/False。

        特殊关系覆盖：若 user_id 位于显式特殊用户列表，按「最高等级」取 extras——
        boundary/rule/preview 用最高等级的、is_max_tier=True、next_describe=""。
        这样对话注入走最高等级边界，但 _get_relationship 仍返回 admin_default_relationship 名称。
        """
        if self._is_special_override(user_id):
            current = self._get_max_tier()
            next_tier = None  # 特殊用户按最高等级处理，无更高等级
        else:
            current = self._find_tier(favour)
            next_tier = self._find_next_tier(favour)
        if not current:
            return {
                "boundary": "", "preview": "", "rule": "",
                "next_describe": "", "is_max_tier": False,
            }
        return {
            "boundary": (current.get("boundary") or "").strip(),
            "preview": (current.get("preview") or "").strip(),
            "rule": (current.get("rule") or "").strip(),
            "next_describe": (next_tier.get("describe") or "").strip() if next_tier else "",
            "is_max_tier": next_tier is None,
        }

    async def _get_initial_favour(self, event: AstrMessageEvent) -> int:
        user_id = event.get_sender_id()
        is_envoy = str(user_id) in self.favour_envoys
        base = self.admin_default_favour if is_envoy else self.default_favour
        return max(self.min_favour_value, min(self.max_favour_value, base))

    async def _get_initial_favour_for(self, event: AstrMessageEvent, user_id: str) -> int:
        """对任意 user_id 计算初始好感度（用于第三方目标 B 首次落库时的起算值）。
        与 _get_initial_favour 一致：仅显式特殊用户 → admin_default_favour，否则 default_favour。
        """
        is_envoy = str(user_id) in self.favour_envoys
        base = self.admin_default_favour if is_envoy else self.default_favour
        return max(self.min_favour_value, min(self.max_favour_value, base))

    def _decay_favour_value(self, record) -> int:
        """对 record.favour 应用 lazy 衰减，返回 transient 数值（不写库）。
        若衰减关闭或 record 为 None，直接返回原值。"""
        if not record:
            return 0
        if not self.favour_decay_enabled:
            return record.favour
        return compute_favour_decay(
            record.favour, record.updated_at,
            anchor=self.favour_decay_anchor,
        )

    # ================= 排序 & T2I =================

    async def _sort_records(self, event: AstrMessageEvent, records: List[FavourRecord]) -> List[FavourRecord]:
        if not records:
            return []
        if self.group_sort_by == "favour":
            return sorted(records, key=lambda x: self._decay_favour_value(x), reverse=True)
        elif self.group_sort_by == "userid":
            return sorted(records, key=lambda x: x.user_id)
        elif self.group_sort_by == "nickname":
            enriched = [(await get_user_display_name(event, r.user_id), r) for r in records]
            enriched.sort(key=lambda x: x[0].lower())
            return [x[1] for x in enriched]
        else:
            return sorted(records, key=lambda x: x.created_at if x.created_at else datetime.min)

    _SCRIPT_URLS = {
        "marked.min.js": "https://cdn.jsdelivr.net/npm/marked/marked.min.js",
        "highlight.min.js": "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js",
    }

    _CDN_TO_FILE = {
        "https://cdn.jsdelivr.net/npm/marked/marked.min.js": "marked.min.js",
        "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js": "highlight.min.js",
    }

    @staticmethod
    def _download_script_sync(url: str, filepath: Path) -> None:
        import urllib.request

        request = urllib.request.Request(url, headers={"User-Agent": "emotion_favour/3.4"})
        with urllib.request.urlopen(request, timeout=15) as response:
            data = response.read()
        filepath.write_bytes(data)

    async def _ensure_local_scripts(self):
        scripts_dir = self.data_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        for filename, url in self._SCRIPT_URLS.items():
            filepath = scripts_dir / filename
            if not filepath.exists():
                try:
                    await asyncio.to_thread(self._download_script_sync, url, filepath)
                    logger.info(f"[EmotionFavour] 已下载本地脚本: {filename}")
                except Exception as e:
                    logger.warning(f"[EmotionFavour] 下载脚本 {filename} 失败: {e}")
                    continue
            try:
                self._local_scripts[filename] = await asyncio.to_thread(
                    filepath.read_text, encoding="utf-8"
                )
            except Exception as e:
                logger.warning(f"[EmotionFavour] 读取本地脚本 {filename} 失败: {e}")

    async def _download_missing_scripts(self, scripts: list[tuple[str, str]]):
        scripts_dir = self.data_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        for cdn_url, filename in scripts:
            if filename in self._local_scripts:
                continue
            filepath = scripts_dir / filename
            try:
                await asyncio.to_thread(self._download_script_sync, cdn_url, filepath)
                self._local_scripts[filename] = await asyncio.to_thread(
                    filepath.read_text, encoding="utf-8"
                )
                logger.info(f"[EmotionFavour] 异步下载完成: {filename}")
            except Exception as e:
                logger.warning(f"[EmotionFavour] 异步下载脚本 {filename} 失败: {e}")

    async def _start_browser(self):
        """启动一套新的 Playwright driver/browser，失败时回收半初始化资源。"""
        from playwright.async_api import async_playwright

        if self._pw_instance is not None:
            await self._pw_instance.stop()
        self._pw_instance = await async_playwright().start()
        try:
            self._pw_browser = await self._pw_instance.chromium.launch()
        except Exception:
            await self._pw_instance.stop()
            self._pw_instance = None
            self._pw_browser = None
            raise
        return self._pw_browser

    async def _ensure_browser_locked(self, *, auto_install: bool = True):
        if self._pw_browser is None or not self._pw_browser.is_connected():
            try:
                await self._start_browser()
            except Exception as launch_error:
                if not auto_install or not is_missing_chromium_error(launch_error):
                    raise
                if self._chromium_install_attempted:
                    raise RuntimeError(
                        "Playwright Chromium 自动安装已在本次进程中尝试过，"
                        "浏览器仍不可用；请检查网络、磁盘权限和 Playwright 安装日志，"
                        "或在 AstrBot Python 环境手动执行 "
                        "`python -m playwright install chromium`"
                    ) from launch_error

                self._chromium_install_attempted = True
                logger.info(
                    "[EmotionFavour] Playwright Chromium 未安装，"
                    "正在使用当前 AstrBot Python 环境自动下载（首次可能耗时较长）"
                )
                await install_chromium()
                logger.info("[EmotionFavour] Playwright Chromium 自动安装完成，正在启动浏览器")
                await self._start_browser()
        return self._pw_browser

    async def _ensure_browser(self, *, auto_install: bool = True):
        async with self._browser_lock:
            return await self._ensure_browser_locked(auto_install=auto_install)

    async def _render_t2i(self, md_text: str, width: int = 800) -> str:
        try:
            return await self._render_custom_t2i(md_text, width=width)
        except Exception as e:
            logger.warning(
                "[EmotionFavour] 自定义 Playwright T2I 失败，"
                f"回退 AstrBot 内置 T2I: {type(e).__name__}: {e}"
            )
            return await self.text_to_image(md_text, return_url=False)

    async def _render_custom_t2i(self, md_text: str, width: int = 800) -> str:
        """使用插件自定义 Playwright 模板渲染；异常由上层统一回退。"""
        async with self._browser_lock:
            browser = await self._ensure_browser_locked()
            page = await browser.new_page(viewport={"width": width, "height": 600})
            try:
                html = self._html_template.replace("{{ version }}", self._plugin_version)
                safe_text = md_text.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
                html = html.replace("{{ text | safe }}", safe_text)
                html = html.replace("{{ max_width }}", str(width))

                # 用本地脚本替换 CDN 引用（fallback: 无本地则保留 CDN 并后台下载）
                missing_scripts = []
                for cdn_url, filename in self._CDN_TO_FILE.items():
                    cdn_tag = f'<script src="{cdn_url}"></script>'
                    if filename in self._local_scripts:
                        html = html.replace(cdn_tag, f"<script>{self._local_scripts[filename]}</script>")
                    else:
                        missing_scripts.append((cdn_url, filename))

                html = re.sub(r'<script[^>]*katex[^>]*>\s*</script>', '', html)
                if missing_scripts:
                    self._tasks.spawn(
                        self._download_missing_scripts(missing_scripts),
                        name="download_t2i_scripts",
                    )

                await page.set_content(html, wait_until="load", timeout=15000)
                digest_input = f"{self._plugin_version}|{width}|{md_text}".encode()
                filename = hashlib.md5(digest_input).hexdigest()[:12] + ".png"
                output_path = self._t2i_output_dir / filename
                await page.screenshot(path=str(output_path), full_page=True)
                return str(output_path)
            finally:
                await page.close()

    async def _send_chunked_t2i(self, event: AstrMessageEvent, title: str, headers: List[str], rows: List[str], chunk_size: int = 200, width: int = 800):
        total = len(rows)
        if total == 0:
            await event.send(event.plain_result(f"{title}\n暂无数据"))
            return
        for i in range(0, total, chunk_size):
            chunk = rows[i:i+chunk_size]
            page_info = f"({i+1}-{min(i+chunk_size, total)}/{total})" if total > chunk_size else ""
            md_lines = [f"# {title} {page_info}", ""] + headers + chunk
            md_text = "\n".join(md_lines)
            try:
                img_path = await self._render_t2i(md_text, width=width)
                await event.send(event.image_result(img_path))
            except Exception as e:
                logger.error(f"{self._tag(event)} 生成图片失败 (Page {page_info}): {e}")
                await event.send(event.plain_result("生成图片失败，请检查日志。"))

    # ================= Hook: 注入 =================

    _CTX_CLEAN_PATTERN = re.compile(
        r"(?:```(?:xml|text)?\s*)?<(?:thought|thinking)>[\s\S]*?</(?:thought|thinking)>(?:\s*```)?"
        r"|<情感好感[^>]*>[\s\S]*?</情感好感>",
        re.IGNORECASE | re.MULTILINE,
    )

    @filter.on_llm_request()
    async def inject_favour_prompt(self, event: AstrMessageEvent, req: ProviderRequest):
        """LLM请求前注入好感度面板与情感状态，并清洗历史上下文中的旧注入标记"""
        if not self.is_ready():
            return

        # 1. 清洗历史上下文：移除旧的 <情感好感> 注入和 <thought> 块
        if hasattr(req, "contexts") and isinstance(req.contexts, list):
            cleaned = []
            for ctx in req.contexts:
                if isinstance(ctx, dict) and "content" in ctx:
                    content = str(ctx["content"])
                    cleaned_content = self._CTX_CLEAN_PATTERN.sub("", content).strip()
                    if cleaned_content:
                        new_ctx = ctx.copy()
                        new_ctx["content"] = cleaned_content
                        cleaned.append(new_ctx)
                elif isinstance(ctx, str):
                    cleaned_str = self._CTX_CLEAN_PATTERN.sub("", ctx).strip()
                    if cleaned_str:
                        cleaned.append(cleaned_str)
                else:
                    cleaned.append(ctx)
            req.contexts = cleaned

        # 2. 注入最新情感好感状态
        try:
            user_id = event.get_sender_id()
            persona_id = await self._get_persona_id(event)

            record = await self.db.get_favour(persona_id, user_id)
            if record:
                current_favour = self._decay_favour_value(record)
                if current_favour != record.favour:
                    logger.debug(
                        f"{self._tag(event)} 好感度衰减生效: user={user_id} "
                        f"{record.favour}->{current_favour}"
                    )
            else:
                current_favour = await self._get_initial_favour(event)
                record = FavourRecord(persona_id=persona_id, user_id=user_id, favour=current_favour)
                logger.info(
                    f"{self._tag(event)} 新用户首次注入: user={user_id} "
                    f"persona={persona_id} initial_favour={current_favour}"
                )

            relationship = self._get_relationship(current_favour, user_id)
            favour_range = self._get_relationship_range(current_favour, user_id)
            tier_extras = self._get_tier_extras(current_favour, user_id) if self.relationship_mode == "advance" else None

            # SystemPrompt 通道：永不变的元规则 + 全档结构总览（提升指令权重 + 命中 prompt cache）
            sys_extra = build_system_prompt_extra(
                advance_items=self._advance_items() if self.relationship_mode == "advance" else None,
                relationship_mode=self.relationship_mode,
            )
            if sys_extra:
                req.system_prompt = (req.system_prompt or "") + "\n\n" + sys_extra

            # User append 通道：当前档位的动态状态（好感度数值、12 维情感、当前 boundary、进度）
            prompt_final = build_injection_prompt(
                record,
                relationship,
                favour_range,
                current_favour,
                tier_extras=tier_extras,
            )

            req.extra_user_content_parts.append(TextPart(text=prompt_final).mark_as_temp())
            logger.debug(f"{self._tag(event)} 注入的印象上下文:\n{prompt_final}")
        except Exception as e:
            logger.error(f"{self._tag(event)} 注入印象上下文失败: {str(e)}\n{traceback.format_exc()}")

    # ================= Hook: 后台结算 =================

    @filter.after_message_sent(priority=10)
    async def evaluate_favour(self, event: AstrMessageEvent):
        """AstrBot 完成发送尝试后，排队结算好感度与情感变化。"""
        if not self.is_ready():
            return
        res = event.get_result()
        if res is None or not res.is_llm_result():
            return
        user_text = event.message_str
        bot_reply = "".join(comp.text for comp in res.chain if isinstance(comp, Plain))
        if not bot_reply.strip() or not user_text.strip():
            return
        self._tasks.spawn(
            self._run_settlement_serialized(event, user_text, bot_reply),
            name="settlement",
        )

    async def _run_settlement_serialized(
        self,
        event: AstrMessageEvent,
        user_text: str,
        bot_reply: str,
    ) -> None:
        user_id = str(event.get_sender_id() or "")
        persona_id = await self._get_persona_id(event)
        record_key = self._record_key(persona_id, user_id)
        if persona_id in self._blocked_personas or record_key in self._blocked_records:
            return
        async with self._record_locks.hold(record_key):
            if persona_id in self._blocked_personas or record_key in self._blocked_records:
                return
            await self._calculate_favour_bg(
                event,
                user_text,
                bot_reply,
                persona_id=persona_id,
            )

    @filter.on_decorating_result(priority=15)
    async def _on_session_reset(self, event: AstrMessageEvent):
        """检测 /reset 或 /new（通过 AstrBot 内置 _clean_group_context_session 标记），
        重置当前人格下所有用户的情感维度。好感度保留，仅情感归零。
        """
        if not event.get_extra("_clean_group_context_session"):
            return

        try:
            persona_id = await self._get_persona_id(event)
            records = await self.db.get_global_records(persona_id)
            zero_emotions = {dim: 0 for dim in EMOTION_DIMENSIONS}
            count = 0
            for record in records:
                async with self._record_locks.hold(
                    self._record_key(persona_id, record.user_id)
                ):
                    if await self.db.set_record_fields(
                        persona_id,
                        record.user_id,
                        emotions_absolute=zero_emotions,
                    ):
                        count += 1
            if count > 0:
                logger.info(f"{self._tag(event)} 检测到会话重置/新建，已重置人格 {persona_id} 下 {count} 位用户的情感维度")
        except Exception as e:
            logger.warning(f"{self._tag(event)} 会话重置情感批量重置失败: {e}")

    async def _calculate_favour_bg(
        self,
        event: AstrMessageEvent,
        user_text: str,
        bot_reply: str,
        *,
        persona_id: Optional[str] = None,
    ):
        try:
            user_id = event.get_sender_id()
            umo = event.unified_msg_origin
            persona_id = persona_id or await self._get_persona_id(event)

            # 提前获取裁决模型（供人设摘要提取和裁决共用）
            if self.judge_provider:
                provider_id = self.judge_provider.strip()
            else:
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            if not provider_id:
                logger.warning(f"{self._tag(event)} 未找到可用的 LLM Provider 进行印象结算")
                return

            # 获取人设性格摘要
            persona_summary = await self._get_persona_summary(persona_id, provider_id)
            persona_section = f"【角色性格】\n{persona_summary}\n\n" if persona_summary else ""

            if self.favour_mode == "galgame":
                mode_rule = (
                    "当前为 GALGAME 模式，严格执行以下规则：\n"
                    "- 善意互动（礼貌、关心、赞美）大概率触发好感上升，幅度乐观判定\n"
                    "- 轻微失礼或不当玩笑宽容处理，多数情况不下降，仅给予中性（0）\n"
                    "- 好感度越高，角色对用户的包容度越高，容易进一步上升\n"
                    "- 只有明确的恶意攻击或严重冒犯才触发下降"
                )
            elif self.favour_mode == "normal":
                mode_rule = (
                    "当前为 NORMAL 模式，严格执行以下规则：\n"
                    "- 善意且有内容的互动正常触发好感上升，但表面客套不足以构成上升理由\n"
                    "- 一般失礼行为（不当玩笑、轻度冒犯）触发小幅下降\n"
                    "- 严重冒犯或反复越界触发中幅下降\n"
                    "- 好感度变化幅度居中，既不苛刻也不宽容，符合一般社交直觉"
                )
            else:
                mode_rule = (
                    "当前为 REALISTIC 模式，严格执行以下规则：\n"
                    "- 好感度上升极其困难，普通礼貌用语不构成上升理由，只有持续、真诚、深入的善意互动才可触发微幅上升（+1）\n"
                    "- 任何越界、冒犯、无礼、轻浮、自以为是的行为一律下降，且惩罚幅度加倍\n"
                    "- 已有正面印象可以被一次严重失礼行为大幅削弱\n"
                    "- 默认对陌生互动保持警惕与距离感，不因表面客套而改变态度"
                )

            emotion_dim_cn = ", ".join(f"{EMOTION_DISPLAY_NAMES[k]}({k})" for k in EMOTION_DIMENSIONS)

            # 获取当前会话 CID，用于对话历史读取
            curr_cid = ""
            if self.history_rounds > 0:
                try:
                    conv_mgr = self.context.conversation_manager
                    curr_cid = await conv_mgr.get_curr_conversation_id(umo) or ""
                except Exception:
                    pass

            record = await self.db.get_favour(persona_id, user_id)
            if record:
                # 情感时间衰减
                if self.emotion_decay_enabled:
                    decayed, elapsed_hours = apply_emotion_decay(
                        record,
                        self.emotion_decay_rate_volatile,
                        self.emotion_decay_rate_standard,
                        self.emotion_decay_rate_sticky,
                        self.emotion_decay_min_hours,
                    )
                    if decayed:
                        decay_deltas = {dim: nv - getattr(record, dim) for dim, nv in decayed.items()}
                        await self.db.update_favour(persona_id, user_id, emotion_updates=decay_deltas)
                        record = await self.db.get_favour(persona_id, user_id)
                        logger.info(f"{self._tag(event)} 情感衰减: 用户 {user_id}, {elapsed_hours:.1f}h, {list(decayed.keys())}")
                current_favour = self._decay_favour_value(record)
                emotion_panel = build_emotion_panel(record)
            else:
                current_favour = await self._get_initial_favour(event)
                emotion_panel = "[喜悦:0] [信任:0] [恐惧:0] [惊讶:0] [悲伤:0] [厌恶:0] [愤怒:0] [期待:0] [得意:0] [内疚:0] [害羞:0] [嫉妒:0]"

            # 只取当前好感值对应的那一条关系规则
            current_rule = ""
            if self.relationship_mode == "advance":
                extras = self._get_tier_extras(current_favour, user_id)
                if extras["rule"]:
                    if self._is_special_override(user_id):
                        desc = self.admin_default_relationship
                    else:
                        tier = self._find_tier(current_favour)
                        desc = (tier.get("describe") if tier else "") or "未知"
                    current_rule = f"【当前关系：{desc}】\n{extras['rule']}\n\n"

            # 获取近期对话历史
            history_section = ""
            if self.history_rounds > 0 and curr_cid:
                history_text = await self._get_recent_history(
                    umo,
                    user_id,
                    curr_cid,
                    bot_reply,
                    persona_id=persona_id,
                )
                if history_text:
                    history_section = f"【近期对话】\n{history_text}\n\n"

            eval_prompt = (
                "<role>你是后台印象与情感结算处理器，根据当前状态、关系和互动评估变化。</role>\n\n"
                f"{persona_section}"
                f"【当前状态】好感度:{current_favour} 情感:{emotion_panel}\n"
                f"{current_rule}"
                f"{history_section}"
                f"【互动】\n用户: {user_text}\n角色: {bot_reply}\n\n"
                f"{mode_rule}\n\n"
                "<rule>\n"
                f"好感度: {self.favour_change_min}~+{self.favour_change_max}，善意互动为正、冒犯为负、普通为0\n"
                f"情感维度: 每维 {self.emotion_change_min}~+{self.emotion_change_max}，无变化不列出\n"
                "代谢: (1)负面消解 — 存在[愤怒/悲伤/厌恶/嫉妒]且互动良好时必须扣除相应数值；(2)激情冷却 — [惊讶/害羞/恐惧]属瞬时情绪，平稳后必须回落\n"
                "</rule>\n"
                "<ban>打招呼/闲聊不触发好感变化；禁止无理由大幅波动</ban>\n\n"
                f"可用维度: {emotion_dim_cn}\n\n"
                "<output>只输出XML，禁止额外文字：\n"
                "<result><reasoning>分析</reasoning><change>0</change><emotions><joy>2</joy></emotions></result>\n"
                "emotions内只列有变化的维度标签，无变化时省略emotions。</output>\n\n"
                "<example>\n"
                "用户: 昨天对不起，我不是故意迟到的\n角色: 没关系啦，你能来就好\n"
                "<result><reasoning>用户主动道歉有诚意，负面情绪应消解，信任上升</reasoning><change>1</change><emotions><guilt>-3</guilt><joy>2</joy><trust>1</trust></emotions></result>\n"
                "</example>\n\n"
                "XML："
            )

            logger.debug(f"{self._tag(event)} 印象结算上下文:\n{eval_prompt}")

            resp = await self._judge_generate(
                provider_id=provider_id,
                prompt=eval_prompt,
            )
            result_text = resp.completion_text

            # 解析 XML 响应
            result_cleaned = re.sub(r'```(?:xml)?\s*', '', result_text).strip()
            xml_match = re.search(r'<result>.*?</result>', result_cleaned, re.DOTALL)
            if not xml_match:
                logger.warning(f"{self._tag(event)} 印象结算未在模型回复中找到 XML: {result_text}")
                return
            try:
                import xml.etree.ElementTree as ET
                root = ET.fromstring(xml_match.group(0))
                data = {
                    "reasoning": (root.findtext("reasoning") or "").strip(),
                    "change": (root.findtext("change") or "0").strip(),
                    "emotions": {}
                }
                emotions_elem = root.find("emotions")
                if emotions_elem is not None:
                    for child in emotions_elem:
                        data["emotions"][child.tag.lower()] = (child.text or "0").strip()
            except Exception as e:
                logger.warning(f"{self._tag(event)} 印象结算 XML 解析失败: {result_text} ({e})")
                return

            delta = int(round(float(data.get("change", 0))))
            raw_emotions = data.get("emotions", {})
            reasoning = data.get("reasoning", "")
            if reasoning:
                logger.debug(f"{self._tag(event)} 印象结算推理: {reasoning}")

            if delta == 0 and not raw_emotions:
                # 即使裁决无变化，也要把当前衰减后的值落盘并刷新 updated_at，
                # 否则下次读取仍从旧 updated_at 起算 Δt，衰减会重复累积。
                record = await self.db.get_favour(persona_id, user_id)
                if record and self.favour_decay_enabled:
                    decayed_fav = self._decay_favour_value(record)
                    if decayed_fav != record.favour:
                        await self.db.update_favour(persona_id, user_id, favour=decayed_fav)
                        logger.info(f"{self._tag(event)} 印象结算无变化，刷新衰减基线 user={user_id} {record.favour}->{decayed_fav}")
                logger.debug(f"{self._tag(event)} 印象结算无变化 user={user_id}")
                return

            record = await self.db.get_favour(persona_id, user_id)
            old_fav = self._decay_favour_value(record) if record else await self._get_initial_favour(event)

            new_fav = max(self.min_favour_value, min(self.max_favour_value, old_fav + delta))

            clamped_emotions = {}
            for dim, value in raw_emotions.items():
                if dim in EMOTION_DIMENSIONS:
                    try:
                        clamped = max(self.emotion_change_min, min(self.emotion_change_max, int(value)))
                        if clamped != 0:
                            clamped_emotions[dim] = clamped
                    except (ValueError, TypeError):
                        continue

            # 对正向情感变化施加收益递减
            if record:
                for dim in list(clamped_emotions.keys()):
                    if clamped_emotions[dim] > 0:
                        current_val = getattr(record, dim, 0)
                        clamped_emotions[dim] = diminish_delta(current_val, clamped_emotions[dim])

            await self.db.update_favour(persona_id, user_id, favour=new_fav, emotion_updates=clamped_emotions if clamped_emotions else None)
            logger.info(f"{self._tag(event)} 用户 {user_id} 结算: 好感值 {old_fav}->{new_fav} (Δ{delta}){', 情感: ' + str(clamped_emotions) if clamped_emotions else ''}")
        except Exception as e:
            logger.error(f"{self._tag(event)} 印象后台结算出错: {str(e)}\n{traceback.format_exc()}")

    # ================= 人设摘要 =================

    async def _get_persona_summary(self, persona_id: str, provider_id: str) -> str:
        """获取人设性格摘要，不存在或人设变更时自动提取并缓存。"""
        persona_prompt = ""
        try:
            persona = self.persona_mgr.get_persona_v3_by_id(persona_id)
            if persona and "prompt" in persona:
                persona_prompt = persona["prompt"] or ""
        except Exception:
            pass

        if not persona_prompt.strip():
            return ""

        prompt_hash = hashlib.sha256(persona_prompt.encode("utf-8")).hexdigest()
        async with self._record_locks.hold(("persona_summary", persona_id)):
            cached = await self.db.get_persona_summary(persona_id)
            if cached and cached["hash"] == prompt_hash:
                return cached["summary"]

            summary = await self._extract_persona_summary(persona_prompt, provider_id)
            if summary:
                await self.db.save_persona_summary(persona_id, summary, prompt_hash)
                logger.info(f"[EmotionFavour] 人设摘要已提取: persona_id={persona_id}, {len(summary)}字")
            return summary

    async def _extract_persona_summary(self, persona_prompt: str, provider_id: str) -> str:
        """使用裁决模型从人设文本中提取性格摘要。"""
        extract_prompt = (
            "从以下角色人设文本中提取性格特征摘要。\n\n"
            "<要求>\n"
            "- 提取：性格类型、情感特征、对人的态度模式、喜好厌恶、典型行为倾向\n"
            "- 忽略对话示例和具体剧情\n"
            "- 控制在200字以内\n"
            "- 只输出摘要文本，无任何前缀、标注或解释\n"
            "</要求>\n\n"
            f"角色人设：\n{persona_prompt[:8000]}"
        )
        try:
            resp = await self._judge_generate(
                provider_id=provider_id,
                prompt=extract_prompt,
            )
            summary = (resp.completion_text or "").strip()
            return summary[:500] if summary else ""
        except Exception as e:
            logger.warning(f"[EmotionFavour] 人设摘要提取失败: {e}")
            return ""

    # ================= 指令组 =================

    @filter.command_group("emotion")
    def emotion_group(self):
        """情感与印象管理指令组。"""

    async def _query_favour_impl(
        self,
        event: AstrMessageEvent,
        target_uid: str,
    ):
        """查询指定用户；权限由公开入口在调用前处理。"""
        persona_id = await self._get_persona_id(event)
        record = await self.db.get_favour(persona_id, target_uid)
        if record:
            fav = self._decay_favour_value(record)
        else:
            sender_id = str(event.get_sender_id())
            fav = await self._get_initial_favour(event) if target_uid == sender_id else 0
            record = FavourRecord(persona_id=persona_id, user_id=target_uid, favour=fav)

        name = await get_user_display_name(event, target_uid)
        relationship = self._get_relationship(fav, target_uid)
        detail = format_emotion_detail(record, relationship, effective_favour=fav)

        md_text = f"# 印象查询\n\n**用户**：{escape_markdown(name)}  \n**ID**：{target_uid}\n\n---\n\n{detail}"
        try:
            img_path = await self._render_t2i(md_text)
            yield event.image_result(img_path)
        except Exception as e:
            logger.warning(f"{self._tag(event)} 印象查询 T2I 失败，回退纯文本: {e}")
            yield event.plain_result(f"🔍 用户：{name}\n🆔 ID：{target_uid}\n{detail}")

    @emotion_group.command("me")
    async def emotion_me(self, event: AstrMessageEvent):
        """查询自己的好感度与情感状态。"""
        async for result in self._query_favour_impl(
            event, str(event.get_sender_id()),
        ):
            yield result

    @filter.command("查询印象", alias={"印象", "查询好感度", "好感度"})
    async def legacy_query_self(self, event: AstrMessageEvent):
        """兼容旧入口：仅查询发送者本人。"""
        async for result in self._query_favour_impl(
            event, str(event.get_sender_id()),
        ):
            yield result

    @emotion_group.command("query")
    async def emotion_query(self, event: AstrMessageEvent, target: str):
        """Bot 管理员查询指定用户。"""
        if not await self._check_command_permission(event, "query"):
            yield event.plain_result("权限不足！只有 Bot 管理员可以查询他人的印象。")
            return
        target_uid = get_target_uid(event, target)
        if not target_uid:
            yield event.plain_result("未找到用户，请使用 @ 或输入 ID。")
            return
        async for result in self._query_favour_impl(event, target_uid):
            yield result

    @emotion_group.command("list")
    async def query_global_favour(self, event: AstrMessageEvent, page: int = 1):
        """分页查看当前人格下所有用户的好感度记录"""
        if not await self._check_command_permission(event, "list"):
            yield event.plain_result("权限不足！你无法使用此命令。")
            return

        persona_id = await self._get_persona_id(event)
        page_size = 20
        total_records = await self.db.count_records(persona_id)
        if total_records <= 0:
            yield event.plain_result("暂无印象记录。")
            return

        total_pages = (total_records + page_size - 1) // page_size
        if page < 1: page = 1
        if page > total_pages and total_pages > 0: page = total_pages

        if self.group_sort_by == "nickname":
            # 昵称来自当前平台查询，不是 favour_records 列；该模式保留兼容性回退。
            records = await self._sort_records(
                event, await self.db.get_global_records(persona_id)
            )
            page_records = records[(page - 1) * page_size:page * page_size]
        else:
            sort_map = {
                "favour": ("favour", "desc"),
                "userid": ("user_id", "asc"),
                "default": ("created_at", "asc"),
            }
            sort_by, sort_order = sort_map.get(
                self.group_sort_by, ("created_at", "asc")
            )
            page_records = await self.db.list_records(
                persona_id,
                offset=(page - 1) * page_size,
                limit=page_size,
                sort_by=sort_by,
                sort_order=sort_order,
            )
        uids = [r.user_id for r in page_records]
        resolved_names = await asyncio.gather(
            *(get_user_display_name(event, uid) for uid in uids),
            return_exceptions=True,
        )
        name_map = {
            uid: name
            for uid, name in zip(uids, resolved_names)
            if isinstance(name, str) and name and name != uid
        }
        headers = ["| 用户 | ID | 好感值 | 关系 | 主导情感 |", "| :--- | :--- | :---: | :---: | :--- |"]
        rows = []
        for r in page_records:
            decayed_fav = self._decay_favour_value(r)
            rel = escape_markdown(self._get_relationship(decayed_fav, r.user_id))
            top = get_dominant_emotions(r, 3)
            emotion_str = "、".join(f"{EMOTION_DISPLAY_NAMES[k]}({v})" for k, v in top) if top else "-"
            display_name = name_map.get(r.user_id) or ""
            rows.append(f"| {escape_markdown(display_name)} | {escape_markdown(r.user_id)} | {decayed_fav} | {rel} | {emotion_str} |")

        await self._send_chunked_t2i(event, f"📊 印象记录 - 第 {page}/{total_pages} 页", headers, rows, width=1200)

    # ================= 修改命令 =================

    @emotion_group.command("set")
    async def modify_favour(self, event: AstrMessageEvent, target: str, value: int):
        """修改指定用户的好感度数值"""
        if not await self._check_command_permission(event, "set"):
            yield event.plain_result("权限不足！你无法使用此命令。")
            return
        uid = get_target_uid(event, target)
        if not uid:
            yield event.plain_result("未找到用户，请使用 @ 或输入 ID。")
            return
        persona_id = await self._get_persona_id(event)
        try:
            if not await self.update_record(persona_id, uid, favour=value):
                raise RuntimeError("记录当前不可修改或数据库写入失败")
            yield event.plain_result(f"已将用户 {uid} 的好感值修改为 {value}。")
            logger.info(f"{self._tag(event)} 管理员 {event.get_sender_id()} 修改用户 {uid} 好感值为 {value}")
        except Exception as e:
            logger.error(f"{self._tag(event)} 修改印象失败: {e}")
            yield event.plain_result("修改失败，请检查日志。")

    @emotion_group.command("mood")
    async def modify_emotion(self, event: AstrMessageEvent, target: str, dimension: str = "", value: int = 0):
        """修改指定用户的单个或全部情感维度"""
        if not await self._check_command_permission(event, "mood"):
            yield event.plain_result("权限不足！你无法使用此命令。")
            return
        uid = get_target_uid(event, target)
        if not uid:
            yield event.plain_result("未找到用户，请使用 @ 或输入 ID。")
            return

        # 如果 dimension 看起来是数字，说明用户省略了维度名：/emotion mood 10001 80
        if dimension and not value and dimension.lstrip('-').isdigit():
            value = int(dimension)
            dimension = ""

        if value < 0 or value > 100:
            yield event.plain_result("数值越界！情感维度范围为 0~100。")
            return

        persona_id = await self._get_persona_id(event)
        record = await self.db.get_favour(persona_id, uid)
        create_favour = None
        if record is None:
            create_favour = await self._get_initial_favour_for(event, uid)

        if not dimension:
            # 未指定维度 → 对所有维度生效
            emotion_updates = {}
            for dim in EMOTION_DIMENSIONS:
                old_val = getattr(record, dim, 0) if record else 0
                delta = value - old_val
                if delta != 0:
                    emotion_updates[dim] = delta
            if not emotion_updates:
                yield event.plain_result(f"用户 {uid} 的所有情感维度已经是 {value}，无需修改。")
                return
            try:
                if not await self.update_record(
                    persona_id,
                    uid,
                    favour=create_favour,
                    emotion_updates=emotion_updates,
                ):
                    raise RuntimeError("记录当前不可修改或数据库写入失败")
                yield event.plain_result(f"已将用户 {uid} 的所有情感维度设置为 {value}。")
                logger.info(f"{self._tag(event)} 管理员 {event.get_sender_id()} 设置用户 {uid} 所有情感维度为 {value}")
            except Exception as e:
                logger.error(f"{self._tag(event)} 修改情感失败: {e}")
                yield event.plain_result("修改失败，请检查日志。")
            return

        # 指定了单个维度
        reverse_map = {v: k for k, v in EMOTION_DISPLAY_NAMES.items()}
        dim_key = dimension if dimension in EMOTION_DIMENSIONS else reverse_map.get(dimension)
        if not dim_key:
            available = ", ".join(f"{n}({k})" for k, n in EMOTION_DISPLAY_NAMES.items())
            yield event.plain_result(f"未知的情感维度：{dimension}\n可用维度: {available}")
            return

        old_val = getattr(record, dim_key, 0) if record else 0
        delta = value - old_val

        if delta == 0:
            cn = EMOTION_DISPLAY_NAMES[dim_key]
            yield event.plain_result(f"用户 {uid} 的{cn}已经是 {value}，无需修改。")
            return

        try:
            if not await self.update_record(
                persona_id,
                uid,
                favour=create_favour,
                emotion_updates={dim_key: delta},
            ):
                raise RuntimeError("记录当前不可修改或数据库写入失败")
            cn = EMOTION_DISPLAY_NAMES[dim_key]
            yield event.plain_result(f"已将用户 {uid} 的{cn}从 {old_val} 修改为 {value}。")
            logger.info(f"{self._tag(event)} 管理员 {event.get_sender_id()} 修改用户 {uid} 的 {dim_key}({cn}): {old_val}->{value}")
        except Exception as e:
            logger.error(f"{self._tag(event)} 修改情感失败: {e}")
            yield event.plain_result("修改失败，请检查日志。")

    # ================= 清空命令 =================

    @emotion_group.command("clear")
    async def clear_user_favour(self, event: AstrMessageEvent, target: str):
        """清空指定用户的好感度与情感数据（需二次确认，自动备份）"""
        if not await self._check_command_permission(event, "clear"):
            yield event.plain_result("权限不足！你无法使用此命令。")
            return
        uid = get_target_uid(event, target)
        if not uid:
            yield event.plain_result("未找到用户，请使用 @ 或输入 ID。")
            return
        persona_id = await self._get_persona_id(event)
        yield event.plain_result(f"⚠️ 警告：即将清空用户 {uid} 的印象数据。\n请在 30 秒内回复「确认清空」以继续，回复其他内容取消。")

        @session_waiter(timeout=30, record_history_chains=False)
        async def confirm_waiter(controller: SessionController, evt: AstrMessageEvent):
            # 忽略空消息（"正在输入"等 lifecycle 事件）和非文本
            msg = (evt.message_str or "").strip()
            if not msg:
                return
            if msg == "确认清空":
                record_key = self._record_key(persona_id, uid)
                self._blocked_records.add(record_key)
                try:
                    async with self._record_locks.hold(record_key):
                        record = await self.db.get_favour(persona_id, uid)
                        if record:
                            backup_file = await self.db.backup_data([record], f"backup_user_{uid}")
                            await self.db.delete_favour(persona_id, uid)
                            await evt.send(evt.plain_result(f"✅ 已清空用户 {uid} 的印象数据。"))
                            logger.info(f"{self._tag(event)} 管理员 {evt.get_sender_id()} 清空了用户 {uid} 的印象数据\n备份: {backup_file}")
                        else:
                            await evt.send(evt.plain_result("该用户无印象记录。"))
                finally:
                    self._blocked_records.discard(record_key)
            else:
                await evt.send(evt.plain_result("已取消清空操作。"))
            controller.stop()

        try:
            await confirm_waiter(event, session_filter=SenderSessionFilter(event.get_sender_id()))
        except TimeoutError:
            yield event.plain_result("操作超时，已取消清空。")
        finally:
            event.stop_event()

    @emotion_group.command("clear-all")
    async def clear_all_favour(self, event: AstrMessageEvent):
        """清空当前人格下所有用户的好感度数据（需二次确认，自动备份）"""
        if not await self._check_command_permission(event, "clear-all"):
            yield event.plain_result("权限不足！你无法使用此命令。")
            return
        persona_id = await self._get_persona_id(event)
        yield event.plain_result("🚨 极度危险：即将清空当前人格下【所有】印象数据！\n请在 30 秒内回复「确认清空所有数据」以继续，回复其他内容取消。")

        @session_waiter(timeout=30, record_history_chains=False)
        async def confirm_waiter(controller: SessionController, evt: AstrMessageEvent):
            msg = (evt.message_str or "").strip()
            if not msg:
                return
            if msg == "确认清空所有数据":
                self._blocked_personas.add(persona_id)
                try:
                    records = await self.db.get_global_records(persona_id)
                    if records:
                        backup_file = await self.db.backup_data(records, "backup_all_database")
                        # 按固定顺序等待已在执行的单用户结算退出，再逐条删除。
                        for record in sorted(records, key=lambda item: item.user_id):
                            async with self._record_locks.hold(
                                self._record_key(persona_id, record.user_id)
                            ):
                                await self.db.delete_favour(persona_id, record.user_id)
                        await evt.send(evt.plain_result("✅ 已清空当前人格下所有印象数据。"))
                        logger.warning(f"{self._tag(event)} Bot管理员 {evt.get_sender_id()} 清空了人格 {persona_id} 的所有印象数据\n备份: {backup_file}")
                    else:
                        await evt.send(evt.plain_result("数据库中无印象记录。"))
                finally:
                    self._blocked_personas.discard(persona_id)
            else:
                await evt.send(evt.plain_result("已取消清空操作。"))
            controller.stop()

        try:
            await confirm_waiter(event, session_filter=SenderSessionFilter(event.get_sender_id()))
        except TimeoutError:
            yield event.plain_result("操作超时，已取消清空。")
        finally:
            event.stop_event()

    # ================= 帮助命令 =================

    @emotion_group.command("help")
    async def help_menu(self, event: AstrMessageEvent):
        """按当前权限显示 emotion 指令帮助。"""
        msg = [
            "⭐ emotion 指令帮助 ⭐",
            "",
            "[个人查询]",
            "- /emotion me",
            "- 兼容入口：/查询印象、/印象、/查询好感度、/好感度",
        ]

        if self._is_bot_admin(event):
            msg.extend([
                "",
                "[Bot 管理员]",
                "- /emotion query <@用户或ID>",
                "- /emotion list [页码]",
                "- /emotion set <@用户或ID> <好感度>",
                "- /emotion mood <@用户或ID> [维度] <数值>",
                "- /emotion clear <@用户或ID>",
                "- /emotion clear-all",
                "- /emotion persona",
                "- /emotion persona-clear",
                "",
                "示例：",
                "- /emotion query 10001",
                "- /emotion set 10001 60",
                "- /emotion mood 10001 喜悦 80",
            ])

        md_text = "\n".join(msg)
        try:
            img_path = await self._render_t2i(md_text)
            yield event.image_result(img_path)
        except Exception as e:
            logger.warning(f"{self._tag(event)} 帮助菜单 T2I 失败，回退纯文本: {e}")
            yield event.plain_result(md_text)

    # ================= 人设摘要管理（仅管理员） =================

    @emotion_group.command("persona")
    async def cmd_view_persona_summary(self, event: AstrMessageEvent):
        """查看当前人格的AI提取性格摘要"""
        if not await self._check_command_permission(event, "persona"):
            yield event.plain_result("权限不足！只有 Bot 管理员可以查看印象人设摘要。")
            return
        persona_id = await self._get_persona_id(event)
        cached = await self.db.get_persona_summary(persona_id)
        if cached and cached["summary"]:
            yield event.plain_result(f"当前人设摘要 (persona_id={persona_id}):\n{cached['summary']}")
        else:
            yield event.plain_result(f"当前无人设摘要 (persona_id={persona_id})")

    @emotion_group.command("persona-clear")
    async def cmd_delete_persona_summary(self, event: AstrMessageEvent):
        """清除当前人格的AI提取性格摘要缓存"""
        if not await self._check_command_permission(event, "persona-clear"):
            yield event.plain_result("权限不足！只有 Bot 管理员可以清除印象人设摘要。")
            return
        persona_id = await self._get_persona_id(event)
        await self.db.delete_persona_summary(persona_id)
        yield event.plain_result(f"已清除人设摘要 (persona_id={persona_id})")
