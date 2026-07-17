# emotion_favour 单元测试

当前源码版本：3.4.0。

## 运行

```bash
# 单个文件
python3 tests/test_migrate.py
python3 tests/test_storage_injection.py
python3 tests/test_tier_extras.py

# 全部（在 emotion_favour 根目录下）
python3 -m unittest discover -s tests -v

# 仅跑某个 TestCase
python3 -m unittest tests.test_migrate.TestMigrateV0ToV1 -v
```

## 测试策略

按 plugin_local_testing 记忆，AstrBot 插件代码本地测试**不走 import**（依赖链 sqlmodel/sqlalchemy/aiofiles 等未装）。三个测试文件分别采用不同策略：

| 文件 | 策略 | 原因 |
|------|------|------|
| `test_migrate.py` | 直接 import | migrate.py 只依赖标准库（json/logging/typing） |
| `test_storage_injection.py` | 字符串 exec 提取函数 | domain.py 纯函数可独立验证，避免引入数据库 |
| `test_tier_extras.py` | 字符串 exec 提取类方法 | main.py 顶部 import astrbot，按记忆绕开 |
| `test_architecture.py` | AST/文本/JSON 静态检查 | 验证 Hook、生命周期、Bridge、DTO 与 schema 约束 |

字符串提取细节：

- **`test_storage_injection.py`**：`_extract_fn` 扫描 domain.py 源码，找到 `def fn_name(` 起到下一个顶层 def（行首无缩进）止，把这段源码作为字符串 `exec` 到独立命名空间。
- **`test_tier_extras.py`**：`_extract_method` 扫描 main.py 源码，找到类内 `    def method(` 起到下一个同缩进 def 或类外结构止，`textwrap.dedent` 去掉类缩进后 `exec`。所有方法共享命名空间（含 `json` / `logger` stub）。Stub 用 `types.SimpleNamespace` 构造，提供 `admin_default_relationship` / `_special_user_ids` / `relationship_advance_raw` 等字段，方法绑定到 stub 上。

## 覆盖范围

### test_permission_policy.py

- 普通成员与 Bot 管理员的固定命令权限
- 已删除的权限配置不参与运行时
- 只有显式特殊用户 ID 获得特殊初始好感

### test_migrate.py（10 个测试）

- v0→v1 老配置迁移：JSON 字符串 → list，原字段保留 + `__template_key=custom` 注入
- v1 已迁移：no-op（字节级相同）
- JSON 损坏：不 bump 版本号，下次启动可重试
- 缺 config_version：视为 v0
- 非 dict 输入：安全返回
- 已是 list：不强制加 `__template_key`
- relationship_config 缺失：视为符合新版本
- 框架健康度：版本号与注册表一致、每版有迁移函数、CURRENT 版本幂等

### test_storage_injection.py（17 个测试）

- 进度计算：4 档分段（< 20 / 20-80 / 80-95 / ≥ 95）+ 退化区间 + clamp
- boundary 注入：simple 模式跳过 / 非空注入 / 空跳过
- preview 触发：80% 阈值 / preview 空 / next_describe 空 / 最高等级不触发
- 最高等级：进度行附加「已达最高等级」
- 机制脱敏：关键词出现在禁止行
- XML 标签完整性

### test_tier_extras.py（19 个测试）

- `_get_max_tier`：取 min_value 最高的等级 / 空配置返回 None / 打乱顺序不影响
- `_get_tier_extras` 特殊用户覆盖：boundary/rule/preview 用最高等级 / is_max_tier=True / next_describe="" / 非特殊用户走原 _find_tier 逻辑 / 双条件（admin_default_relationship 非空 + uid 在显式列表）缺一不可 / 空配置兜底
- `_get_relationship_range` 特殊用户覆盖：advance 模式返回最高等级 (x, y) / favour 高低不影响 / simple 模式仍返回 None / 空配置返回 None
- `_is_special_override`：显式 ID 列表判定

## 维护守则

- 新加迁移版本（v2、v3...）时，必须在 test_migrate.py 加对应 TestCase
- 改 build_injection_prompt 的注入分支时，必须更新 test_storage_injection.py 的对应测试
- 改 main.py 的 _get_max_tier / _get_tier_extras / _get_relationship_range / _is_special_override 时，必须更新 test_tier_extras.py 的对应测试
- 测试**不依赖** AstrBot 环境，纯本地可跑——若发现测试需要 stub 整个 astrbot 生态才能 import，违反 plugin_local_testing 策略，应改用字符串 exec 方案
