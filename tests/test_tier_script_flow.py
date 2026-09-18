"""关系档位描述的编排层测试：`_ensure_tier_scripts` / `_tier_script_for` / `regenerate`。

覆盖复检指出的关键分支：缓存命中不重生成、缺失时生成并落库、force 失败必须
如实返回空（否则 `/emotion regenerate` 会误报成功）、部分解析时保留其余档位、
人格取不到时绝不生成、后台刷新只在缓存不可用时触发。

运行：
    python3 -m unittest tests.test_tier_script_flow -v
"""
import asyncio
import hashlib
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import (  # noqa: E402
    EventStub,
    LocksStub,
    PLUGIN_DIR,
    TasksStub,
    load_methods,
    make_logger,
)

sys.path.insert(0, str(PLUGIN_DIR))
from domain import (  # noqa: E402
    build_tier_script_prompt,
    parse_tier_script_response,
    tier_scripts_fresh,
)

METHODS = (
    "_persona_prompt_text",
    "_resolve_generation_provider",
    "_persona_hash",
    "_generate_tier_scripts",
    "_ensure_tier_scripts",
    "_warm_tier_scripts",
    "_tier_script_for",
    "_refresh_tier_scripts",
    "regenerate_tier_scripts",
)

TIERS = [
    {"min_value": 0, "max_value": 30, "describe": "失望"},
    {"min_value": 31, "max_value": 100, "describe": "陌生"},
    {"min_value": 101, "max_value": 350, "describe": "友好"},
]

PERSONA_PROMPT = "她说话简短，习惯用反问。"

class _Db:
    def __init__(self, cached=None, persona_ids=("persona_demo",)):
        self.cached = {key: dict(value) for key, value in (cached or {}).items()}
        self.replaced = []
        self.persona_ids = list(persona_ids)

    async def get_distinct_personas(self):
        return list(self.persona_ids)

    async def get_tier_scripts(self, _persona_id):
        return {key: dict(value) for key, value in self.cached.items()}

    async def replace_tier_scripts(self, _persona_id, scripts, persona_hash):
        self.replaced.append(({key: dict(v) for key, v in scripts.items()}, persona_hash))
        self.cached = {
            key: {
                "label": value.get("label", ""),
                "script": value.get("script", ""),
                "hash": value.get("hash") or persona_hash,
            }
            for key, value in scripts.items()
        }

class _Stub:
    _TIER_SCRIPT_WARM_DELAY = 0  # 预热等待在测试里不需要

    def __init__(
        self, *, cached=None, tiers=None, llm=None, provider="provider_demo",
        permitted=True, persona_error=None, tier_script_enabled=True,
    ):
        self.db = _Db(cached)
        self.tier_script_enabled = tier_script_enabled
        self._tiers = TIERS if tiers is None else tiers
        self._llm = llm
        self._provider = provider
        self.permitted = permitted
        self.persona_error = persona_error
        self.llm_provider = "provider_demo" if provider else ""
        self.llm_calls = []
        self._tasks = TasksStub()
        self._record_locks = LocksStub()
        self.context = SimpleNamespace(get_current_chat_provider_id=self._current_provider_id)
        self.persona_mgr = SimpleNamespace(get_persona_v3_by_id=self._persona)
        self.min_favour_value = 0
        self.max_favour_value = 800

    def _persona(self, _persona_id):
        if self.persona_error:
            raise self.persona_error
        return {"prompt": PERSONA_PROMPT}

    def _advance_items(self):
        return list(self._tiers)

    async def _llm_generate(self, *, provider_id, prompt, timeout=None):
        self.llm_calls.append({"provider_id": provider_id, "prompt": prompt})
        if isinstance(self._llm, Exception):
            raise self._llm
        return SimpleNamespace(completion_text=self._llm)

    async def _current_provider_id(self, *, umo=""):
        self.provider_umo = umo
        return self._provider

    async def _check_command_permission(self, _event, _command):
        return self.permitted

    async def _get_persona_id(self, _event):
        return "persona_demo"

    def _tag(self, _event):
        return "[EmotionFavour]"

def _llm_payload(pairs):
    return json.dumps({"tiers": [{"min_value": key, "script": text} for key, text in pairs]})

def _fresh_cache():
    persona_hash = hashlib.sha256(PERSONA_PROMPT.encode("utf-8")).hexdigest()
    return {
        str(tier["min_value"]): {
            "label": tier["describe"],
            "script": f"{tier['describe']}时的心里话。",
            "hash": persona_hash,
        }
        for tier in TIERS
    }

class TierScriptFlowTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = load_methods(
            METHODS,
            namespace={
                "hashlib": hashlib,
                "logger": make_logger("test_tier_script_flow"),
                "build_tier_script_prompt": build_tier_script_prompt,
                "parse_tier_script_response": parse_tier_script_response,
                "tier_scripts_fresh": tier_scripts_fresh,
                "AstrMessageEvent": object,
            },
            stub_cls=_Stub,
            static_names=("_persona_hash",),
        )

    def call(self, name, stub, *args, **kwargs):
        return asyncio.run(getattr(stub, name)(*args, **kwargs))

class EnsureTierScriptsTest(TierScriptFlowTestBase):
    def test_fresh_cache_skips_generation(self):
        stub = _Stub(cached=_fresh_cache(), llm=_llm_payload([(0, "不该被调用")]))
        scripts = self.call("_ensure_tier_scripts", stub, "persona_demo", "provider_demo")
        self.assertEqual(stub.llm_calls, [])
        self.assertEqual(len(scripts), 3)
        self.assertEqual(stub.db.replaced, [])

    def test_force_failure_returns_empty(self):
        """回归：force 失败不能拿旧缓存冒充成功，否则 /emotion regenerate 误报。"""
        for label, llm in {"调用异常": RuntimeError("provider 超时"), "输出不可解析": "不是 JSON"}.items():
            with self.subTest(label):
                stub = _Stub(cached=_fresh_cache(), llm=llm)
                scripts = self.call(
                    "_ensure_tier_scripts", stub, "persona_demo", "provider_demo", force=True
                )
                self.assertEqual(scripts, {})
                self.assertEqual(stub.db.replaced, [])

    def test_partial_result_merges_and_keeps_retry_signal(self):
        """部分解析不能删掉其余档位；档位名变化的档位要保留旧指纹以便重试。"""
        stale = {
            "0": {"label": "旧档位名", "script": "旧描述。", "hash": "old-hash"},
            "31": {"label": "陌生", "script": "旧的陌生档。", "hash": "old-hash"},
            "101": {"label": "友好", "script": "旧的友好档。", "hash": "old-hash"},
        }
        stub = _Stub(cached=stale, llm=_llm_payload([(101, "重写的友好档。")]))
        with self.assertLogs("test_tier_script_flow", level="WARNING") as logs:
            self.call("_ensure_tier_scripts", stub, "persona_demo", "provider_demo")
        written, _hash = stub.db.replaced[0]
        self.assertEqual(len(written), 3, "部分解析不能把其余档位删掉")
        self.assertEqual(written["101"]["script"], "重写的友好档。")
        self.assertEqual(written["0"]["script"], "旧描述。")
        self.assertEqual(written["0"]["hash"], "old-hash", "档位名变了要留待重试")
        # 档位名未变的档位打上当前指纹，避免「模型漏档位 → 每次查询都重复调 LLM」
        self.assertEqual(written["31"]["hash"], written["101"]["hash"])
        self.assertTrue(any("覆盖不全" in line for line in logs.output))

    def test_empty_persona_hash_never_generates(self):
        """人格设定取不到时不能生成：兜底文案会覆盖人格专属描述，空指纹还会一直命中。"""
        for label, cached in {"有旧缓存": _fresh_cache(), "无缓存": None}.items():
            with self.subTest(label):
                stub = _Stub(cached=cached, llm=_llm_payload([(0, "通用文案")]))
                stub.persona_error = RuntimeError("persona manager 不可用")
                with self.assertLogs("test_tier_script_flow", level="WARNING") as logs:
                    scripts = self.call(
                        "_ensure_tier_scripts", stub, "persona_demo", "provider_demo"
                    )
                self.assertEqual(stub.llm_calls, [])
                self.assertEqual(stub.db.replaced, [])
                self.assertEqual(len(scripts), len(cached or {}))
                self.assertTrue(any("取不到人格设定" in line for line in logs.output))
                # force 同样要如实返回空：否则 /emotion regenerate 会拿旧缓存报成功
                forced = self.call(
                    "_ensure_tier_scripts", stub, "persona_demo", "provider_demo", force=True
                )
                self.assertEqual(forced, {})
                self.assertEqual(stub.llm_calls, [])

class WarmTierScriptsTest(TierScriptFlowTestBase):
    """启动预热：只为缺失的人格补齐，开关/配置缺失时安静跳过。"""

    def test_generates_only_for_personas_missing_scripts(self):
        stub = _Stub(llm=_llm_payload([(0, "a"), (31, "b"), (101, "c")]))
        self.call("_warm_tier_scripts", stub)
        self.assertEqual(len(stub.llm_calls), 1)
        self.assertEqual(len(stub.db.replaced), 1)

    def test_skips_when_cache_is_fresh(self):
        stub = _Stub(cached=_fresh_cache(), llm=_llm_payload([(0, "不该调用")]))
        self.call("_warm_tier_scripts", stub)
        self.assertEqual(stub.llm_calls, [])

    def test_skips_when_disabled_or_unconfigured(self):
        """配置被静默忽略会白烧 LLM 调用：开关关、没档位、没后台模型都必须跳过。"""
        cases = {
            "开关关闭": _Stub(tier_script_enabled=False),
            "没有档位配置": _Stub(tiers=[]),
            "未配置后台任务模型": _Stub(provider=""),
        }
        for label, stub in cases.items():
            with self.subTest(label):
                self.call("_warm_tier_scripts", stub)
                self.assertEqual(stub.llm_calls, [])

    def test_failure_does_not_escape(self):
        """预热在后台跑，异常必须自己吞掉，否则会变成未处理任务异常。"""
        stub = _Stub(llm=RuntimeError("provider 超时"))

        async def boom():
            raise RuntimeError("database is locked")

        stub.db.get_distinct_personas = boom
        self.call("_warm_tier_scripts", stub)  # 不抛即通过


class TierScriptForTest(TierScriptFlowTestBase):
    """出图路径上的取用：命中直接展示，未命中交后台补齐、不能阻塞出图。"""

    def test_hit_returns_script_without_background_task(self):
        stub = _Stub(cached=_fresh_cache())
        text = self.call("_tier_script_for", stub, EventStub(), "persona_demo", TIERS[0])
        self.assertEqual(text, "失望时的心里话。")
        self.assertEqual(stub._tasks.spawned, [])

    def test_miss_returns_empty_and_spawns_refresh(self):
        stub = _Stub()
        text = self.call("_tier_script_for", stub, EventStub(), "persona_demo", TIERS[0])
        self.assertEqual(text, "")
        self.assertEqual(len(stub._tasks.spawned), 1)


class RegenerateCommandTest(TierScriptFlowTestBase):
    """/emotion regenerate 的命令层输出：失败绝不能报成功。"""

    def _regenerate(self, stub):
        async def collect():
            return [item async for item in stub.regenerate_tier_scripts(EventStub())]

        return asyncio.run(collect())

    def test_failure_is_reported_not_swallowed(self):
        """回归：force 失败时曾回落到旧缓存并打印「✅ 已为 N 个档位生成描述」，
        实际一个字都没重写；LLM 失败与落库失败两条路径都必须报失败。"""
        cases = {
            "LLM 失败": _Stub(cached=_fresh_cache(), llm=RuntimeError("provider 超时")),
            "落库失败": _Stub(llm=_llm_payload([(0, "保持距离。")])),
        }
        for label, stub in cases.items():
            with self.subTest(label):
                if label == "落库失败":
                    async def boom(*_args, **_kwargs):
                        raise RuntimeError("database is locked")

                    stub.db.replace_tier_scripts = boom
                results = self._regenerate(stub)
                self.assertIn("生成失败", results[-1][1])
                self.assertNotIn("已为", results[-1][1])

if __name__ == "__main__":
    unittest.main()
