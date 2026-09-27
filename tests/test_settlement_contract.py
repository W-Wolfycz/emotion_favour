"""结算契约回归：main.py 结算 prompt 里的 <example> 必须仍能被同文件的解析器读懂。

prompt 的 <example> 与解析器（`re.search(r'<result>.*?</result>')` + ElementTree +
change/emotions 标签名）是两份独立文本：只改一边（例如 prompt 写成 <changes>）不会
报错、不会崩溃，只会让之后每一轮结算都静默解析出 0——好感与情感从此不再增长，而
日志里只有「结算无变化」。所有可见症状都要等很久、且要与别的因素比对才浮现。

做法：用 _shared.extract_method 提取整个 `_calculate_favour_bg`（解析逻辑内联在这个
长方法里，没有独立函数），绑到桩类上执行；桩把真实 prompt 抓下来，取出 prompt 自带的
<example> 原样当作模型回复喂回去（不硬编码示例内容，示例被改测试跟着走），断言落库
效果等于示例自己声明的数值。

桩按当前 `_calculate_favour_bg` 的调用面写死（provider 解析与 WebUI 结算计数用替身）：
产品重命名或改签名时本文件会直接报错，不是静默失效，按新调用面补齐桩即可。

运行：
    python3 -m unittest tests.test_settlement_contract -v
"""
import asyncio
import importlib.util
import re
import traceback
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime
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

from domain import (  # noqa: E402
    EMOTION_DIMENSIONS,
    EMOTION_DISPLAY_NAMES,
    build_emotion_panel,
    build_interaction_section,
    diminish_delta,
    apply_scar,
    apply_scar_decay,
    utc_now,
)

LOGGER = _shared.make_logger("test_settlement_contract")

# 哨兵：让桩把 prompt 自带的 <example> 当作模型回复回灌，实现「示例 ↔ 解析器」闭环
_ECHO_EXAMPLE = object()

BASE_FAVOUR = 100


class _Event(_shared.EventStub):
    def get_sender_id(self):
        return "10001"


class _Record:
    """FavourRecord 替身：结算路径要读 favour、updated_at 与 12 维情感。"""

    favour = BASE_FAVOUR
    updated_at = datetime(2026, 1, 1)
    joy = 0
    trust = 0
    fear = 0
    surprise = 0
    sadness = 0
    disgust = 0
    anger = 0
    anticipation = 0
    pride = 0
    guilt = 0
    shame = 0
    envy = 0
    scar_joy = 0
    scar_trust = 0
    scar_fear = 0
    scar_surprise = 0
    scar_sadness = 0
    scar_disgust = 0
    scar_anger = 0
    scar_anticipation = 0
    scar_pride = 0
    scar_guilt = 0
    scar_shame = 0
    scar_envy = 0


class _Db:
    def __init__(self):
        self.record = _Record()

    async def get_favour(self, _persona_id, _user_id):
        return self.record


class _SettlementStub:
    """只提供 `_calculate_favour_bg` 真正会用到的属性与方法。"""

    def __init__(self, reply, *, favour_change_max=3, emotion_change_max=5, decay_loss=0,
                 scar_enabled=False, scar_penalty_k=2.0, scar_repair_k2=0.5):
        self.db = _Db()
        self.reply = reply
        self.prompts = []
        self.settlements = []
        self.stats = []
        self.example_body = None
        self.decay_loss = decay_loss
        self.llm_provider = "provider_demo"
        self.favour_mode = "normal"
        self.history_rounds = 0
        self.relationship_mode = ""
        self.emotion_decay_enabled = False
        self.emotion_decay_min_hours = 1.0
        self.favour_decay_enabled = True
        self.scar_enabled = scar_enabled
        self.scar_penalty_k = scar_penalty_k
        self.scar_repair_k2 = scar_repair_k2
        self.favour_change_min = -favour_change_max
        self.favour_change_max = favour_change_max
        self.emotion_change_min = -emotion_change_max
        self.emotion_change_max = emotion_change_max
        self.min_favour_value = 0
        self.max_favour_value = 800

    def _tag(self, _event):
        return "[EmotionFavour]"

    async def _resolve_generation_provider(self, event):
        """解析结算模型的接线（走 llm_provider 或当前会话模型）不在本文件守护范围内；
        这里固定回一个可用 provider，让流程走到 prompt 与解析段。"""
        return (self.llm_provider or "").strip() or "provider_demo"

    def _bump_settlement_stat(self, persona_id, outcome, error="", *, clamped=0):
        """WebUI 健康度计数不在本文件守护范围内，只记录调用。"""
        self.stats.append((persona_id, outcome, clamped))

    async def _get_persona_id(self, _event):
        return "persona_demo"

    async def _get_persona_summary(self, _persona_id, _provider_id):
        return ""

    def _decay_favour_value(self, record):
        # decay_loss > 0 时表示「这一轮本该落盘一个更低的衰减基线」：解析失败的回复若被
        # 当成「无变化」，就会把这条基线写下去（见 test_*_writes_nothing）。
        return record.favour - self.decay_loss

    async def _llm_generate(self, *, provider_id, prompt, timeout=None):
        self.prompts.append(prompt)
        if self.reply is _ECHO_EXAMPLE:
            self.example_body = _example_body(prompt)
            return SimpleNamespace(completion_text=self.example_body)
        return SimpleNamespace(completion_text=self.reply)

    async def _update_settlement_record(
        self, persona_id, user_id, mutation_epoch, *,
        favour=None, emotion_updates=None, scar_updates=None,
    ):
        self.settlements.append({
            "persona_id": persona_id,
            "user_id": user_id,
            "favour": favour,
            "emotion_updates": emotion_updates,
            "scar_updates": scar_updates,
        })
        return True


def _example_body(prompt: str) -> str:
    """取出真实结算 prompt 里自带的 <example> 片段（不硬编码示例内容）。"""
    match = re.search(r"<example>(.*?)</example>", prompt, re.DOTALL)
    if not match:
        raise AssertionError("结算 prompt 里已找不到 <example> 块")
    return match.group(1).strip()


class SettlementContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = _shared.load_methods(
            ("_calculate_favour_bg",),
            namespace={
                "re": re,
                "traceback": traceback,
                "logger": LOGGER,
                "EMOTION_DIMENSIONS": EMOTION_DIMENSIONS,
                "EMOTION_DISPLAY_NAMES": EMOTION_DISPLAY_NAMES,
                "build_emotion_panel": build_emotion_panel,
                "build_interaction_section": build_interaction_section,
                "diminish_delta": diminish_delta,
                "apply_scar": apply_scar,
                "apply_scar_decay": apply_scar_decay,
                "AstrMessageEvent": object,
            },
            stub_cls=_SettlementStub,
        )

    def settle(self, reply, *, decay_loss=0):
        stub = _SettlementStub(reply, decay_loss=decay_loss)
        asyncio.run(stub._calculate_favour_bg(
            _Event(), "用户说了点什么", "角色回了一句",
            persona_id="persona_demo", mutation_epoch=1,
        ))
        return stub

    def test_scar_factor_reaches_the_settlement_write(self):
        """守「伤痕接进结算路径」：开启且该维有伤痕时，写入的 delta 必须被放大、
        并带上伤痕增量。接线断了不报错，只是机制静默失效——开关打开也等于没开。"""
        stub = _SettlementStub(
            "<result><reasoning>r</reasoning><change>0</change>"
            "<emotions><trust>-6</trust></emotions></result>",
            emotion_change_max=8,
            scar_enabled=True,
        )
        stub.db.record.scar_trust = -80
        # 记录时间戳设为当前：否则桩里固定的旧时间会先触发一次伤痕衰减写入，
        # 把本用例要看的结算挤到第二位
        stub.db.record.updated_at = utc_now()
        asyncio.run(stub._calculate_favour_bg(
            _Event(), "用户说了点什么", "角色回了一句",
            persona_id="persona_demo", mutation_epoch=1,
        ))
        settlement = stub.settlements[0]
        # 伤痕 -80、信任受伤权重 1.0、k=2：因子 1+(2-1)×0.8=1.8 → -6×1.8≈-11
        self.assertEqual(settlement["emotion_updates"], {"trust": -11})
        self.assertEqual(settlement["scar_updates"], {"trust": -6})

    def example_expectation(self, stub):
        """从示例文本自身推出期望效果：标签缺失就地 fail（= prompt 与解析器已分叉）。"""
        self.assertIsNotNone(stub.example_body, "桩没拿到 prompt 的 <example> 片段")
        match = re.search(r"<result>.*?</result>", stub.example_body, re.DOTALL)
        self.assertIsNotNone(match, "prompt 示例里没有完整的 <result> 块")
        root = ET.fromstring(match.group(0))

        change_text = root.findtext("change")
        if change_text is None:
            self.fail("prompt 示例的 <result> 里没有解析器要读的 <change> 标签")
        change = int(round(float(change_text)))
        expected_change = max(stub.favour_change_min, min(stub.favour_change_max, change))

        emotions_elem = root.find("emotions")
        self.assertIsNotNone(emotions_elem, "prompt 示例的 <result> 里没有 <emotions> 容器")
        expected_emotions = {}
        for child in emotions_elem:
            value = int(child.text or 0)
            value = max(stub.emotion_change_min, min(stub.emotion_change_max, value))
            # 记录桩 12 维全 0，diminish_delta(0, v) 恒为 v；未知维度按契约丢弃
            if value != 0 and child.tag.lower() in EMOTION_DIMENSIONS:
                expected_emotions[child.tag.lower()] = value
        return expected_change, expected_emotions

    def test_prompt_example_round_trip_settles(self):
        """守 prompt 示例与解析器标签分叉：改 prompt 的 <change>/<emotions> 而忘了改
        解析器（或反向）时，每轮结算都静默返回 0，好感与情感再也不动，不报错。"""
        with self.assertNoLogs(LOGGER, level="ERROR"):
            stub = self.settle(_ECHO_EXAMPLE)
        expected_change, expected_emotions = self.example_expectation(stub)
        self.assertNotEqual(expected_change, 0, "prompt 示例必须能被解析出非零好感变化")
        self.assertTrue(expected_emotions, "prompt 示例必须能被解析出情感变化")

        self.assertEqual(len(stub.settlements), 1, "示例应产生一次结算写入")
        settlement = stub.settlements[0]
        self.assertEqual(settlement["favour"], BASE_FAVOUR + expected_change)
        self.assertEqual(settlement["emotion_updates"], expected_emotions)

    def test_change_and_emotions_clamped_to_configured_range(self):
        """守 clamp 被删/写错：模型幻觉出的巨幅数值会被整段写进库，单轮好感跳几十点；
        库里的数字看着合理，只有比对配置上下限才发现。两个方向都要钳。"""
        high = self.settle(
            "<result><reasoning>r</reasoning><change>999</change>"
            "<emotions><joy>999</joy></emotions></result>"
        )
        self.assertEqual(high.settlements[0]["favour"], BASE_FAVOUR + 3)
        self.assertEqual(high.settlements[0]["emotion_updates"], {"joy": 5})

        low = self.settle(
            "<result><reasoning>r</reasoning><change>-999</change>"
            "<emotions><joy>-999</joy></emotions></result>"
        )
        self.assertEqual(low.settlements[0]["favour"], BASE_FAVOUR - 3)
        self.assertEqual(low.settlements[0]["emotion_updates"], {"joy": -5})

    def test_unknown_dimension_is_dropped(self):
        """守维度白名单被删：模型编出的未知维度名会一路进到落库参数里（写不进去或写坏
        列），既不报错也没有任何界面症状。"""
        stub = self.settle(
            "<result><reasoning>r</reasoning><change>0</change>"
            "<emotions><jealousy>5</jealousy><joy>2</joy></emotions></result>"
        )
        self.assertEqual(stub.settlements[0]["emotion_updates"], {"joy": 2})
        self.assertEqual(stub.settlements[0]["favour"], BASE_FAVOUR)

    def test_reply_without_result_block_writes_nothing(self):
        """守「找不到 XML 仍走写路径」：模型跑题时不能产生任何结算写入。桩让衰减基线
        差 5 点，所以一旦解析失败被当成「无变化」，那条伪造的基线就会落盘并刷新
        updated_at——衰减起算点被凭空吞掉，日志里只说「结算无变化」。"""
        stub = self.settle("我不知道该怎么评估这次互动。", decay_loss=5)
        self.assertEqual(stub.settlements, [])

    def test_broken_xml_never_raises_and_never_writes(self):
        """守「解析失败被当成空结果落库」：坏 XML / 非数字 change 都必须停在解析层，
        既不写库（同上：会以无变化分支刷新衰减基线）也不把异常抛给后台任务。"""
        replies = {
            "未闭合标签": "<result><change>3</change><emotions></result>",
            "非数字 change": "<result><change>很多</change></result>",
            "截断输出": "<result><change>3</change>",
        }
        for label, reply in replies.items():
            with self.subTest(label):
                stub = self.settle(reply, decay_loss=5)
                self.assertEqual(stub.settlements, [], f"{label} 不应产生写入")

    def test_float_change_rounds_instead_of_truncating(self):
        """守 `int(round(float(...)))` 被简写成 int()：模型输出 2.6 / -2.6 时结算值
        悄悄偏 1 点（长期单向偏小），日志里只是一个正常数字。"""
        positive = self.settle(
            "<result><reasoning>r</reasoning><change>2.6</change></result>")
        self.assertEqual(positive.settlements[0]["favour"], BASE_FAVOUR + 3)
        negative = self.settle(
            "<result><reasoning>r</reasoning><change>-2.6</change></result>")
        self.assertEqual(negative.settlements[0]["favour"], BASE_FAVOUR - 3)


if __name__ == "__main__":
    unittest.main()
