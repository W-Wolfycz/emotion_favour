"""emotion_favour 配置迁移：基于 config_version 字段的链式迁移。

机制：
- 配置里若没有 config_version 字段，视为版本 0
- 每次插件加载调用 migrate(config)，从声明的版本一路迁移到 CURRENT_CONFIG_VERSION
- 每个版本间的迁移逻辑独立成 _migrate_vN_to_vNp1 函数，注册在 _MIGRATIONS 表里
- 迁移幂等：已经是目标版本时 no-op；老格式解析失败时跳过而不报错

新增版本时：
1. 把 CURRENT_CONFIG_VERSION 加 1
2. 写 _migrate_vN_to_vNp1 函数（原地修改并返回 config）
3. 注册到 _MIGRATIONS[N+1]
"""
import json
import logging
from typing import Callable

logger = logging.getLogger("astrbot")

CURRENT_CONFIG_VERSION = 1


def migrate(config: dict) -> dict:
    """主入口：从声明的版本迁移到 CURRENT_CONFIG_VERSION。原地修改并返回。
    迁移失败时保留原 config 不变（只打 error 日志），不抛异常。"""
    if not isinstance(config, dict):
        return config

    version = config.get("config_version", 0)
    if not isinstance(version, int) or version < 0:
        version = 0

    if version >= CURRENT_CONFIG_VERSION:
        return config

    initial_version = version
    while version < CURRENT_CONFIG_VERSION:
        target = version + 1
        step_fn = _MIGRATIONS.get(target)
        if step_fn is None:
            logger.warning(
                f"[emotion_favour] 配置迁移中断：v{version} → v{target} 没有注册的迁移函数"
            )
            break
        try:
            new_config = step_fn(config)
            if new_config is None:
                # 迁移函数主动放弃（如数据格式损坏、不符合预期）
                # 不 bump 版本号，下次启动会再试一次
                logger.info(
                    f"[emotion_favour] v{version}→v{target} 迁移函数跳过（数据格式不符合预期），版本号保持 v{version}"
                )
                break
            config = new_config
            config["config_version"] = target
            version = target
        except Exception as e:
            logger.error(
                f"[emotion_favour] 配置迁移失败 v{version} → v{target}: {e}",
                exc_info=True,
            )
            break

    if version != initial_version:
        logger.info(
            f"[emotion_favour] 配置已迁移：v{initial_version} → v{version}"
        )
    return config


def _migrate_v0_to_v1(config: dict):
    """v0 → v1：relationship_config.advance_config 从 JSON 字符串迁移到 template_list (list) 格式。

    - 老格式：text 字段，存 JSON 字符串 `[{"describe":"普通",...}]`
    - 新格式：list[dict]，每项是 template_list 元素（带 `__template_key` 元字段）

    保留原 describe/min_value/max_value/rule 字段不变；
    boundary/preview 是 v1 新增字段，老配置自然缺失，留空让用户后续补充。

    返回值：
    - config：迁移成功 / 数据已符合新格式（list 或缺失）→ 主入口会 bump 版本号
    - None：数据格式损坏（JSON 解析失败、解析后非 list）→ 主入口不 bump，下次启动再试
    """
    rel_conf = config.get("relationship_config")
    if not isinstance(rel_conf, dict):
        return config  # 没这节配置，视为符合新版本（无数据可迁）

    raw = rel_conf.get("advance_config")
    if raw is None or isinstance(raw, list):
        return config  # 已是 list 或不存在 → 符合新版本
    if not isinstance(raw, str):
        return config  # 未知类型，不动；视为已迁移避免反复尝试

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(
            f"[emotion_favour] v0→v1 advance_config 不是合法 JSON，跳过迁移: {e}"
        )
        return None  # 数据损坏 → 不 bump

    if not isinstance(parsed, list):
        logger.warning(
            "[emotion_favour] v0→v1 advance_config JSON 解析后非 list，跳过迁移"
        )
        return None

    new_items = []
    for item in parsed:
        if isinstance(item, dict):
            new_item = {"__template_key": "custom"}
            new_item.update(item)
            new_items.append(new_item)

    rel_conf["advance_config"] = new_items
    return config


_MIGRATIONS: dict[int, Callable[[dict], dict]] = {
    1: _migrate_v0_to_v1,
}
