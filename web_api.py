"""emotion_favour Web API

向 AstrBot 注册插件 REST API，供 Plugin Pages 调用。

端点清单：
- GET  /emotion_favour/about                        版本号
- GET  /emotion_favour/personas                     DB 中存在记录的所有 persona_id
- GET  /emotion_favour/records?persona_id=          该 persona 下所有用户记录
- GET  /emotion_favour/relationship?favour=&user_id= 实时计算关系名（前端编辑即时反馈）
- POST /emotion_favour/records/save                 单行保存（绝对值写入，不走 diminish_delta）

统一响应信封：``{success: bool, ...data | error: str}``
鉴权继承 AstrBot 主 webui 登录态。
"""

import json
import os

import yaml
from quart import jsonify, request

from astrbot.api import logger

from .domain import EMOTION_DIMENSIONS, EMOTION_DISPLAY_NAMES, EMOTION_GROUPS
from .storage import _is_valid_userid


PLUGIN_NAME = "emotion_favour"

_METADATA_CACHE: dict | None = None


# ==================== 静态资源读取 ====================

def _plugin_root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _read_metadata() -> dict:
    global _METADATA_CACHE
    if _METADATA_CACHE is None:
        path = os.path.join(_plugin_root(), "metadata.yaml")
        try:
            with open(path, encoding="utf-8") as f:
                _METADATA_CACHE = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            _METADATA_CACHE = {}
    return _METADATA_CACHE if isinstance(_METADATA_CACHE, dict) else {}


# ==================== 响应助手 ====================

def _ok(**data):
    return jsonify({"success": True, **data})


def _err(msg: str, status: int = 400):
    logger.warning(f"[{PLUGIN_NAME}] Web API 返回 {status}: {msg}")
    return jsonify({"success": False, "error": msg}), status


def _internal_error(e: Exception):
    logger.error(f"[{PLUGIN_NAME}] Web API 内部错误: {e}")
    return jsonify({"success": False, "error": "服务器内部错误"}), 500


# ==================== 序列化辅助 ====================

def _record_to_dict(plugin, record, persona_id: str | None = None) -> dict:
    """把 FavourRecord 序列化为前端 dict。

    返回的 favour 是 decayed（transient）值——展示与编辑基准都用它，
    避免管理员看到「存储 60 但实际生效 55」的混淆。
    stored_favour 仅作只读信息附带。
    """
    decayed = plugin._decay_favour_value(record)
    emotions = {dim: int(getattr(record, dim, 0)) for dim in EMOTION_DIMENSIONS}
    is_special = plugin._is_special_override(record.user_id)
    created_at = plugin.db.to_local_datetime(record.created_at)
    updated_at = plugin.db.to_local_datetime(record.updated_at)
    return {
        "persona_id": persona_id or record.persona_id,
        "user_id": record.user_id,
        "favour": decayed,
        "stored_favour": record.favour,
        "relationship": plugin._get_relationship(decayed, record.user_id),
        "relationship_range": list(plugin._get_relationship_range(decayed, record.user_id) or []),
        "is_special_override": is_special,
        "emotions": emotions,
        "created_at": str(created_at) if created_at else "",
        "updated_at": str(updated_at) if updated_at else "",
    }


# ==================== 注册入口 ====================

def register_web_apis(context, plugin) -> None:
    """注册所有 Web API。

    plugin 需暴露：
    - plugin.db: FavourDBManager
    - plugin._get_relationship / _get_relationship_range / _is_special_override
    - plugin._decay_favour_value
    - plugin.min_favour_value / max_favour_value
    - plugin.relationship_mode
    """

    def _not_ready():
        if plugin.is_ready():
            return None
        return _err(f"插件当前状态为 {plugin._state}，存储尚不可用", 503)

    # ==================== 端点：about ====================

    async def get_about():
        try:
            meta = _read_metadata()
            return _ok(
                name=str(meta.get("name", PLUGIN_NAME)),
                version=str(meta.get("version", "")),
                display_name=str(meta.get("display_name", "")),
                author=str(meta.get("author", "")),
            )
        except Exception as e:
            return _internal_error(e)

    # ==================== 端点：personas ====================

    async def get_personas():
        try:
            unavailable = _not_ready()
            if unavailable:
                return unavailable
            personas = set(await plugin.db.get_distinct_personas())
            personas.add("default")
            try:
                for item in getattr(plugin.persona_mgr, "personas_v3", []) or []:
                    name = str(item.get("name", "") or "").strip()
                    if name:
                        personas.add(name)
            except Exception:
                pass
            return _ok(personas=sorted(personas))
        except Exception as e:
            return _internal_error(e)

    # ==================== 端点：records ====================

    async def get_records():
        try:
            unavailable = _not_ready()
            if unavailable:
                return unavailable
            persona_id = (request.args.get("persona_id") or "").strip()
            if not persona_id:
                return _err("persona_id 不能为空", 400)
            if len(persona_id) > 64:
                return _err("persona_id 过长", 400)

            try:
                page = max(1, int(request.args.get("page") or 1))
                page_size = max(1, min(200, int(request.args.get("page_size") or 50)))
            except (TypeError, ValueError):
                return _err("page 和 page_size 必须是整数", 400)
            sort_by = (request.args.get("sort_by") or "favour").strip()
            sort_order = (request.args.get("sort_order") or "desc").strip().lower()
            user_id_search = (request.args.get("user_id_search") or "").strip()
            if len(user_id_search) > 64:
                return _err("user_id_search 过长", 400)
            if sort_by not in {"user_id", "favour", "updated_at", "created_at"}:
                return _err("sort_by 不受支持", 400)
            if sort_order not in {"asc", "desc"}:
                return _err("sort_order 必须是 asc 或 desc", 400)

            total = await plugin.db.count_records(
                persona_id, user_id_search=user_id_search,
            )
            total_pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, total_pages)
            records = await plugin.db.list_records(
                persona_id,
                offset=(page - 1) * page_size,
                limit=page_size,
                sort_by=sort_by,
                sort_order=sort_order,
                user_id_search=user_id_search,
            )
            items = [_record_to_dict(plugin, r, persona_id) for r in records]
            # 批量读取昵称缓存
            uids = [r.user_id for r in records]
            nicknames = plugin.db.get_cached_nicknames(uids) if hasattr(plugin.db, 'get_cached_nicknames') else {}
            return _ok(
                persona_id=persona_id,
                records=items,
                total=total,
                page=page,
                page_size=page_size,
                total_pages=total_pages,
                sort_by=sort_by,
                sort_order=sort_order,
                user_id_search=user_id_search,
                favour_min=plugin.min_favour_value,
                favour_max=plugin.max_favour_value,
                relationship_mode=plugin.relationship_mode,
                emotion_dimensions=list(EMOTION_DIMENSIONS),
                emotion_display_names=dict(EMOTION_DISPLAY_NAMES),
                nicknames=nicknames,
                emotion_groups=dict(EMOTION_GROUPS),
            )
        except Exception as e:
            return _internal_error(e)

    # ==================== 端点：relationship（实时计算） ====================

    async def get_relationship():
        try:
            persona_id = (request.args.get("persona_id") or "").strip()
            user_id = (request.args.get("user_id") or "").strip()
            favour_str = (request.args.get("favour") or "").strip()

            if not persona_id or not user_id:
                return _err("persona_id 和 user_id 不能为空", 400)
            if not _is_valid_userid(user_id):
                return _err("user_id 含非法字符", 400)
            try:
                favour = int(favour_str)
            except (ValueError, TypeError):
                return _err("favour 必须是整数", 400)

            favour = max(plugin.min_favour_value, min(plugin.max_favour_value, favour))
            name = plugin._get_relationship(favour, user_id)
            range_tuple = plugin._get_relationship_range(favour, user_id)
            return _ok(
                favour=favour,
                name=name,
                range=list(range_tuple) if range_tuple else [],
                is_special_override=plugin._is_special_override(user_id),
            )
        except Exception as e:
            return _internal_error(e)

    # ==================== 端点：records/save ====================

    async def save_record():
        try:
            unavailable = _not_ready()
            if unavailable:
                return unavailable
            data = await request.get_json() or {}
            persona_id = str(data.get("persona_id", "")).strip()
            user_id = str(data.get("user_id", "")).strip()

            if not persona_id or not user_id:
                return _err("persona_id 和 user_id 不能为空", 400)
            if len(persona_id) > 64:
                return _err("persona_id 过长", 400)
            if not _is_valid_userid(user_id):
                return _err("user_id 含非法字符", 400)

            favour = None
            if "favour" in data and data["favour"] is not None:
                try:
                    favour = int(data.get("favour"))
                except (ValueError, TypeError):
                    return _err("favour 必须是整数", 400)
                favour = max(plugin.min_favour_value, min(plugin.max_favour_value, favour))

            emotions_absolute = None
            if "emotions_absolute" in data and data["emotions_absolute"] is not None:
                raw_emotions = data.get("emotions_absolute")
                if not isinstance(raw_emotions, dict):
                    return _err("emotions_absolute 必须是对象", 400)
                emotions_absolute = {}
                for dim, val in raw_emotions.items():
                    if dim not in EMOTION_DIMENSIONS:
                        continue
                    try:
                        emotions_absolute[dim] = max(0, min(100, int(val)))
                    except (ValueError, TypeError):
                        return _err(f"情感维度 {dim} 的值必须是整数", 400)

            if favour is None and not emotions_absolute:
                return _err("至少需要传 favour 或 emotions_absolute 之一", 400)

            create_only = bool(data.get("create_only", False))
            if create_only:
                created = await plugin.create_record(
                    persona_id,
                    user_id,
                    favour=favour if favour is not None else 0,
                    emotions_absolute=emotions_absolute,
                )
                if not created:
                    existing = await plugin.db.get_favour(persona_id, user_id)
                    if existing is None:
                        return _err("创建失败，请检查日志", 500)
                    return _err(
                        f"该用户在 persona={persona_id} 下已存在记录，请用编辑修改",
                        409,
                    )
            else:
                ok = await plugin.set_record_fields(
                    persona_id, user_id,
                    favour=favour,
                    emotions_absolute=emotions_absolute,
                )
                if not ok:
                    return _err("保存失败，请检查日志", 500)

            record = await plugin.db.get_favour(persona_id, user_id)
            return _ok(record=_record_to_dict(plugin, record, persona_id) if record else None)
        except Exception as e:
            return _internal_error(e)

    # ==================== 注册 ====================

    context.register_web_api(
        f"/{PLUGIN_NAME}/about", get_about, ["GET"], "获取插件版本信息"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/personas", get_personas, ["GET"], "获取所有 persona_id 列表"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/records", get_records, ["GET"], "获取指定 persona 的所有用户记录"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/relationship", get_relationship, ["GET"], "实时计算好感度对应的关系名"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/records/save", save_record, ["POST"], "保存单行用户记录变更"
    )

    logger.info(f"[{PLUGIN_NAME}] ✅ Web API 已注册（共 5 个端点）")
