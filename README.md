# emotion_favour — 多维度情感好感度系统

为 AstrBot 提供完整的角色-用户情感关系追踪。后台 LLM 实时结算好感度与 12 维情感变化，注入对话上下文引导角色表现。

## 功能

- **好感度系统**：后台 LLM 自动结算好感度变化，支持 GALGAME / NORMAL / REALISTIC 三种模式
- **12维情感模型**：喜悦、信任、恐惧、惊讶、悲伤、厌恶、愤怒、期待、得意、内疚、害羞、嫉妒
- **情感代谢**：负面消解（愤怒/悲伤等随善意互动消退）+ 激情冷却（惊讶/害羞等瞬时情绪回落）
- **情感时间衰减**：长时间未互动的用户情感自然回归 baseline，防止情绪永久累积
- **连续下降保护**：好感度连续下降时指数衰减，避免断崖式暴跌
- **注入渲染**：自动将好感度、关系、情感面板与三层语气渲染注入对话上下文
- **上下文清洗**：自动清理历史中残留的旧状态标签和 `<thought>` 块
- **人设性格摘要**：AI 自动从人设文本提取性格特征摘要，辅助裁决模型判断
- **人格隔离**：不同人格拥有独立的好感度记录
- **对话上下文集成**：可选集成 chat_memory 插件，按用户 ID 隔离读取对话历史
- **权限系统**：基于群角色的多级权限控制（普通成员→群主→Bot管理员）
- **T2I 渲染**：查询结果与全局排行支持图片渲染，防刷屏
- **安全操作**：清空操作需二次确认 + 自动 JSON 备份

## 安装

```
/astrbot_plugin install https://github.com/W-Wolfycz/emotion_favour
```

## 文件结构

```
emotion_favour/
├── main.py              # 插件主体（Star 类、Hook、命令）
├── storage.py           # 数据库模型与管理（SQLModel + aiosqlite）
├── utils.py             # 工具函数
├── permissions.py       # 权限等级与权限管理
├── _conf_schema.json    # 配置项定义
├── custom_t2i.html      # T2I 图片渲染模板
├── metadata.yaml
├── requirements.txt
└── logo.png
```

## 配置项

### 基础配置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `favour_mode` | 好感度判定模式（galgame / normal / realistic） | normal |
| `min_favour_value` / `max_favour_value` | 好感度范围 | -100 / 100 |
| `default_favour` | 新用户初始好感度 | 0 |
| `judge_provider` | 裁决模型（留空跟随当前对话模型） | — |
| `relationship_config` | 关系映射（simple 列表 / advance 自定义区间） | simple |

### 高级配置（advanced_config）

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `favour_change_min` / `favour_change_max` | 好感度单次变化范围 | -5 / 5 |
| `emotion_change_min` / `emotion_change_max` | 情感维度单次变化范围 | -10 / 5 |
| `history_rounds` | 裁决模型参考的近期对话轮数（0-10） | 0 |
| `use_chat_memory` | 启用 chat_memory 插件提供按用户隔离的对话上下文 | false |
| `emotion_decay_enabled` | 启用情感时间衰减 | true |
| `emotion_decay_rate` | 每小时情感保留比例 | 0.85 |
| `admin_default_favour` | 管理员/特使初始好感度 | 50 |
| `favour_envoys` | 好感度特使用户 ID 列表 | [] |
| `query_others_favour_level` | 查看他人印象的最低权限等级 | 群管理员 |

### 日志配置（log_config）

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `log_with_bot_id` | 日志前缀附加机器人实例 ID | false |
| `debug_to_info` | debug 日志提级为 info 输出 | false |

## 命令

| 命令 | 权限 | 说明 |
|------|------|------|
| `/查询印象 [@用户]` | 所有成员 | 查看自己（或他人）的好感度与情感维度 |
| `/查询全局印象 [页码]` | Bot管理员 | 好感度排行榜（T2I 分页渲染） |
| `/修改印象 @用户 数值` | 群管理员 | 直接设置用户好感度 |
| `/修改情感 @用户 维度 数值` | 群管理员 | 修改单个或全部情感维度 |
| `/清空印象 @用户` | 群主 | 清空指定用户数据（二次确认+自动备份） |
| `/清空全局印象` | Bot管理员 | 清空当前人格所有数据（二次确认+自动备份） |
| `/印象帮助` | 所有成员 | 命令菜单 |
| `/印象指令帮助` | 所有成员 | 详细用法示例 |
| `/查看印象人设` | Bot管理员 | 查看当前人格的 AI 性格摘要 |
| `/清除印象人设` | Bot管理员 | 清除性格摘要缓存 |

## 会话重置行为

执行 `/reset` 或 `/new` 时，自动重置当前人格下**所有用户**的情感维度归零（好感度保留）。通过 AstrBot 内置的 `_clean_group_context_session` 标记精确检测，无需轮询。

## 对话上下文

裁决模型参考的对话历史支持两种来源：

- **AstrBot 自带上下文**（默认）：从 `conv.history` 读取，群聊场景下所有用户共享上下文
- **chat_memory 插件**（需安装）：按 `UMO + conversation_id + user_id` 隔离读取，群内每个用户独立历史

> 启用 chat_memory 前需先安装该插件，否则自动回退。

## 依赖

- Python ≥ 3.10
- AstrBot ≥ 4.0
- sqlmodel, aiosqlite, aiofiles
- playwright（可选，T2I 渲染）

## License

[MIT](LICENSE)
