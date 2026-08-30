# emotion_favour 代码逻辑测试

当前源码版本：3.4.4。

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
| `test_config.py` | 配置默认值、规范化和关系档位校验 | 直接导入纯配置模块 |
| `test_ctx_clean.py` | 历史上下文清洗与 tool 调用配对保留 | 从 `main.py` 提取静态方法 |
| `test_migrate.py` | 配置迁移、坏数据处理和幂等性 | 直接导入标准库模块 |
| `test_settlement_catchup.py` | 结算互动段落构造与超长文本截断 | 从 `domain.py` 提取纯函数 |
| `test_utils.py` | 过期文件清理（T2I 渲染缓存回收） | 从 `utils.py` 提取纯函数 |
| `test_permission_policy.py` | 固定权限策略和显式特殊用户规则 | 源码逻辑测试 |
| `test_playwright_support.py` | Chromium 缺失识别、安装命令和失败处理 | mock 子进程，不下载浏览器 |
| `test_prompt_injection.py` | Prompt marker、幂等注入和 system fallback | 轻量假请求对象 |
| `test_admin_backup_logic.py` | 管理台编辑变更检测，避免无变化时生成备份 | 直接导入纯领域函数 |
| `test_runtime.py` | `TaskSupervisor`、`KeyedLockPool`、人格写入屏障和记录 epoch 逻辑 | asyncio 与同步纯逻辑测试 |
| `test_storage_injection.py` | 情感面板、关系边界和提示词构造 | 从 `domain.py` 提取纯函数 |
| `test_tier_extras.py` | 关系档位、特殊用户覆盖和范围解析 | 从 `main.py` 提取目标方法 |
| `test_persona_logic.js` | 初始人格选择、搜索过滤和键盘索引 | Node 内置测试运行器 |

## 部署端验收

以下行为不放进本目录的自动化测试，由部署端 Beta/体验测试确认：

- 插件真实加载、初始化、卸载和热重载
- 既有运行数据库的备份、迁移和数据保全
- 数据库迁移 runner 的成功、幂等、失败回滚、备份与 schema 校验
- 真实 Provider 响应、后台裁决及平台消息链路
- Playwright Chromium 安装、HTML 渲染和错误提示
- Plugin Page Bridge、WebUI 操作流程和视觉体验
- 与 ChatMemory 的真实插件发现、历史查询及降级行为

## 维护守则

- 新增或修改领域规则时，优先为纯函数补充用例。
- 数据库持久化与迁移逻辑不在本目录自动化覆盖，由部署端验收。
- 修改 Prompt 注入、权限或关系档位时，更新对应行为测试。
- 外部依赖统一使用 mock；不得把下载、联网或真实平台副作用引入测试。
- 自动化测试通过只说明代码逻辑符合预期，不代表已完成 AstrBot 集成或用户体验验收。
