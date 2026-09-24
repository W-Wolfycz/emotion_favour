"""关系档位描述的编排层测试：`_ensure_tier_scripts` / `_tier_script_for` / `regenerate`。

覆盖复检指出的关键分支：缓存命中不重生成、缺失时生成并落库、force 失败必须如实
返回空（否则 `/emotion regenerate` 会误报成功）、部分解析时保留其余档位、
人格取不到时绝不生成、后台刷新只在缓存不可用时触发。

运行：
    python3 -m unittest tests.test_tier_script_flow -v
"""
import asyncio
import hashlib
import importlib.util
import json
import unittest
from pathlib import Path
from types import SimpleNamespace


def _load_shared():
    """按路径加载 tests/_shared.py（pytest 下不能按包名 import）。"""
    path = Path(__file__).resolve().parent / "_shared.py"
    spec = importlib.util.spec_from_file_location("ef_tests_shared", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

_shared = _load_shared()
EventStub = _shared.EventStub
LocksStub = _shared.LocksStub
TasksStub = _shared.TasksStub
load_methods = _shared.load_methods
make_logger = _shared.make_logger

from domain import (  # noqa: E402
    build_tier_script_prompt,
    parse_tier_script_response,
    tier_scripts_fresh,
)
from runtime import KeyedLockPool  # noqa: E402

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
        permitted=True, persona_error=None, tier_script_enabled=True, db_factory=None,
    ):
        self.db = (db_factory or _Db)(cached)
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


class _CoordDb(_Db):
    """并发用例的协调桩：第二路（任务名 second）做外层检查时放行第一路的生成，
    确保两路都拿着「缓存过期」的判断进入锁，用来验证锁内会再查一次。"""

    def __init__(self, cached=None):
        super().__init__(cached)
        self.second_outer_check = asyncio.Event()

    async def get_tier_scripts(self, persona_id):
        task = asyncio.current_task()
        if task is not None and task.get_name() == "second":
            self.second_outer_check.set()
        return await super().get_tier_scripts(persona_id)


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
        """守「缓存命中仍重复生成」：新鲜缓存必须原样返回、不调 LLM、不重写库。
        判错不会报错，只在每次查询背后白烧一次生成调用。"""
        stub = _Stub(cached=_fresh_cache(), llm=_llm_payload([(0, "不该被调用")]))
        scripts = self.call("_ensure_tier_scripts", stub, "persona_demo", "provider_demo")
        self.assertEqual(stub.llm_calls, [])
        self.assertEqual(len(scripts), 3)
        self.assertEqual(stub.db.replaced, [])

    def test_concurrent_calls_generate_once(self):
        """守双检锁的锁内复检：两路同时看到过期缓存时，后进锁的一路必须重新确认，
        否则同一人格被生成两次（多烧一次 LLM、多写一次库），单线程查询看不出来。"""
        stub = _Stub(llm=_llm_payload([(0, "a"), (31, "b"), (101, "c")]),
                     db_factory=_CoordDb)
        stub._record_locks = KeyedLockPool()
        generate = stub._llm_generate

        async def blocking_generate(*, provider_id, prompt, timeout=None):
            result = await generate(provider_id=provider_id, prompt=prompt, timeout=timeout)
            # 等第二路也拿到「缓存过期」的判断再写库，制造双检锁窗口
            await asyncio.wait_for(stub.db.second_outer_check.wait(), timeout=2)
            return result

        stub._llm_generate = blocking_generate

        async def race():
            first = asyncio.create_task(
                stub._ensure_tier_scripts("persona_demo", "provider_demo"), name="first")
            second = asyncio.create_task(
                stub._ensure_tier_scripts("persona_demo", "provider_demo"), name="second")
            return await asyncio.gather(first, second)

        first_scripts, second_scripts = asyncio.run(race())
        self.assertEqual(len(stub.llm_calls), 1, "并发过期只该生成一次")
        self.assertEqual(len(stub.db.replaced), 1)
        self.assertEqual(first_scripts, second_scripts)

    def test_force_failure_returns_empty(self):
        """回归：force 失败不能拿旧缓存冒充成功，否则 /emotion regenerate 误报。
        调用异常与输出不可解析是两条不同的失败路径，都要如实返回空。"""
        for label, llm in {"调用异常": RuntimeError("provider 超时"), "输出不可解析": "不是 JSON"}.items():
            with self.subTest(label):
                stub = _Stub(cached=_fresh_cache(), llm=llm)
                scripts = self.call(
                    "_ensure_tier_scripts", stub, "persona_demo", "provider_demo", force=True
                )
                self.assertEqual(scripts, {})
                self.assertEqual(stub.db.replaced, [])

    def test_partial_result_merges_and_keeps_retry_signal(self):
        """守部分解析的两个静默面：不能把模型没返回的档位从库里删掉；档位名变化的
        档位要保留旧指纹以便下次重试，档位名未变的则打上当前指纹，避免每次查询
        都因为「模型漏档位」重复调 LLM。"""
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
        self.assertEqual(written["31"]["hash"], written["101"]["hash"])
        self.assertTrue(any("覆盖不全" in line for line in logs.output))

    def test_empty_persona_hash_never_generates(self):
        """守「取不到人格设定仍生成」：兜底文案会覆盖人格专属描述，且空指纹与后续
        空指纹互相匹配会一直命中有害缓存。有/无旧缓存两种情况都不许调 LLM，
        force 也必须如实返回空（否则 /emotion regenerate 拿旧缓存报成功）。"""
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
                forced = self.call(
                    "_ensure_tier_scripts", stub, "persona_demo", "provider_demo", force=True
                )
                self.assertEqual(forced, {})
                self.assertEqual(stub.llm_calls, [])


class WarmTierScriptsTest(TierScriptFlowTestBase):
    """启动预热：只为缺失的人格补齐，开关/后台模型缺失时安静跳过。"""

    def test_generates_only_for_personas_missing_scripts(self):
        """守「预热静默变成空转」：有人格记录但无描述时必须生成并落库一次。
        判错不会报错，只是启动补齐永远不生效（首次查询才临时生成）。"""
        stub = _Stub(llm=_llm_payload([(0, "a"), (31, "b"), (101, "c")]))
        self.call("_warm_tier_scripts", stub)
        self.assertEqual(len(stub.llm_calls), 1)
        self.assertEqual(len(stub.db.replaced), 1)

    def test_skips_when_cache_is_fresh(self):
        """守「预热重复生成」：缓存新鲜时不得调 LLM，否则每次启动都白烧一次调用。"""
        stub = _Stub(cached=_fresh_cache(), llm=_llm_payload([(0, "不该调用")]))
        self.call("_warm_tier_scripts", stub)
        self.assertEqual(stub.llm_calls, [])

    def test_skips_when_disabled_or_unconfigured(self):
        """守「配置被静默忽略」：开关关闭、未配后台任务模型时都不得发起生成调用。
        两者判错都不会报错，只会在启动时悄悄多出 LLM 开销。"""
        cases = {
            "开关关闭": _Stub(tier_script_enabled=False),
            "未配置后台任务模型": _Stub(provider=""),
        }
        for label, stub in cases.items():
            with self.subTest(label):
                self.call("_warm_tier_scripts", stub)
                self.assertEqual(stub.llm_calls, [])

    def test_failure_does_not_escape(self):
        """守预热的后台异常不外泄：它在后台任务里跑，抛出去只会变成启动期噪音
        （任务异常日志），还会中断后面人格的补齐。"""
        stub = _Stub(llm=RuntimeError("provider 超时"))

        async def boom():
            raise RuntimeError("database is locked")

        stub.db.get_distinct_personas = boom
        self.call("_warm_tier_scripts", stub)  # 不抛即通过


class TierScriptForTest(TierScriptFlowTestBase):
    """出图路径上的取用：命中直接展示，未命中交后台补齐、不能阻塞出图。"""

    def test_hit_returns_script_without_background_task(self):
        """守「命中仍起后台任务」：新鲜命中必须直接返回且不 spawn，否则每次出图都
        多一次无用的后台生成。"""
        stub = _Stub(cached=_fresh_cache())
        text = self.call("_tier_script_for", stub, EventStub(), "persona_demo", TIERS[0])
        self.assertEqual(text, "失望时的心里话。")
        self.assertEqual(stub._tasks.spawned, [])

    def test_miss_returns_empty_and_spawns_refresh(self):
        """守「缺失时不补生成」：未命中要返回空并交给后台补齐（不阻塞出图）。
        不 spawn 不会报错，只是该档位的描述永远不再生成。"""
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
