# emotion_favour 代码逻辑测试

当前源码版本：3.4.3。

## 定位与边界

`tests/` 只用于验证函数、配置、数据库迁移等可重复的代码逻辑。
测试套件由远端测试环境执行；部署端负责真实 AstrBot 集成和用户体验验收。

自动化测试允许使用临时 SQLite、mock 和源码片段提取，但不得依赖或修改真实
插件数据，也不得执行以下操作：

- 启动、停止或重载真实 AstrBot
- 调用真实 LLM / Provider 或发送平台消息
- 下载 Playwright Chromium 或访问外部网络
- 驱动真实 WebUI、浏览器或 Plugin Page Bridge
- 把真实账号、群组、Persona 或消息内容写入夹具

`test_storage_db.py` 使用临时目录和临时 SQLite 验证持久化逻辑；这属于代码逻辑测试，
不等同于部署端数据库迁移或运行时验收。远端必须安装插件运行依赖；依赖缺失或源码
导入失败应直接让测试失败，不能伪装成 skipped。

## 远端执行

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
| `test_migrate.py` | 配置迁移、坏数据处理和幂等性 | 直接导入标准库模块 |
| `test_permission_policy.py` | 固定权限策略和显式特殊用户规则 | 源码逻辑测试 |
| `test_playwright_support.py` | Chromium 缺失识别、安装命令和失败处理 | mock 子进程，不下载浏览器 |
| `test_prompt_injection.py` | Prompt marker、幂等注入和 system fallback | 轻量假请求对象 |
| `test_admin_backup_logic.py` | 管理台编辑变更检测，避免无变化时生成备份 | 直接导入纯领域函数 |
| `test_runtime.py` | `TaskSupervisor`、`KeyedLockPool`、人格写入屏障和记录 epoch 逻辑 | asyncio 与同步纯逻辑测试 |
| `test_storage_injection.py` | 情感面板、关系边界和提示词构造 | 从 `domain.py` 提取纯函数 |
| `test_tier_extras.py` | 关系档位、特殊用户覆盖和范围解析 | 从 `main.py` 提取目标方法 |
| `test_storage_db.py` | 临时库 CRUD、并发、局部备份恢复、保留期清理、清空后人格发现、UTC 迁移、热重载、migration runner、完整性检查和 schema validator | 临时目录与临时 SQLite |
| `test_persona_logic.js` | 初始人格选择、搜索过滤和键盘索引 | Node 内置测试运行器 |

## 部署端验收

以下行为不放进本目录的自动化测试，由部署端 Beta/体验测试确认：

- 插件真实加载、初始化、卸载和热重载
- 既有运行数据库的备份、迁移和数据保全
- 真实 Provider 响应、后台裁决及平台消息链路
- Playwright Chromium 安装、HTML 渲染和错误提示
- Plugin Page Bridge、WebUI 操作流程和视觉体验
- 与 ChatMemory 的真实插件发现、历史查询及降级行为

## 维护守则

- 新增或修改领域规则时，优先为纯函数补充用例。
- 新增数据库迁移时，必须覆盖成功、幂等、失败回滚、备份与 schema 校验。
- 修改 Prompt 注入、权限或关系档位时，更新对应行为测试。
- 外部依赖统一使用 mock；不得把下载、联网或真实平台副作用引入测试。
- 只能在临时目录创建数据库和文件，禁止读取或写入部署端 `plugin_data`。
- 自动化测试通过只说明代码逻辑符合预期，不代表已完成 AstrBot 集成或用户体验验收。
