# emotion_favour — 多维度情感好感度系统

为 AstrBot 提供完整的角色-用户情感关系追踪。后台 LLM 实时结算好感度与 12 维情感变化，注入对话上下文引导角色表现。

## 功能

- **好感度系统**：后台 LLM 自动结算好感度变化，支持 GALGAME / NORMAL / REALISTIC 三种模式
- **12维情感模型**：喜悦、信任、恐惧、惊讶、悲伤、厌恶、愤怒、期待、得意、内疚、害羞、嫉妒
- **情感代谢**：负面消解（愤怒/悲伤等随善意互动消退）+ 激情冷却（惊讶/害羞等瞬时情绪回落）
- **情感时间衰减**：长时间未互动的用户情感自然回归 baseline，防止情绪永久累积
- **好感度时间衰减**：与情感衰减独立，采用 lazy 求值（读时计算、不跑定时任务），偏离锚点越远衰减越快；仅在裁决模型结算时落盘新值与时间戳
- **关系区间进度**：对话注入会告诉 AI 当前好感在所处关系区间内的积累百分比（刚进入/稳定/接近下一阶段/巅峰），对冲衰减带来的挫败感，让"刷好感"有可见的进度反馈
- **互动边界与过渡预告**：advance 模式下每个关系等级可配置 `boundary`（允许/回避的身体接触动作清单，注入对话）、`preview`（高进度时预告下一等级的入门动作）、`rule`（此等级的加分/扣分判定规则，后台结算用）——三者解耦对话行为与数值结算
- **人设性格摘要**：AI 自动从人设文本提取性格特征摘要，辅助裁决模型判断
- **人格隔离**：不同人格拥有独立的好感度记录
- **对话上下文集成**：可选集成 chat_memory 插件，按用户 ID 隔离读取对话历史
- **备份与恢复**：管理台可创建、查看、恢复并删除当前人格备份；清空和恢复操作自动保留保护备份

## 安装

```
/astrbot_plugin install https://github.com/W-Wolfycz/emotion_favour
```

## 文件结构

```
emotion_favour/
├── main.py              # 插件主体（Star 类、Hook、命令）
├── domain.py            # 纯情感计算、衰减与提示词构造（无 AstrBot/数据库依赖）
├── storage.py           # 数据库模型与管理（SQLModel + aiosqlite，兼容导出 domain 函数）
├── config.py            # 类型化配置读取与运行时校验
├── runtime.py           # 后台任务监督器与 keyed lock
├── migrate.py           # 配置版本链式迁移
├── utils.py             # 工具函数
├── log.py               # 包内日志出口（跟随 AstrBot 核心插件 logger）
├── _conf_schema.json    # 配置项定义
├── custom_t2i.html      # T2I 图片渲染模板
├── pages/webui/         # AstrBot Plugin Page 管理台
├── tests/               # 无 AstrBot 环境可运行的回归测试
├── docs/                # 版本改造与架构说明
├── metadata.yaml
├── requirements.txt
└── LOGO.png
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
| `config_version` | 配置版本号（**自动管理**，插件启动时链式迁移旧配置） | 0 |

### 关系映射配置（relationship_config）

两种模式：

- **simple**：`simple_list` 给一组关系名（从低到高），插件按好感度范围自动均分。仅注入关系名，无边界/规则。
- **advance**：`advance_config` 用 `template_list` 表单化编辑每个等级。点击「添加项」选「好感度等级」模板，每个等级含六个字段：

| 字段 | 注入位置 | 作用 |
|------|----------|------|
| `describe` / `min_value` / `max_value` | 关系名 + 区间匹配 | 等级名称；好感度落在 [min, max] 内则匹配此等级 |
| `boundary` | 对话 `<情感好感>` 标签 | 允许/回避的身体接触动作，决定 LLM 在当前好感度下的接触尺度 |
| `preview` | 对话（高进度时） | 本区间积累到 80% 以上时额外注入下一等级的入门动作预告 |
| `rule` | 后台裁决 prompt | 此等级下什么样的互动会扣分/加分，只影响数值结算 |

> ⚠️ 区间不可重叠——建议等级间留 1 间隙（A=0-30、B=31-60、C=61-100）。老版 JSON 字符串配置会在启动时自动迁移到 template_list 格式。

### 高级配置（advanced_config）

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `favour_change_min` / `favour_change_max` | 好感度单次变化范围 | -5 / 5 |
| `emotion_change_min` / `emotion_change_max` | 情感维度单次变化范围 | -10 / 5 |
| `history_rounds` | 裁决模型参考的近期对话轮数（0-10） | 0 |
| `use_chat_memory` | 启用 chat_memory 插件提供按用户隔离的对话上下文 | false |
| `emotion_decay_enabled` | 启用情感时间衰减 | true |
| `emotion_decay_rate_volatile` | 挥发组衰减率（surprise/anticipation） | 0.7 |
| `emotion_decay_rate_standard` | 标准组衰减率（joy/anger/disgust/fear） | 0.85 |
| `emotion_decay_rate_sticky` | 黏附组衰减率（trust/sadness/guilt/shame/pride/envy） | 0.93 |
| `favour_decay_enabled` | 启用好感度时间衰减（与情感衰减独立，lazy 求值） | true |
| `favour_decay_anchor` | 好感度衰减锚点（高于锚点部分向其回归，≤ 锚点不衰减） | 50 |
| `backup_retention_days` | JSON 业务备份保留天数；0 为永久保留，迁移 `.db` 不自动删除 | 0 |
| `judge_request_max_retries` | 裁决 LLM 请求最大尝试次数（含首次，1-10；默认 5，设为 1 表示失败不重试） | 5 |
| `settlement_timeout_seconds` | 好感度结算与人设摘要共用的裁决超时（秒） | 60 |
| `terminate_flush_timeout_seconds` | 终止时等待短任务完成的最长时间（秒） | 8 |
| `admin_default_favour` | 特殊用户初始好感度 | 50 |
| `favour_envoys` | 特殊关系用户 ID 列表 | [] |

### 日志配置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `log_with_bot_id` | 日志前缀附加机器人实例 ID（多 Bot 环境便于定位） | false |

日志等级不再由插件配置控制：请到 WebUI 插件详情页调整本插件的日志等级，
运行期生效、无需重启（AstrBot 4.27+ 支持按插件独立设置）。

### 名字解析规则

展示名通过当前消息平台即时查询，不再维护本地群名单缓存：

- **群聊**：当前群名片（card）> 平台昵称（nickname）> 用户 ID
- **私聊**：平台昵称（nickname）> 用户 ID
- `/emotion list` 每页最多即时查询 20 位用户；查询失败时名称列留空，ID 列仍正常显示

## 命令

| 命令 | 权限 | 说明 |
|------|------|------|
| `/emotion me` | 所有成员 | 查看自己的好感度与情感维度 |
| `/查询印象`、`/印象`、`/查询好感度`、`/好感度` | 所有成员 | 个人查询兼容入口，只能查看自己 |
| `/emotion query <@用户或ID>` | Bot管理员 | 查看指定用户的好感度与情感维度 |
| `/emotion list [页码]` | Bot管理员 | 好感度排行榜（T2I 分页渲染） |
| `/emotion set <@用户或ID> <数值>` | Bot管理员 | 直接设置用户好感度 |
| `/emotion mood <@用户或ID> [维度] <数值>` | Bot管理员 | 修改单个或全部情感维度 |
| `/emotion clear <@用户或ID>` | Bot管理员 | 清空指定用户数据（二次确认+自动备份） |
| `/emotion clear-all` | Bot管理员 | 清空当前人格所有数据（二次确认+自动备份） |
| `/emotion persona` | Bot管理员 | 查看当前人格的 AI 性格摘要 |
| `/emotion persona-clear` | Bot管理员 | 清除性格摘要缓存 |
| `/emotion help` | 所有成员 | 按当前权限显示指令帮助 |

权限固定为两层：所有成员可以查询自己的印象和查看帮助；查询他人、全局查询、修改、清空以及人设摘要管理仅允许 AstrBot `admins_id` 中的 Bot 管理员。插件不再提供高等级群员阈值、群管理员/群主命令列表或“允许查看他人印象的最低等级”等配置。

`/emotion clear-all` 会在警告中显示 resolved 当前人格，只有 30 秒内准确回复 `Accpet.` 才会继续；备份创建失败时不会删除数据。

## 管理台备份与恢复

Plugin Page 顶栏的备份按钮管理当前选中人格的数据：

- **创建备份**：把当前人格全部好感与 12 维情感写入 `plugin_data/emotion_favour/backups/` 下的 JSON 文件。
- **人格隔离**：列表只显示包含当前选中 `persona_id` 的备份，切换人格后可恢复列表会随之变化。
- **清空后可恢复**：人格的数据库记录全部删除后，只要仍有有效 JSON 备份，该人格仍会保留在可搜索选择器中；页面会记住上次选择，首次打开默认选择 `default`。
- **查看历史**：旧版 `/emotion clear`、`/emotion clear-all` 生成的 JSON 备份也会自动出现；单条备份展示用户、好感度和 12 维情感预览，批量备份仅显示条数。
- **恢复备份**：按 `persona_id + user_id` 合并覆盖备份值，不删除备份之外的现有用户。
- **恢复前保护**：恢复前只备份本次会被覆盖且当前已存在的用户；单条恢复保护单条，批量恢复保护实际冲突集合。保护备份失败则取消恢复，不为纯新增用户生成空备份。
- **编辑前保护**：管理台编辑现有记录且数值确实变化时，先生成该用户的 `pre_edit` 单条备份；备份失败则取消保存。新建记录没有旧值，不生成编辑前备份。
- **删除备份**：可删除当前人格专属的 JSON 备份文件，不影响当前数据库记录。
- **操作确认**：恢复和删除均在 Plugin Page 内二次确认；确认后才会发送对应请求，取消、Esc 或点击遮罩不会执行操作。
- **时间兼容**：新备份包含 UTC 时间存储标记；旧版 JSON 备份恢复时会按其生成格式解释历史时间，避免时间偏移影响好感度衰减。
- **并发保护**：备份会等待已进入数据库的短写入完成；清空、恢复和情感重置会提升人格 epoch，单用户清空会提升记录 epoch。操作开始前已排队或仍在裁决中的旧写入会被丢弃，不会在管理操作后覆盖或重新创建数据。
- **保留期限**：`backup_retention_days=0` 时永久保留；设为正数后，插件初始化和新建备份时清理过期的有效 JSON 业务备份。正在恢复的文件受保护，SQLite 迁移备份和未知/损坏 JSON 不会自动删除。

迁移前生成的完整 SQLite `.db` 备份不在管理台列表中，仍只用于数据库迁移故障恢复。

## 会话重置行为

执行 `/reset` 或 `/new` 时，自动重置当前人格下**所有用户**的情感维度归零（好感度保留）。通过 AstrBot 内置的 `_clean_group_context_session` 标记精确检测，无需轮询。

## 好感度时间衰减

好感度数值随时间向**锚点**回归，与 12 维情感衰减完全独立。

- **lazy 求值**：所有读位点（LLM 注入、个人查询、`/emotion list`、裁决结算）调用纯函数实时计算衰减值，不写库——避免无意义的 UPDATE
- **唯一落盘点**：裁决模型结算应用 delta 时，通过 `update_favour` 自然刷新 `updated_at`，下一轮读取从此时间戳起算 Δt
- **σ 简化**：只在 `favour > anchor` 时衰减（高好感会被时间冲淡）；≤ 锚点的好感度（含负面印象）不被拉抬
- **非线性 ODE**：`γ(x) = γ₀·(1 + u/S)`，偏离锚点越远衰减率越高；封闭解 `x₁ = E + K·S/(1−K)`，`K = a·e^{−γ₀·Δt}`
- **内置参数**：γ₀=0.005/tick（1 tick=3 min，向下取整）、S=50；锚点 `favour_decay_anchor` 用户可配置
- **管理台排序**：按好感度排序时，会先用同一时间快照计算全部候选记录的衰减后有效值，再进行全局排序和分页，页面顺序与实际生效数值一致。

## 关系区间进度

为对冲好感度衰减带来的挫败感，对话注入会附带「当前好感在所处关系区间内的积累进度」，让 AI 表现出"快升级了"的细微差异，给刷好感的用户正向反馈。

- **进度计算**：`(z - x) / (y - x)`，[x, y] 为当前关系区间，输出 0-100% + 4 档语义（刚进入 / 稳定 / 接近下一阶段 / 巅峰）
- **正负向统一**：语义均为"向下一阶段过渡的进度"——「厌恶」区间 90% 表示"即将松动到「反感」"，而非"厌恶加深"
- **区间对齐**：与 `_get_relationship` 共用 idx 切分逻辑；simple 模式最后区间 y 取 `max_favour_value` 覆盖端点；advance 模式直接从配置取 [min_value, max_value]
- **特殊场景**：特殊用户覆盖关系不注入进度行；挚爱区间（y==x）直接显示 100% 巅峰
- **与衰减协同**：进度跟着衰减后的 transient 值走，长期不互动 → 进度掉 → AI 表现冷淡，形成"需要维护关系"的自然反馈循环

## 特殊关系用户

`admin_default_relationship` 非空时，仅 `favour_envoys` 列表中明确填写的用户走特殊关系路径；Bot 管理员、群主和群管理员不会自动加入：

- **关系名展示**：始终展示 `admin_default_relationship`（如「特殊」），不随好感度变化
- **advance 模式按最高等级处理**：`boundary` / `rule` / `preview` 取自配置中 min_value 最高的等级（如「挚爱」），`is_max_tier=True`，进度行展示「已达最高等级」
- **后台结算**：`current_rule` 用最高等级的 rule，但描述仍标注 `admin_default_relationship` 名称
- **初始好感**：仅列表内用户使用 `admin_default_favour`；管理员自己仍使用普通 `default_favour`
- **simple 模式**：仅替换关系名，无边界/规则字段（simple 模式不配置 tier）

效果：特殊用户对话注入走最高等级的接触边界（深度亲密），但关系标签仍读作自定义的「特殊」名，不混淆用户。

## 对话上下文

裁决模型参考的对话历史支持两种来源：

- **AstrBot 自带上下文**（默认）：从 `conv.history` 读取，群聊场景下所有用户共享上下文
- **chat_memory 插件**（需安装）：按 `UMO + conversation_id + user_id` 隔离读取，群内每个用户独立历史

> 启用 chat_memory 前需安装其正式版 **≥ 1.0.0**（<https://github.com/W-Wolfycz/chat_memory>）。插件会按当前 resolved persona ID 读取严格配对的 LLM 历史；未安装、版本不兼容或查询失败时，本轮自动回退到 AstrBot 自带上下文。

> 回退路径仅保证裁决仍有历史可用；AstrBot 原生历史不提供与 ChatMemory 等价的 persona 隔离，群聊中也可能包含共享上下文。

## 依赖

- Python ≥ 3.10
- AstrBot ≥ 4.16（已在 4.26.3 验证）
- sqlmodel, aiosqlite, aiofiles
- PyYAML（Web API 元数据读取）
- playwright（自定义 T2I 渲染；插件首次需要 T2I 且检测到 Chromium 缺失时，会使用当前 AstrBot Python 环境自动执行 `python -m playwright install chromium`；包缺失、下载失败或浏览器启动失败时自动回退 AstrBot 内置 T2I）

## 3.4.0 架构说明

- 数据库中的时间统一存为 UTC naive；首次发现旧库时会在数据目录的 `backups/` 下自动生成 SQLite 备份，再按服务器配置时区迁移。
- `after_message_sent` 只用于排队结算，不代表平台确认送达；主动消息和流式路径仍不宣称“全量捕获”。
- 管理台通过 AstrBot Plugin Page Bridge 访问 `/api/plug/emotion_favour/...`；不需要手工处理短期 `asset_token`。
- T2I 继续使用本插件自定义 Playwright 模板，以保持现有渲染稳定性；浏览器由插件生命周期统一管理。
- 3.4.0 已删除关系调节/AI 劝说、群名单定时采集和 `rapidfuzz` 依赖；旧库中的 `group_roster` 表保留但不再读写。

## License

[MIT](LICENSE)
