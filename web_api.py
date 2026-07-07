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

from .storage import EMOTION_DIMENSIONS, EMOTION_DISPLAY_NAMES, EMOTION_GROUPS, _is_valid_userid


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

def _record_to_dict(plugin, record) -> dict:
    """把 FavourRecord 序列化为前端 dict。

    返回的 favour 是 decayed（transient）值——展示与编辑基准都用它，
    避免管理员看到「存储 60 但实际生效 55」的混淆。
    stored_favour 仅作只读信息附带。
    """
    decayed = plugin._decay_favour_value(record)
    emotions = {dim: int(getattr(record, dim, 0)) for dim in EMOTION_DIMENSIONS}
    is_admin = plugin._is_admin_override(record.user_id)
    return {
        "user_id": record.user_id,
        "favour": decayed,
        "stored_favour": record.favour,
        "relationship": plugin._get_relationship(decayed, record.user_id),
        "relationship_range": list(plugin._get_relationship_range(decayed, record.user_id) or []),
        "is_admin_override": is_admin,
        "emotions": emotions,
        "created_at": str(record.created_at) if record.created_at else "",
        "updated_at": str(record.updated_at) if record.updated_at else "",
    }


# ==================== 注册入口 ====================

def register_web_apis(context, plugin) -> None:
    """注册所有 Web API。

    plugin 需暴露：
    - plugin.db: FavourDBManager
    - plugin._get_relationship / _get_relationship_range / _is_admin_override
    - plugin._decay_favour_value
    - plugin.min_favour_value / max_favour_value
    - plugin.relationship_mode
    """

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
            personas = await plugin.db.get_distinct_personas()
            return _ok(personas=personas)
        except Exception as e:
            return _internal_error(e)

    # ==================== 端点：records ====================

    async def get_records():
        try:
            persona_id = (request.args.get("persona_id") or "").strip()
            if not persona_id:
                return _err("persona_id 不能为空", 400)
            if len(persona_id) > 64:
                return _err("persona_id 过长", 400)

            records = await plugin.db.get_global_records(persona_id)
            items = [_record_to_dict(plugin, r) for r in records]
            return _ok(
                persona_id=persona_id,
                records=items,
                total=len(items),
                favour_min=plugin.min_favour_value,
                favour_max=plugin.max_favour_value,
                relationship_mode=plugin.relationship_mode,
                emotion_dimensions=list(EMOTION_DIMENSIONS),
                emotion_display_names=dict(EMOTION_DISPLAY_NAMES),
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
                is_admin_override=plugin._is_admin_override(user_id),
            )
        except Exception as e:
            return _internal_error(e)

    # ==================== 端点：records/save ====================

    async def save_record():
        try:
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
                existing = await plugin.db.get_favour(persona_id, user_id)
                if existing is not None:
                    return _err(
                        f"该用户在 persona={persona_id} 下已存在记录，请用编辑修改",
                        409,
                    )

            ok = await plugin.db.set_record_fields(
                persona_id, user_id,
                favour=favour,
                emotions_absolute=emotions_absolute,
            )
            if not ok:
                return _err("保存失败，请检查日志", 500)

            record = await plugin.db.get_favour(persona_id, user_id)
            return _ok(record=_record_to_dict(plugin, record) if record else None)
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
