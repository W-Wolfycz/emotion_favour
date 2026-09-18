# emotion_favour 代码逻辑测试

当前源码版本：3.5.0。

## 定位与边界

`tests/` 只用于验证函数、配置、权限等可重复的纯代码逻辑，不导入真实
`astrbot.core`，也不依赖 AstrBot 源码、backend 或插件运行依赖（sqlmodel、
aiofiles 等）；需要真实运行时的行为由部署端验收。

自动化测试允许使用 mock 和源码片段提取，但不得依赖或修改真实插件数据，
也不得执行以下操作：

- 启动、停止或重载真实 AstrBot
- 调用真实 LLM / Provider 或发送平台消息
- 下载 Playwright Chromium 或访问外部网络
- 驱动真实 WebUI、浏览器或 Plugin Page Bridge
- 把真实账号、群组、Persona 或消息内容写入夹具

## 本地执行

在 `emotion_favour` 根目录下执行：

```bash
# 全部测试
python -m unittest discover -s tests -v

# 单个模块
python -m unittest tests.test_migrate -v

# 单个 TestCase
python -m unittest tests.test_migrate.TestMigrateV0ToV1 -v

# WebUI 纯逻辑
node --test tests/test_persona_logic.js
```

## 测试模块

| 文件 | 验证内容 | 隔离方式 |
|------|----------|----------|
| `support.py` | 共享测试基础设施：源码方法/常量提取、事件与任务桩 | 被其它用例导入，自身无用例 |
| `test_ctx_clean.py` | 历史清洗保留 tool 配对（孤儿 function_call_output 的 400 事故回归） | 从 `main.py` 提取静态方法 |
| `test_emotion_html.py` | 出图 HTML 的用户文本转义 | 直接导入 `domain.py` |
| `test_storage_injection.py` | 注入内容的静默失效点：simple 模式不带档位、满级不留预告、有效好感度与禁令分层 | 直接导入 `domain.py` |
| `test_tier_extras.py` | 关系档位与特殊用户覆盖 | 从 `main.py` 提取目标方法 |
| `test_tier_script.py` | 档位描述解析（含档位键归一化）与缓存新鲜度 | 直接导入 `domain.py` |
| `test_tier_script_flow.py` | 档位描述编排的回归点：force 失败、部分解析、空指纹、缓存命中 | 从 `main.py` 提取目标方法 |
| `test_query_impl.py` | 档位描述只在自己查询时出现 | 从 `main.py` 提取命令方法 |
| `test_list_command.py` | 排行榜昵称模式的取数分支 | 从 `main.py` 提取命令方法 |
| `test_help_menu.py` | 帮助菜单权限分支 | 从 `main.py` 提取命令方法 |
| `test_runtime.py` | 锁串行/回收与「清空后旧写不得复活」 | 真实 asyncio，不 mock |
| `test_permission_policy.py` | 命令权限策略与特殊 ID 越权面 | 源码逻辑测试 |
| `test_playwright_support.py` | Chromium 缺失识别（误判会白等或装错环境） | mock 子进程，不下载浏览器 |
| `test_prompt_injection.py` | 注入幂等与临时面板不入历史 | 轻量假请求对象 |
| `test_admin_backup_logic.py` | 编辑变更检测（无变化不备份、有变化必须备份） | 直接导入 `domain.py` |
| `test_config.py` | 重试次数钳制、备份保留期、档位重叠校验 | 直接导入 `config.py` |
| `test_utils.py` | 过期文件清理的后缀/年龄过滤 | 从 `utils.py` 提取顶层函数 |
| `test_interaction_section.py` | 结算互动段落的角色对应 | 从 `domain.py` 提取顶层函数 |
| `test_persona_logic.js` | 初始人格回退、搜索过滤、键盘导航 | Node 内置测试运行器 |

## 部署端验收

以下行为不放进本目录的自动化测试，由部署端 Beta/体验测试确认：

- 插件真实加载、初始化、卸载和热重载
- 既有运行数据库的备份、迁移和数据保全
- 数据库迁移 runner 的成功、幂等、失败回滚、备份与 schema 校验
- 真实 Provider 响应、后台 LLM 任务及平台消息链路
- Playwright Chromium 安装、HTML 渲染和错误提示
- Plugin Page Bridge、WebUI 操作流程和视觉体验
- 与 ChatMemory 的真实插件发现、历史查询及降级行为

## 维护守则

- 只测「**改错了一眼看不出来**」的地方：静默失效（数字键被丢弃、缓存永不过期、
  描述被通用文案覆盖、转义漏掉）、重要不变量与曾经出过 bug 的回归点。
  跑一遍代码就能发现的（结构塌陷、报错、页面空白、分页错乱）不写测试。
- 新增或修改领域规则时，优先为纯函数补充用例。
- 数据库持久化与迁移逻辑不在本目录自动化覆盖，由部署端验收。
- 修改 Prompt 注入、权限或关系档位时，更新对应行为测试。
- 外部依赖统一使用 mock；不得把下载、联网或真实平台副作用引入测试。
- 插件类方法的提取与通用桩统一放 `support.py`，新测试文件不要各抄一套样板。
- 自动化测试通过只说明代码逻辑符合预期，不代表已完成 AstrBot 集成或用户体验验收。
