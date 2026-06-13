# main.py
import re
import json
import random
import asyncio
import traceback
import hashlib
from pathlib import Path
from typing import List
from datetime import datetime

from astrbot.api import logger
from astrbot.api.star import Star, Context
from astrbot.api import AstrBotConfig
from astrbot.api.provider import ProviderRequest
from astrbot.api.event import filter
from astrbot.core.message.components import Plain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.agent.message import TextPart
from astrbot.core.utils.session_waiter import session_waiter, SessionController

from .permissions import PermLevel, PermissionManager
from .storage import (
    FavourDBManager, FavourRecord,
    EMOTION_DIMENSIONS,
    EMOTION_DISPLAY_NAMES,
    build_emotion_panel,
    build_injection_prompt,
    format_emotion_detail,
    get_dominant_emotions,
    diminish_delta,
    apply_emotion_decay,
)
from .utils import (
    get_target_uid,
    escape_markdown,
    get_user_display_name,
)


class EmotionFavourPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        # 基础配置
        self.favour_mode = config.get("favour_mode", "galgame")
        self.group_sort_by = config.get("group_sort_by", "default")
        self.min_favour_value = config.get("min_favour_value", -100)
        self.max_favour_value = config.get("max_favour_value", 100)
        self.default_favour = config.get("default_favour", 0)

        # 关系映射
        rel_conf = config.get("relationship_config", {})
        self.relationship_mode = rel_conf.get("mode", "simple")
        self.relationship_simple_list = rel_conf.get("simple_list", ["极度厌恶", "厌恶", "反感", "普通", "喜欢", "亲密", "挚爱"])
        self.relationship_advance_raw = rel_conf.get("advance_config", "")

        # 高级配置
        adv_conf = config.get("advanced_config", {})
        self.admin_default_favour = adv_conf.get("admin_default_favour", 50)
        self.admin_default_relationship = adv_conf.get("admin_default_relationship", "")
        self.favour_envoys = adv_conf.get("favour_envoys", [])
        self.favour_change_min = adv_conf.get("favour_change_min", -5)
        self.favour_change_max = adv_conf.get("favour_change_max", 5)
        self.emotion_change_min = adv_conf.get("emotion_change_min", -10)
        self.emotion_change_max = adv_conf.get("emotion_change_max", 5)
        self.perm_level_threshold = adv_conf.get("level_threshold", 50)

        # 命令权限
        self.member_commands = adv_conf.get("member_commands", ["查询印象", "印象帮助", "印象指令帮助"])
        self.high_commands = adv_conf.get("high_commands", [])
        self.admin_commands = adv_conf.get("admin_commands", ["修改印象"])
        self.owner_commands = adv_conf.get("owner_commands", ["清空印象"])
        self.superuser_commands = adv_conf.get("superuser_commands", ["查询全局印象", "清空全局印象"])
        self.query_others_favour_level = adv_conf.get("query_others_favour_level", "群管理员")

        # 裁判模型
        self.judge_provider = config.get("judge_provider", "")

        # 情感衰减
        self.emotion_decay_enabled = adv_conf.get("emotion_decay_enabled", True)
        self.emotion_decay_rate = adv_conf.get("emotion_decay_rate", 0.85)
        self.emotion_decay_min_hours = max(0.1, adv_conf.get("emotion_decay_min_hours", 1.0))

        # 对话历史轮数
        self.history_rounds = max(0, min(10, adv_conf.get("history_rounds", 0)))

        # chat_memory 集成
        self.use_chat_memory = adv_conf.get("use_chat_memory", False)
        self._chat_memory_query = None
        if self.use_chat_memory:
            try:
                from chat_memory.main import query_history as _cm_query
                self._chat_memory_query = _cm_query
                logger.info("[EmotionFavour] 已启用 chat_memory 对话历史集成")
            except ImportError:
                logger.warning("[EmotionFavour] 未找到 chat_memory 插件，回退到 AstrBot 自带上下文")
                self.use_chat_memory = False

        # 日志配置
        log_conf = config.get("log_config", {})
        self.log_with_bot_id = log_conf.get("log_with_bot_id", False)
        self.debug_to_info = log_conf.get("debug_to_info", False)

        self._validate_config()

        # 权限管理器
        self.admins_id = context.get_config().get("admins_id", [])
        self.perm_mgr = PermissionManager(
            superusers=self.admins_id,
            level_threshold=self.perm_level_threshold,
        )

        # 特权用户
        self._privileged_user_ids = set(self.admins_id) | set(str(e) for e in self.favour_envoys)

        # 人设管理器
        self.persona_mgr = context.persona_manager

        # 数据库
        self.data_dir = Path(context.get_config().get("plugin.data_dir", "./data")) / "plugin_data" / "emotion_favour"
        self.db = FavourDBManager(self.data_dir, self.min_favour_value, self.max_favour_value)

        # 连续下降衰减：key = "persona_id:user_id", value = 连续下降次数
        self._consecutive_decreases: dict[str, int] = {}

        # Playwright T2I
        self._pw_instance = None
        self._pw_browser = None
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

        asyncio.create_task(self._init_storage())

    def _validate_config(self):
        if self.min_favour_value >= self.max_favour_value:
            self.min_favour_value = -100
            self.max_favour_value = 100
        self.default_favour = max(self.min_favour_value, min(self.max_favour_value, self.default_favour))
        self.admin_default_favour = max(self.min_favour_value, min(self.max_favour_value, self.admin_default_favour))

    def _tag(self, event=None) -> str:
        if self.log_with_bot_id and event is not None:
            try:
                return f"[EmotionFavour:{event.get_platform_id()}]"
            except Exception:
                pass
        return "[EmotionFavour]"

    def _log_debug(self, msg: str):
        if self.debug_to_info:
            logger.info(msg)
        else:
            logger.debug(msg)

    async def _init_storage(self):
        try:
            await self.db.init_db()
        except Exception as e:
            logger.error(f"[EmotionFavour] 数据库初始化失败: {str(e)}\n{traceback.format_exc()}")
        self._ensure_local_scripts()
        try:
            await self._ensure_browser()
            logger.info("[EmotionFavour] Playwright 浏览器预热完成")
        except Exception as e:
            logger.warning(f"[EmotionFavour] Playwright 浏览器预热失败: {e}")

    # ================= 对话历史提取 =================

    async def _get_recent_history(self, umo: str, user_id: str, conversation_id: str, current_bot_reply: str = "") -> str:
        """获取最近 N 轮对话历史。

        启用 chat_memory 时从 chat_memory 读取（按 user_id 隔离）；
        否则从 AstrBot 自带上下文 conv.history 读取（群聊共享）。
        """
        if self.history_rounds <= 0 or not conversation_id:
            return ""

        try:
            if self.use_chat_memory and self._chat_memory_query:
                records = await self._chat_memory_query(umo, conversation_id, user_id, limit=self.history_rounds * 2)
            else:
                records = await self._read_astrbot_history(umo, conversation_id)
        except Exception as e:
            logger.warning(f"[EmotionFavour] 获取对话历史失败: {e}")
            return ""

        if not records:
            return ""

        # 去重：若末尾 assistant 等于当前 bot_reply，丢弃最后一对（避免当前轮次重复）
        if current_bot_reply and len(records) >= 2 and records[-1].get("role") == "assistant":
            if records[-1].get("content", "").strip() == current_bot_reply.strip():
                records = records[:-2]

        # 只保留最近 N 轮
        records = records[-(self.history_rounds * 2):]

        lines = []
        for i in range(0, len(records) - 1, 2):
            idx = i // 2 + 1
            user_msg = records[i].get("content", "")
            bot_msg = records[i + 1].get("content", "")
            lines.append(f"  [{idx}] 用户: {user_msg}")
            lines.append(f"  [{idx}] 角色: {bot_msg}")
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
        umo = event.unified_msg_origin
        persona = await self.persona_mgr.get_default_persona_v3(umo)
        return persona["name"] if persona and persona.get("name") else "default"

    async def _check_command_permission(self, event: AstrMessageEvent, command_name: str) -> bool:
        user_id = event.get_sender_id()
        if user_id in self.admins_id:
            role = PermLevel.SUPERUSER
        elif isinstance(event, AiocqhttpMessageEvent):
            role = await self.perm_mgr.get_perm_level(event, user_id)
        else:
            role = PermLevel.MEMBER

        allowed = set()
        if role >= PermLevel.MEMBER:
            allowed.update(self.member_commands)
        if role >= PermLevel.HIGH:
            allowed.update(self.high_commands)
        if role >= PermLevel.ADMIN:
            allowed.update(self.admin_commands)
        if role >= PermLevel.OWNER:
            allowed.update(self.owner_commands)
        if role >= PermLevel.SUPERUSER:
            allowed.update(self.superuser_commands)
        return command_name in allowed

    _LEVEL_NAME_MAP = {
        "普通成员": PermLevel.MEMBER, "高等级群员": PermLevel.HIGH,
        "群管理员": PermLevel.ADMIN, "群主": PermLevel.OWNER, "Bot管理员": PermLevel.SUPERUSER,
    }

    async def _get_user_perm_level(self, event: AstrMessageEvent) -> int:
        user_id = event.get_sender_id()
        if user_id in self.admins_id:
            return PermLevel.SUPERUSER
        if isinstance(event, AiocqhttpMessageEvent):
            return await self.perm_mgr.get_perm_level(event, user_id)
        return PermLevel.MEMBER

    def _get_relationship(self, favour: int, user_id: str = "") -> str:
        if self.admin_default_relationship and user_id and user_id in self._privileged_user_ids:
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
            try:
                items = json.loads(self.relationship_advance_raw) if isinstance(self.relationship_advance_raw, str) else self.relationship_advance_raw
                for item in items:
                    if item.get("min_value", self.min_favour_value) <= favour <= item.get("max_value", self.max_favour_value):
                        return item.get("describe", "未知")
            except (json.JSONDecodeError, TypeError):
                pass
            return "未知"

    async def _get_initial_favour(self, event: AstrMessageEvent) -> int:
        user_id = event.get_sender_id()
        is_envoy = str(user_id) in [str(e) for e in self.favour_envoys]
        is_admin = await self.perm_mgr.check_permission(event, PermLevel.OWNER)
        base = self.admin_default_favour if (is_envoy or is_admin) else self.default_favour
        return max(self.min_favour_value, min(self.max_favour_value, base))

    # ================= 排序 & T2I =================

    async def _sort_records(self, event: AstrMessageEvent, records: List[FavourRecord]) -> List[FavourRecord]:
        if not records:
            return []
        if self.group_sort_by == "favour":
            return sorted(records, key=lambda x: x.favour, reverse=True)
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

    def _ensure_local_scripts(self):
        import urllib.request
        scripts_dir = self.data_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        for filename, url in self._SCRIPT_URLS.items():
            filepath = scripts_dir / filename
            if not filepath.exists():
                try:
                    urllib.request.urlretrieve(url, str(filepath))
                    logger.info(f"[EmotionFavour] 已下载本地脚本: {filename}")
                except Exception as e:
                    logger.warning(f"[EmotionFavour] 下载脚本 {filename} 失败: {e}")
                    continue
            try:
                self._local_scripts[filename] = filepath.read_text(encoding="utf-8")
            except Exception:
                pass

    async def _download_missing_scripts(self, scripts: list[tuple[str, str]]):
        import urllib.request
        scripts_dir = self.data_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_event_loop()
        for cdn_url, filename in scripts:
            if filename in self._local_scripts:
                continue
            filepath = scripts_dir / filename
            try:
                await loop.run_in_executor(None, lambda: urllib.request.urlretrieve(cdn_url, str(filepath)))
                self._local_scripts[filename] = filepath.read_text(encoding="utf-8")
                logger.info(f"[EmotionFavour] 异步下载完成: {filename}")
            except Exception as e:
                logger.warning(f"[EmotionFavour] 异步下载脚本 {filename} 失败: {e}")

    async def _ensure_browser(self):
        if self._pw_browser is None or not self._pw_browser.is_connected():
            from playwright.async_api import async_playwright
            if self._pw_instance:
                await self._pw_instance.stop()
            self._pw_instance = await async_playwright().start()
            self._pw_browser = await self._pw_instance.chromium.launch()
        return self._pw_browser

    async def _render_t2i(self, md_text: str) -> str:
        browser = await self._ensure_browser()
        page = await browser.new_page(viewport={"width": 800, "height": 600})
        html = self._html_template.replace("{{ version }}", self._plugin_version)
        safe_text = md_text.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
        html = html.replace("{{ text | safe }}", safe_text)

        # 用本地脚本替换 CDN 引用（fallback: 无本地则保留 CDN 并异步下载）
        missing_scripts = []
        for cdn_url, filename in self._CDN_TO_FILE.items():
            cdn_tag = f'<script src="{cdn_url}"></script>'
            if filename in self._local_scripts:
                html = html.replace(cdn_tag, f"<script>{self._local_scripts[filename]}</script>")
            else:
                missing_scripts.append((cdn_url, filename))

        # 移除 KaTeX（印象渲染不需要）
        html = re.sub(r'<script[^>]*katex[^>]*>\s*</script>', '', html)

        # 异步下载缺失的脚本（下次渲染时可用）
        if missing_scripts:
            asyncio.create_task(self._download_missing_scripts(missing_scripts))

        await page.set_content(html, wait_until="load", timeout=15000)
        filename = hashlib.md5(md_text.encode()).hexdigest()[:12] + ".png"
        output_path = self._t2i_output_dir / filename
        await page.screenshot(path=str(output_path), full_page=True)
        await page.close()
        return str(output_path)

    async def _send_chunked_t2i(self, event: AstrMessageEvent, title: str, headers: List[str], rows: List[str], chunk_size: int = 200):
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
                img_path = await self._render_t2i(md_text)
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
                current_favour = record.favour
            else:
                current_favour = await self._get_initial_favour(event)
                record = FavourRecord(persona_id=persona_id, user_id=user_id, favour=current_favour)

            relationship = self._get_relationship(current_favour, user_id)
            prompt_final = build_injection_prompt(record, relationship)

            req.extra_user_content_parts.append(TextPart(text=prompt_final).mark_as_temp())
            self._log_debug(f"{self._tag(event)} 注入的印象上下文:\n{prompt_final}")
        except Exception as e:
            logger.error(f"{self._tag(event)} 注入印象上下文失败: {str(e)}\n{traceback.format_exc()}")

    # ================= Hook: 后台结算 =================

    @filter.on_decorating_result(priority=10)
    async def evaluate_favour(self, event: AstrMessageEvent):
        """LLM回复后后台结算好感度与12维情感变化，写入数据库"""
        res = event.get_result()
        if not res.is_llm_result():
            return
        user_text = event.message_str
        bot_reply = "".join(comp.text for comp in res.chain if isinstance(comp, Plain))
        if not bot_reply.strip() or not user_text.strip():
            return
        asyncio.create_task(self._calculate_favour_bg(event, user_text, bot_reply))

    @filter.on_decorating_result(priority=15)
    async def _on_session_reset(self, event: AstrMessageEvent):
        """检测 /reset 或 /new（通过 AstrBot 内置 _clean_group_context_session 标记），
        重置当前人格下所有用户的情感维度。好感度保留，仅情感归零。
        """
        if not event.get_extra("_clean_group_context_session"):
            return

        try:
            persona_id = await self._get_persona_id(event)
            count = await self.db.reset_all_emotions_by_persona(persona_id)
            if count > 0:
                logger.info(f"{self._tag(event)} 检测到会话重置/新建，已重置人格 {persona_id} 下 {count} 位用户的情感维度")
        except Exception as e:
            logger.warning(f"{self._tag(event)} 会话重置情感批量重置失败: {e}")

    async def _calculate_favour_bg(self, event: AstrMessageEvent, user_text: str, bot_reply: str):
        try:
            user_id = event.get_sender_id()
            umo = event.unified_msg_origin
            persona_id = await self._get_persona_id(event)

            # 提前获取裁决模型（供人设摘要提取和裁决共用）
            if self.judge_provider:
                provider_id = self.judge_provider.strip()
            else:
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            if not provider_id:
                logger.warning(f"{self._tag(event)} 未找到可用的 LLM Provider 进行印象结算")
                return

            # 获取人设性格摘要
            persona_summary = await self._get_persona_summary(persona_id, umo, provider_id)
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
                        record, self.emotion_decay_rate, self.emotion_decay_min_hours
                    )
                    if decayed:
                        decay_deltas = {dim: nv - getattr(record, dim) for dim, nv in decayed.items()}
                        await self.db.update_favour(persona_id, user_id, emotion_updates=decay_deltas)
                        record = await self.db.get_favour(persona_id, user_id)
                        logger.info(f"{self._tag(event)} 情感衰减: 用户 {user_id}, {elapsed_hours:.1f}h, {list(decayed.keys())}")
                current_favour = record.favour
                emotion_panel = build_emotion_panel(record)
            else:
                current_favour = await self._get_initial_favour(event)
                emotion_panel = "[喜悦:0] [信任:0] [恐惧:0] [惊讶:0] [悲伤:0] [厌恶:0] [愤怒:0] [期待:0] [得意:0] [内疚:0] [害羞:0] [嫉妒:0]"

            # 只取当前好感值对应的那一条关系规则
            current_rule = ""
            if self.relationship_mode == "advance" and self.relationship_advance_raw:
                try:
                    items = json.loads(self.relationship_advance_raw) if isinstance(self.relationship_advance_raw, str) else self.relationship_advance_raw
                    for item in items:
                        if item.get("min_value", self.min_favour_value) <= current_favour <= item.get("max_value", self.max_favour_value):
                            rule = item.get("rule", "")
                            desc = item.get("describe", "")
                            if rule:
                                current_rule = f"【当前关系：{desc}】\n{rule}\n\n"
                            break
                except (json.JSONDecodeError, TypeError):
                    pass

            # 获取近期对话历史
            history_section = ""
            if self.history_rounds > 0 and curr_cid:
                history_text = await self._get_recent_history(umo, user_id, curr_cid, bot_reply)
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

            self._log_debug(f"{self._tag(event)} 印象结算上下文:\n{eval_prompt}")

            resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt=eval_prompt)
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
                self._log_debug(f"{self._tag(event)} 印象结算推理: {reasoning}")

            if delta == 0 and not raw_emotions:
                return

            record = await self.db.get_favour(persona_id, user_id)
            old_fav = record.favour if record else await self._get_initial_favour(event)

            # 连续下降衰减：连续下降时按 0.8^n 衰减，低于1时转为概率
            fk = f"{persona_id}:{user_id}"
            if delta < 0:
                count = self._consecutive_decreases.get(fk, 0)
                if count > 0:
                    adjusted = abs(delta) * (0.8 ** count)
                    if adjusted < 1:
                        if random.random() < adjusted:
                            delta = -1
                        else:
                            delta = 0
                    else:
                        delta = -max(1, round(adjusted))
                self._consecutive_decreases[fk] = count + 1
            elif delta > 0:
                self._consecutive_decreases[fk] = 0

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

    async def _get_persona_summary(self, persona_id: str, umo: str, provider_id: str) -> str:
        """获取人设性格摘要，不存在或人设变更时自动提取并缓存。"""
        persona_prompt = ""
        try:
            persona = await self.persona_mgr.get_default_persona_v3(umo)
            if persona and "prompt" in persona:
                persona_prompt = persona["prompt"] or ""
        except Exception:
            pass

        if not persona_prompt.strip():
            return ""

        prompt_hash = hashlib.sha256(persona_prompt.encode("utf-8")).hexdigest()
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
            resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt=extract_prompt)
            summary = (resp.completion_text or "").strip()
            return summary[:500] if summary else ""
        except Exception as e:
            logger.warning(f"[EmotionFavour] 人设摘要提取失败: {e}")
            return ""

    # ================= 查询命令 =================

    @filter.command("查询印象", alias={'印象', '查询好感度', '好感度'})
    async def query_favour(self, event: AstrMessageEvent, target: str = ""):
        """查询自己或指定用户的好感度与12维情感状态"""
        if not await self._check_command_permission(event, "查询印象"):
            yield event.plain_result("权限不足！你无法使用此命令。")
            return

        sender_id = event.get_sender_id()
        target_uid = get_target_uid(event, target) or sender_id

        if target_uid != sender_id:
            required = self._LEVEL_NAME_MAP.get(self.query_others_favour_level, PermLevel.ADMIN)
            user_level = await self._get_user_perm_level(event)
            if user_level < required:
                yield event.plain_result("权限不足！你只能查看自己的印象。")
                return

        persona_id = await self._get_persona_id(event)
        record = await self.db.get_favour(persona_id, target_uid)
        if record:
            fav = record.favour
        else:
            fav = await self._get_initial_favour(event) if target_uid == sender_id else 0
            record = FavourRecord(persona_id=persona_id, user_id=target_uid, favour=fav)

        name = await get_user_display_name(event, target_uid)
        relationship = self._get_relationship(fav, target_uid)
        detail = format_emotion_detail(record, relationship)

        md_text = f"# 印象查询\n\n**用户**：{escape_markdown(name)}  \n**ID**：{target_uid}\n\n---\n\n{detail}"
        try:
            img_path = await self._render_t2i(md_text)
            yield event.image_result(img_path)
        except Exception as e:
            logger.warning(f"{self._tag(event)} 印象查询 T2I 失败，回退纯文本: {e}")
            yield event.plain_result(f"🔍 用户：{name}\n🆔 ID：{target_uid}\n{detail}")

    @filter.command("查询全局印象", alias={'全局印象', '全局好感度', '查询全局好感度'})
    async def query_global_favour(self, event: AstrMessageEvent, page: int = 1):
        """分页查看当前人格下所有用户的好感度记录"""
        if not await self._check_command_permission(event, "查询全局印象"):
            yield event.plain_result("权限不足！你无法使用此命令。")
            return

        persona_id = await self._get_persona_id(event)
        records = await self.db.get_global_records(persona_id)
        if not records:
            yield event.plain_result("暂无印象记录。")
            return

        records = await self._sort_records(event, records)
        page_size = 20
        total_records = len(records)
        total_pages = (total_records + page_size - 1) // page_size
        if page < 1: page = 1
        if page > total_pages and total_pages > 0: page = total_pages

        page_records = records[(page - 1) * page_size:page * page_size]
        headers = ["| 用户ID | 好感值 | 关系 | 主导情感 |", "| :--- | :---: | :---: | :--- |"]
        rows = []
        for r in page_records:
            rel = escape_markdown(self._get_relationship(r.favour, r.user_id))
            top = get_dominant_emotions(r, 3)
            emotion_str = "、".join(f"{EMOTION_DISPLAY_NAMES[k]}({v})" for k, v in top) if top else "-"
            rows.append(f"| {r.user_id} | {r.favour} | {rel} | {emotion_str} |")

        await self._send_chunked_t2i(event, f"📊 印象记录 - 第 {page}/{total_pages} 页", headers, rows)

    # ================= 修改命令 =================

    @filter.command("修改印象")
    async def modify_favour(self, event: AstrMessageEvent, target: str, value: int):
        """修改指定用户的好感度数值"""
        if not await self._check_command_permission(event, "修改印象"):
            yield event.plain_result("权限不足！你无法使用此命令。")
            return
        uid = get_target_uid(event, target)
        if not uid:
            yield event.plain_result("未找到用户，请使用 @ 或输入 ID。")
            return
        persona_id = await self._get_persona_id(event)
        try:
            await self.db.update_favour(persona_id, uid, favour=value)
            yield event.plain_result(f"已将用户 {uid} 的好感值修改为 {value}。")
            logger.info(f"{self._tag(event)} 管理员 {event.get_sender_id()} 修改用户 {uid} 好感值为 {value}")
        except Exception as e:
            logger.error(f"{self._tag(event)} 修改印象失败: {e}")
            yield event.plain_result("修改失败，请检查日志。")

    @filter.command("修改情感")
    async def modify_emotion(self, event: AstrMessageEvent, target: str, dimension: str = "", value: int = 0):
        """修改指定用户的单个或全部情感维度"""
        if not await self._check_command_permission(event, "修改情感"):
            yield event.plain_result("权限不足！你无法使用此命令。")
            return
        uid = get_target_uid(event, target)
        if not uid:
            yield event.plain_result("未找到用户，请使用 @ 或输入 ID。")
            return

        # 如果 dimension 看起来是数字，说明用户省略了维度名：/修改情感 @user 80
        if dimension and not value and dimension.lstrip('-').isdigit():
            value = int(dimension)
            dimension = ""

        if value < 0 or value > 100:
            yield event.plain_result("数值越界！情感维度范围为 0~100。")
            return

        persona_id = await self._get_persona_id(event)
        record = await self.db.get_favour(persona_id, uid)

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
                await self.db.update_favour(persona_id, uid, emotion_updates=emotion_updates)
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
            await self.db.update_favour(persona_id, uid, emotion_updates={dim_key: delta})
            cn = EMOTION_DISPLAY_NAMES[dim_key]
            yield event.plain_result(f"已将用户 {uid} 的{cn}从 {old_val} 修改为 {value}。")
            logger.info(f"{self._tag(event)} 管理员 {event.get_sender_id()} 修改用户 {uid} 的 {dim_key}({cn}): {old_val}->{value}")
        except Exception as e:
            logger.error(f"{self._tag(event)} 修改情感失败: {e}")
            yield event.plain_result("修改失败，请检查日志。")

    # ================= 清空命令 =================

    @filter.command("清空印象")
    async def clear_user_favour(self, event: AstrMessageEvent, target: str):
        """清空指定用户的好感度与情感数据（需二次确认，自动备份）"""
        if not await self._check_command_permission(event, "清空印象"):
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
            if evt.message_str.strip() == "确认清空":
                record = await self.db.get_favour(persona_id, uid)
                if record:
                    backup_file = await self.db.backup_data([record], f"backup_user_{uid}")
                    await self.db.delete_favour(persona_id, uid)
                    await evt.send(evt.plain_result(f"✅ 已清空用户 {uid} 的印象数据。"))
                    logger.info(f"{self._tag(event)} 管理员 {evt.get_sender_id()} 清空了用户 {uid} 的印象数据\n备份: {backup_file}")
                else:
                    await evt.send(evt.plain_result("该用户无印象记录。"))
            else:
                await evt.send(evt.plain_result("已取消清空操作。"))
            controller.stop()

        try:
            await confirm_waiter(event)
        except TimeoutError:
            yield event.plain_result("操作超时，已取消清空。")
        finally:
            event.stop_event()

    @filter.command("清空全局印象")
    async def clear_all_favour(self, event: AstrMessageEvent):
        """清空当前人格下所有用户的好感度数据（需二次确认，自动备份）"""
        if not await self._check_command_permission(event, "清空全局印象"):
            yield event.plain_result("权限不足！你无法使用此命令。")
            return
        persona_id = await self._get_persona_id(event)
        yield event.plain_result("🚨 极度危险：即将清空当前人格下【所有】印象数据！\n请在 30 秒内回复「确认清空所有数据」以继续，回复其他内容取消。")

        @session_waiter(timeout=30, record_history_chains=False)
        async def confirm_waiter(controller: SessionController, evt: AstrMessageEvent):
            if evt.message_str.strip() == "确认清空所有数据":
                records = await self.db.get_global_records(persona_id)
                if records:
                    backup_file = await self.db.backup_data(records, "backup_all_database")
                    await self.db.clear_persona(persona_id)
                    await evt.send(evt.plain_result("✅ 已清空当前人格下所有印象数据。"))
                    logger.warning(f"{self._tag(event)} Bot管理员 {evt.get_sender_id()} 清空了人格 {persona_id} 的所有印象数据\n备份: {backup_file}")
                else:
                    await evt.send(evt.plain_result("数据库中无印象记录。"))
            else:
                await evt.send(evt.plain_result("已取消清空操作。"))
            controller.stop()

        try:
            await confirm_waiter(event)
        except TimeoutError:
            yield event.plain_result("操作超时，已取消清空。")
        finally:
            event.stop_event()

    # ================= 帮助命令 =================

    @filter.command("印象帮助", alias={'查看印象帮助'})
    async def help_menu(self, event: AstrMessageEvent):
        """显示印象插件命令菜单"""
        msg = ["⭐ 印象插件命令菜单 ⭐"]

        query_cmds = []
        if await self._check_command_permission(event, "查询印象"):
            required = self._LEVEL_NAME_MAP.get(self.query_others_favour_level, PermLevel.ADMIN)
            user_level = await self._get_user_perm_level(event)
            query_cmds.append("- 查询印象 [@用户]" if user_level >= required else "- 查询印象")
        if await self._check_command_permission(event, "查询全局印象"):
            query_cmds.append("- 查询全局印象 [页码]")
        if query_cmds:
            msg.append("\n[查询命令]")
            msg.extend(query_cmds)

        modify_cmds = []
        if await self._check_command_permission(event, "修改印象"):
            modify_cmds.append("- 修改印象 @用户 <数值>")
        if await self._check_command_permission(event, "修改情感"):
            modify_cmds.append("- 修改情感 @用户 <维度> <数值>")
        if modify_cmds:
            msg.append("\n[修改命令]")
            msg.extend(modify_cmds)

        clear_cmds = []
        if await self._check_command_permission(event, "清空印象"):
            clear_cmds.append("- 清空印象 @用户")
        if await self._check_command_permission(event, "清空全局印象"):
            clear_cmds.append("- 清空全局印象")
        if clear_cmds:
            msg.append("\n[清空命令]")
            msg.extend(clear_cmds)

        if await self._check_command_permission(event, "印象指令帮助"):
            msg.append("\n- 印象指令帮助")

        md_text = "\n".join(msg)
        try:
            img_path = await self._render_t2i(md_text)
            yield event.image_result(img_path)
        except Exception as e:
            logger.warning(f"{self._tag(event)} 帮助菜单 T2I 失败，回退纯文本: {e}")
            yield event.plain_result(md_text)

    @filter.command("印象指令帮助")
    async def help_usage(self, event: AstrMessageEvent):
        """显示印象指令的详细用法示例"""
        msg_parts = ["⭐ 印象指令用法示例 ⭐"]
        section = 0

        has_query = await self._check_command_permission(event, "查询印象") or await self._check_command_permission(event, "查询全局印象")
        if has_query:
            section += 1
            msg_parts.append(f"\n{section}. 查询印象")
            if await self._check_command_permission(event, "查询印象"):
                required = self._LEVEL_NAME_MAP.get(self.query_others_favour_level, PermLevel.ADMIN)
                user_level = await self._get_user_perm_level(event)
                if user_level >= required:
                    msg_parts += ["   用法: /查询印象 [@用户]", "   示例: /查询印象 @Wolfycz"]
                else:
                    msg_parts += ["   用法: /查询印象", "   说明: 查看自己的印象和情感维度。"]
            if await self._check_command_permission(event, "查询全局印象"):
                msg_parts += ["   用法: /查询全局印象 [页码]", "   示例: /查询全局印象 2"]

        has_modify = await self._check_command_permission(event, "修改印象")
        has_modify_emotion = await self._check_command_permission(event, "修改情感")
        if has_modify:
            section += 1
            msg_parts += [f"\n{section}. 修改印象", "   用法: /修改印象 @用户 <数值>", "   示例: /修改印象 @Wolfycz 60"]
        if has_modify_emotion:
            section += 1
            msg_parts += [f"\n{section}. 修改情感", "   用法: /修改情感 @用户 <维度> <数值>", "   示例: /修改情感 @Wolfycz 喜悦 80", "   说明: 维度可用中文或英文（如 喜悦/joy），数值范围 0~100。"]

        clear_cmds = []
        if await self._check_command_permission(event, "清空印象"):
            clear_cmds.append("   用法: /清空印象 @用户")
        if await self._check_command_permission(event, "清空全局印象"):
            clear_cmds.append("   用法: /清空全局印象")
        if clear_cmds:
            section += 1
            msg_parts.append(f"\n{section}. 清空操作")
            msg_parts.extend(clear_cmds)
            msg_parts.append("   说明: 清空操作需要二次确认，并会自动备份数据。")

        md_text = "\n".join(msg_parts)
        try:
            img_path = await self._render_t2i(md_text)
            yield event.image_result(img_path)
        except Exception as e:
            logger.warning(f"{self._tag(event)} 指令帮助 T2I 失败，回退纯文本: {e}")
            yield event.plain_result(md_text)

    # ================= 人设摘要管理（仅管理员） =================

    @filter.command("查看印象人设")
    async def cmd_view_persona_summary(self, event: AstrMessageEvent):
        """查看当前人格的AI提取性格摘要"""
        sender_id = event.get_sender_id()
        if str(sender_id) not in {str(a) for a in self.admins_id}:
            return
        persona_id = await self._get_persona_id(event)
        cached = await self.db.get_persona_summary(persona_id)
        if cached and cached["summary"]:
            yield event.plain_result(f"当前人设摘要 (persona_id={persona_id}):\n{cached['summary']}")
        else:
            yield event.plain_result(f"当前无人设摘要 (persona_id={persona_id})")

    @filter.command("清除印象人设")
    async def cmd_delete_persona_summary(self, event: AstrMessageEvent):
        """清除当前人格的AI提取性格摘要缓存"""
        sender_id = event.get_sender_id()
        if str(sender_id) not in {str(a) for a in self.admins_id}:
            return
        persona_id = await self._get_persona_id(event)
        await self.db.delete_persona_summary(persona_id)
        yield event.plain_result(f"已清除人设摘要 (persona_id={persona_id})")
