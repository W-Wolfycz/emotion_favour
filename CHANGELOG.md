# 更新日志

## 3.4.0
2026-07-17

本次版本集中修复 P0/P1/P2 架构问题，并保持配置版本 `config_version=1` 不变；已有配置无需重新填写。

### P0：生命周期、并发与数据安全

- 将数据库、脚本准备、Playwright 预热和 Web API 注册移入异步 `initialize()`；初始化失败会回收已创建资源并交回 AstrBot 失败插件处理。
- 最终插件类直接实现 `terminate()`；统一停止受管后台任务、关闭自定义 Playwright 和释放数据库连接。
- 所有后台任务纳入 `TaskSupervisor`，对同一 `(persona_id, user_id)` 使用 keyed lock 串行化结算、命令和 WebUI 写入。
- SQLite 启用 WAL、busy timeout、foreign keys；情感增量、记录创建和字段写入改为原子 UPSERT/SQL 更新。
- 旧服务器本地 naive 时间首次启动自动备份并迁移为 UTC naive（按配置时区解释），API 展示仍转换为本地时间。

### P1：Hook、LLM 和 persona 一致性

- 结算 Hook 改为 `after_message_sent`，只表示发送流程结束/发送尝试完成，不宣称平台已确认送达。
- 结算和人设摘要统一使用并发上限与共用超时；补充 persona/目标用户锁，避免竞态和缓存击穿。
- 人设摘要和 favour 记录均使用当前会话解析出的 persona ID，不再误用默认 persona。
- ChatMemory 历史查询显式传当前 resolved `persona_id`，查询不可用时回退 AstrBot 自带上下文。

### P2：模块边界与管理台

- 新增无 AstrBot/数据库依赖的 `domain.py`，集中纯情感计算、衰减和提示词构造；`storage.py` 保留兼容导出。
- Web API 增加 persona、分页和排序字段；WebUI 只走 `window.AstrBotPluginPage` Bridge，不再自行拼接 `asset_token` 或调用 `fetch`。
- WebUI 的人格切换和新增记录支持搜索现有人格。
- WebUI 增加服务端用户 ID 搜索、每页数量选择和页码跳转。
- 好感展示区分衰减后的当前有效值与数据库原值；编辑窗口支持情感归零、恢复打开时数值和未保存修改提醒。
- 自定义 Playwright T2I 按现有用户选择保留，增加 15 秒网络/页面超时、浏览器锁、页面 finally 关闭和带版本/宽度的缓存名。
- 补充架构静态测试、运行期并发测试、SQLite 并发/UTC 迁移测试和改造说明文档。

### 功能收敛与权限策略

- 删除关系调节/AI 劝说、群名单观察与定时同步、`persuasion_config`、`roster_matcher.py` 和 `rapidfuzz`；旧 `group_roster` 表只作历史保留，不再创建、迁移或读写。
- 普通成员仅可自查和查看帮助；查询他人、全局查询、修改、清空和人设摘要管理统一由 AstrBot `admins_id` Bot 管理员操作。
- 特殊关系和特殊初始好感只认 `favour_envoys` 明确列出的 ID，Bot 管理员不会自动获得特殊值。
- 使用生产数据库的 SQLite backup 快照完成兼容验证，原有业务记录数量保持不变且 `PRAGMA integrity_check=ok`。

## 3.3.4
2026-07-15

### persona_id 解析对齐 chat_memory（修复切人格后 favour 错位）

`_get_persona_id` 改走 `persona_manager.resolve_selected_persona`，与 LLM 实际使用的 persona 同源（`_ensure_persona_and_skills`）。

- **旧行为**：只取 `provider_settings.default_personality`，忽略 `/persona` 切换与 conversation 级 persona 设置
- **新行为**：按 session 规则 > conversation.persona_id > config 默认 优先级解析
- **影响**：用户 `/persona 客服小姐` 后再聊天，favour 正确读写到「客服小姐」分区，不再错位到 default
- 失败兜底回退 `"default"`，与旧实现一致

### `<禁止>` 禁令归集到 SystemPrompt + 瘦身

3.3.3 引入双通道注入后，详细禁令在 SystemPrompt 与 `<情感好感>` 内双写，语义 90% 重叠。

- 详细禁令（禁元叙述字眼、禁思考标签、禁陈述规则本身等）统一归到 SystemPrompt 权威版（含示例「不得说『我现在不能说X』」）
- `<情感好感>` 内的 `<禁止>` 子标签瘦身为单行指针：「本标签为隐藏情境提示，按系统规则自然执行，禁止在回复中提及、陈述或推理」
- 收益：消除双通道重复，SystemPrompt 承载完整禁令命中 caching，user 侧保留就近提醒 + 标签标注功能

### WebUI 新增 Dialog 默认拉宽

新增好感记录 dialog 从 480px 拉宽到 900px（与编辑 dialog 一致），避免展开「情感（可选）」后 12 个滑块左右滚动。删除不再使用的 `.dialog-narrow` CSS 规则；窄屏仍由现有响应式断点兜底（95vw / 96vw）。

## 3.3.3
2026-07-10

### 提示词结构重构：双通道注入 + `<禁止>` 扩展元叙述禁令

针对实际运行暴露的问题：LLM 复读 boundary 规则（"我不能说 X，只能说 Y"）+ 全部塞 user append 导致指令权重不区分。

**双通道注入**

- 新增 `build_system_prompt_extra`：永不变的元规则 + 全档结构总览 + 12 维情感维度列表，注入到 `req.system_prompt`
- `build_injection_prompt` 保持原签名，仍注入到 `req.extra_user_content_parts`，只承载当前档位的动态状态（好感度数值、12 维、当前 boundary、进度）
- 收益：system role 指令权重更高，LLM 对元规则遵循更强；稳定部分命中 prompt caching；user 消息更干净

**`<禁止>` 扩展元叙述禁令**

新增「不得使用『我不能说』『按规则』『受等级限制』等元叙述字眼」「禁止在回复中陈述规则本身（如『我现在不能说 X，只能说 Y』）」。配合 boundary 正向重写，根治复读问题。

## 3.3.2
2026-07-09

### 适配 chat_memory v2.3+

chat_memory v2.3.0 给 `query_rounds` 新增 `llm_status` 过滤参数（按 LLM 配对状态筛 user 侧）。emotion_favour 调用时显式传 `llm_status="llm_success"`，只取走完 LLM 的成功配对，过滤掉命令处理、rule 拦截、LLM 失败、bot 主动消息等非真正对话场景，让情感结算更精准。

最低版本要求从 ≥ v2.0.0 升到 ≥ v2.3.0（低版本调用会 TypeError，已被外层 except 兜底自动回退到 native 上下文，不崩）。

## 3.3.1
2026-07-08

### WebUI 印象管理台（首次上线）

新增 Plugin Pages 单页面管理台（`pages/webui/` + 后端 `web_api.py` 5 端点），bot admin 可在主 webui 直接查看与编辑印象数据。

- 列表单行展示 + 12 维情感 chip 摘要（按 group 染色），支持按 user_id / favour / 修改时间排序
- 编辑走原生 `<dialog>` 模态：favour 实时算关系名，12 滑块按组分组，确认才落盘
- 顶栏「+」新增记录，`create_only` 防覆盖（已存在则 409）

storage 层新增 `set_record_fields`（绝对值写入，不走 diminish_delta）与 `get_distinct_personas`。

### Bug 修复

- **清空命令 waiter 错捕**：原 `session_waiter` 默认 filter 仅按 session 过滤，群聊他人消息与「正在输入」事件都被当作二次确认。新增 `SenderSessionFilter` 按 sender 严格匹配，handler 内追加空消息兜底过滤。
- **WebUI persona 加载死锁**：防御逻辑 `if (!state.currentPersona) return` 与初始空字符串冲突，永远走不到赋值。改为非空时自动选第一个。
- **桥接 SDK 注入时序**：改为运行时 `getBridge()` 查询 + 轮询 `bridge.ready()` 握手；fetch 降级自动附加 `asset_token`，规避 CORS + 401。

## 3.3.0
2026-07-05

### 适配 chat_memory v2.0 接口

chat_memory v2.0 由「仅 LLM 触发存档」改为「全量捕获 + 7-tag 配对存储」，原 `query_history` 默认返回范围变化（会含 `non_llm` / `orphan` / `proactive` 等非配对消息），破坏下游「按 2 步长切轮」的解析假设。

- **改用 `query_rounds` 接口**：`_get_recent_history` 调用 chat_memory 时优先走 v2.0 新增的 `query_rounds`，DB 层用 `EXISTS` 子查询严格保证每轮 `[user, assistant]` 配对，过滤单边 user。flatten 后下游去重与解析循环零改动。
- **移除 v1.x 兼容路径**：`_resolve_chat_memory` 删除 `sys.modules` fallback（v2.0 已删除模块级 `query_history`，该路径成为 dead code）；探测属性由 `query_history` 改为 `query_rounds`，与 v2.0 唯一入口对齐。
- **不再支持 chat_memory v1.x**：用户需将 chat_memory 升级到 ≥ v2.0.0；未安装或版本过低时自动回退到 AstrBot 自带上下文，行为不变。

字段层：chat_memory v2.0 返回 dict 在原 `role` / `content` / `user_id` / `created_at` 基础上新增 `message_id` / `pair_id` / `tag` 三字段，emotion_favour 仅读旧字段，完全向后兼容。

## 3.2.0
2026-06-30

### 关系等级 template_list 改造

advance 模式的 `relationship_config.advance_config` 从原始 JSON 字符串迁移到 AstrBot 原生的 `template_list` 类型——每个等级是一个独立模板项，WebUI 配置页可表单化编辑，不再需要手写 JSON。

模板项 `好感度等级` 包含六个字段：

- `describe` / `min_value` / `max_value`：等级名称与好感度区间
- `boundary`：**互动边界**（注入对话）——允许/回避哪些身体接触动作，决定 LLM 在当前好感度下的接触尺度
- `preview`：**过渡预告**（高进度时注入）——本区间积累到 80% 以上时额外注入下一等级的入门动作，让等级切换更自然
- `rule`：**评估规则**（后台结算用）——此等级下什么样的互动会扣分/加分，只影响数值结算，不注入对话

`boundary` 与 `rule` 解耦：前者决定「角色此刻愿意做到哪一步」（对话行为），后者决定「此等级下加分/扣分判定」（数值结算）。

### 配置版本迁移系统

新增 `config_version` 字段（自动管理）+ `migrate.py` 链式迁移框架：

- **自动迁移**：插件启动时按版本号链式升级旧配置到 `CURRENT_CONFIG_VERSION`
- **v0 → v1**：老 JSON 字符串格式的 `advance_config` 自动解析为 list 并注入 `__template_key=custom`，原字段全部保留
- **幂等可重试**：JSON 损坏时不 bump 版本号，下次启动可重试；已是当前版本时无操作
- **持久化**：迁移发生时自动调 `save_config()` 落盘，避免内存迁移丢失
- **健康度自检**：测试覆盖「版本号与注册表一致」「每版有迁移函数」「CURRENT 版本幂等」

### 特殊用户覆盖按最高等级处理

显式特殊用户（`admin_default_relationship` 非空 + uid 在 `favour_envoys`）在 advance 模式下走「最高等级」分支：

- **对话注入**：`boundary` / `rule` / `preview` 取自最高等级（如「挚爱」的深度亲密边界），`is_max_tier=True`
- **关系名**：`<情感好感>` 标签里仍展示 `admin_default_relationship`（如「特殊」），不混淆用户
- **进度行**：展示「已达最高等级」而非具体百分比
- **后台结算**：`current_rule` 取最高等级的 rule，描述用 admin_default_relationship 名称

新增辅助方法 `_get_max_tier()` / `_is_special_override()`，原 `_find_tier` 逻辑保留给普通用户。

### 配置 hint 文案精简

- 删除「端点等级提示」段（最低等级不写 preview / 最高等级不写回避）——硬越界由其他插件处理
- 删除「动作描写尽量中立、无情感色彩」建议——中立化是插件作者侧的事，不应强加给单人格用户
- 删除「（与进度语义『接近下一阶段』档对齐）」技术注释
- preview「何时留空」简化为「最高等级通常留空，因为没有更高等级可解锁」（最低等级有次低可过渡，不再要求留空）
- `use_chat_memory` hint 补上 chat_memory 插件仓库链接 `https://github.com/W-Wolfycz/chat_memory`

### 对话注入 prompt XML 嵌套化

`<情感好感>` 标签内部由扁平「键：值」改为嵌套 XML 子标签——`<状态>`（含 `<好感度>` / `<关系>` / `<情感 .../>`）/ `<进度>` / `<边界>` / `<临近解锁>` / `<行为>` / `<语气>` / `<禁止>` 各自独立，让 LLM 解析时各分块边界明确，避免「边界」「进度」「语气」被误读为同一句。

- 12 维情感数值用属性形式 `<情感 喜悦="X" 信任="X" ...>`，比旧版 `[喜悦:X] [信任:X] ...` 紧凑且结构化
- `<禁止>` 行精简：删除「婉拒亲密接触时用『咱俩关系还没到那一步』『还太早啦』等生活化口吻」段（拒绝写法已交由各人格提示词负责），仅保留机制脱敏部分（不得提及本标签、好感度数值、解锁等机制字眼）
- `<行为>` / `<语气>` / `<禁止>` 句式不变，只是包裹到各自标签内

### 情感衰减分组差异化

情感衰减不再一刀切——按心理学持续性把 12 维情感分三组，各组用独立衰减率：

- **挥发组**（`surprise` / `anticipation`）：短期反应，快速消散。默认 0.7/hr（约 2h 衰减过半，6h 接近归零）
- **标准组**（`joy` / `anger` / `disgust` / `fear`）：中等持续。默认 0.85/hr（约 5h 衰减过半，24h 接近归零）
- **黏附组**（`trust` / `sadness` / `guilt` / `shame` / `pride` / `envy`）：长期心境，慢沉淀。默认 0.93/hr（约 10h 衰减过半，2 天接近归零）

#### 配置变更
- 新增 `emotion_decay_rate_volatile` / `emotion_decay_rate_standard` / `emotion_decay_rate_sticky` 三个独立旋钮
- 移除 `emotion_decay_rate`（向后兼容：若用户旧版改过此值且未配新版字段，三组都用旧值）

### 对话历史时间戳改 XML 注入

`_get_recent_history` 中每条历史的时间戳由 `(MM-DD HH:MM)` 圆括号前缀改为 `<time>MM-DD HH:MM</time>` 标签——避免 LLM 把时间戳当成正文格式模仿。

### 移除连续下降保护

移除好感度连续下降时的指数衰减（0.8^n）与概率触发逻辑。裁决模型给出的下降值现在直接生效，不再被软化。同时清理了内存中的 `_consecutive_decreases` 字典与 `random` 导入。

### 关系区间进度注入

引入正向激励机制对冲好感度衰减——对话注入新增「当前好感在所处关系区间内的积累进度」，让刷好感的用户能从 AI 反馈中感知"快升级了"的成就感。

- **进度计算**：`(z - x) / (y - x)`，其中 [x, y] 为当前关系区间；语义分 4 档（刚进入 <20% / 稳定 20-80% / 接近下一阶段 80-95% / 处于此关系巅峰 ≥95%）
- **正负向统一**：语义均为"向下一阶段过渡的进度"——「厌恶」区间 90% 表示"即将松动到「反感」"，而非"厌恶加深"
- **区间对齐**：`_get_relationship_range` 与 `_get_relationship` 共用 idx 逻辑，保证关系名与区间范围一致；simple 模式最后区间 y 取 `max_favour_value` 覆盖端点
- **特殊场景**：特殊用户覆盖关系不注入进度行；挚爱区间（y==x）直接显示 100% 巅峰

### 对话注入 XML 结构展开

`<情感好感>` 标签由"属性 + 子项"混合形式重构为**全展开子项**——`好感度` / `关系` / `情感` / `进度` / `行为` / `语气` / `禁止` 全部同级，可读性更强：

```xml
<情感好感>
  好感度：85
  关系：喜欢
  情感：[喜悦:60] [信任:50] ...
  进度：在「喜欢」区间内已积累 90%（接近下一阶段）
  行为：...
  语气：...
  禁止：...
</情感好感>
```

附带修复：注入显示的好感度数值从 `record.favour`（原值）改为传入的 `effective_favour`（默认衰减后值），与关系/进度对齐，也与 `/查询印象` 展示一致。

## 3.1.0
2026-06-20

### 关系调解（AI 劝说）

- 新增 **A 劝说 AI 调整对 B 的好感度**：群聊中 A 可通过多轮对话影响 AI 对第三方群成员（B）的情感，由裁决模型从对话中抽取目标名 + 调整意图，经模糊匹配解析到具体 user_id 后落地变更
- 新增 **裁决模型 XML 输出**：劝说判定走结构化 XML，避免 tool-call 复杂性；含正则兜底解析
- 新增 **渐进式劝说识别**：`persuasion_config.history_rounds` 让裁决模型参考最近 N 轮对话，识别"多轮逐步诋毁/夸奖"的渐进模式
- 安全闸：自指目标拒绝、单轮动作数上限、变化幅度钳制、模糊匹配分数门槛

### 群名单基础设施

- 新增 **群名单存储**（`group_roster` 表）：按群维护 user_id → {card, nickname} 映射，card 与 nickname 分列存储
- 新增 **被动观察**：发言即实时更新当前群的发言者名片
- 新增 **定时全量拉取**：每日可配置时间对所有已知群调用 `get_group_member_list` 全量同步
- 新增 **热加载即时拉取**：插件（重）加载后首个事件触发一次全量拉取，不等次日定时
- 新增 **rapidfuzz 模糊匹配工具**（`roster_matcher.py`）：综合 token_sort / partial / ratio 三种指标，给定查询字符串返回 Top-N 候选

### 名字解析统一规则

所有调用统一遵循"**当前群名片 > QQ 昵称 > 空**"：
- 群聊场景：严格按当前群取值，不跨群回退
- 私聊场景：跨群兜底但**仅取 nickname**，绝不展示其他群的群名片
- 模糊匹配：每个用户只对单个 effective name（card 非空则用 card）评分一次，"群昵称60/QQ昵称100 取60"

### 展示与渲染

- 全局印象查询拆分为 5 列表格：**用户 | ID | 好感值 | 关系 | 主导情感**
- 好感值/关系列居中对齐，表头通过 JS 同步 `<td>` 的对齐方式（兼容 marked.js 各版本的 align 表达）
- T2I 渲染宽度参数化：默认 800px，全局查询走 1200px，避免长 ID/关系换行

### 日志与诊断

- 拉取 API 返回空/非 list、API 返回但全无有效条目 → 升级为 warning（之前是 debug 或静默）
- 新增 `_log_debug` helper：storage 层也响应 `debug_to_info` 配置，覆盖 migration / pull / query 全链路诊断
- migration 自动从老 `display_name` 列复制到 `card`，老库平滑升级

### 好感度时间衰减（lazy 求值）

- 新增 **好感度数值随时间向锚点回归**：与 12 维情感衰减独立。非线性 ODE 封闭解 `γ(x)=γ₀·(1+u/S)`，偏离锚点越远衰减越快
- **lazy 求值，不跑定时任务**：所有读位点（LLM 注入、查询、裁决结算、劝说结算）调用 `compute_favour_decay` 纯函数算 transient 值，**不落盘**
- **唯一落盘点**：裁决模型结算（`evaluate_favour`）应用 delta 时通过 `update_favour` 自然刷新 `updated_at`，下一轮读取从此时间戳起算 Δt
- **σ 简化**：只在 `favour > anchor` 时衰减（σ=+1）——高好感会被时间冲淡，≤ 锚点的好感度（含负面印象）不被拉抬
- 参数：γ₀=0.005/tick（1 tick=3 min，向下取整）、S=50、锚点默认 50（用户可配置）
- 配置 `favour_decay_enabled` / `favour_decay_anchor`（位于 advanced_config，与 `emotion_decay_*` 并列）

### 依赖

- 新增 `rapidfuzz>=3.0.0`

---

## 3.0.0
2026-06-13

### 情感系统

- 新增 **12维情感模型**：喜悦、信任、恐惧、惊讶、悲伤、厌恶、愤怒、期待、得意、内疚、害羞、嫉妒，每维独立结算
- 新增 **情感代谢机制**：负面消解（愤怒/悲伤等随善意互动消退）+ 激情冷却（惊讶/害羞等瞬时情绪平稳后回落）
- 新增 **情感时间衰减**：长时间未互动的用户情感自然回归 baseline，防止情绪永久累积
- 新增 **连续下降保护**：好感度连续下降时指数衰减（0.8^n），低于 1 时转为概率触发，避免断崖式暴跌

### 裁决提示词

- 重构为 XML 结构（`<role>` / `<rule>` / `<ban>` / `<output>` / `<example>`），提升思考模型解析质量
- 新增 GALGAME / NORMAL / REALISTIC 三种判定模式，各自定义善意/冒犯的判定尺度
- 好感度与情感维度变化范围统一为 `-x~+y` 格式，简化模型理解
- 新增 `<ban>` 禁令与 one-shot 示例，减少误判

### 人设与上下文

- 新增 **人设性格摘要**：AI 自动从人设文本提取性格特征并缓存，辅助裁决模型判断角色行为倾向
- 新增 **对话历史参考**：裁决模型可参考最近 N 轮对话（`history_rounds` 配置），来源可选 AstrBot 自带上下文或 chat_memory 插件（按用户 ID 隔离）
- 新增 **上下文清洗**：注入前自动清理历史中残留的旧 `<情感好感>` 标签和 `<thought>` 块

### 会话行为

- `/reset` 与 `/new` 执行时自动重置当前人格下**所有用户**的情感维度归零（好感度保留），通过 `_clean_group_context_session` 精确检测

### 日志

- 统一所有日志前缀为 `[EmotionFavour]`
- 新增 `log_config` 配置组：
  - `log_with_bot_id`：日志附加机器人实例 ID
  - `debug_to_info`：debug 日志提级为 info 输出

### 渲染与安全

- 查询结果与全局排行支持 **T2I 图片渲染**（Playwright），防刷屏分页
- 清空操作需 **二次确认** + 自动 JSON 备份

---

## 2.0.0
2026-05-25

- 移除冷暴力功能
- 数据库按人格 ID（persona_id）隔离存储，不同人格独立好感度
- 好感度评估改为后台 LLM 调用（不再内联解析标签）
- 简化 LLM 注入，仅提供好感度数值和关系名称
- 新增裁判模型配置（judge_provider）
- 新增管理员/特使关系覆盖（admin_default_relationship）

---

## 1.3.0
2026-05-18

- 移除非全局选项
- 移除旧配置迁移
- 允许用户降低自身好感度
