"""domain.py 数值函数的固定种子属性测试（stdlib random，不引入 hypothesis）。

守的全是「数值悄悄算错」类静默失效：收益递减方向反了、好感衰减越过锚点或反向增长、
三档情感代谢率接错位置、档位百分比越界——这些判错都不会报错，界面/日志里只是一串
看起来合理的数字，要专门比对历史值才看得出来。

只喂契约内的输入域（cap > 0、current ∈ [0, cap]、时间戳取过去时刻、档位区间自洽），
不喂 NaN/Inf/负 cap 这类产品不会遇到的值，避免假阳性。固定种子保证可复现。

运行：
    python3 -m unittest tests.test_numeric_invariants -v
"""
import random
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from domain import (
    EMOTION_DIMENSIONS,
    EMOTION_GROUPS,
    apply_emotion_decay,
    compute_favour_decay,
    compute_tier_progress,
    diminish_delta,
    utc_now,
)

SEED = 20260926
SAMPLES = 300
NOW = datetime(2026, 6, 1, 12, 0, 0)
CAP = 100  # 生产调用 `diminish_delta(current, delta)` 的默认 cap


def _record(**overrides):
    """12 维齐全的 FavourRecord 替身（默认全 0）。"""
    values = {dimension: 0 for dimension in EMOTION_DIMENSIONS}
    values.update(overrides)
    return SimpleNamespace(**values)


class DiminishDeltaTest(unittest.TestCase):
    def test_non_positive_delta_passes_through_unchanged(self):
        """守「负向变化也被递减/被钳成 0」：delta <= 0（扣分）必须原样返回，否则惩罚被
        悄悄抹平——负面情绪消解不掉、冒犯不再扣好感，界面上只是数值不动。"""
        rng = random.Random(SEED)
        for _ in range(SAMPLES):
            current = rng.randint(0, CAP)
            delta = rng.randint(-CAP, 0)
            self.assertEqual(diminish_delta(current, delta, CAP), delta)

    def test_positive_gain_is_bounded_and_monotone(self):
        """守收益递减的两个方向错误：正向变化不得超过申请值（出现放大就是衰减系数写反）、
        不得变成 0（正反馈被吃光），且 current 越大收益越小（单调非增）。
        方向反了不会报错，只是高好感时涨得更快/永远涨不动。

        cap 固定取 100（`diminish_delta(current, delta)` 的唯一生产调用口径）；函数在
        cap 不是 4 的倍数时会在阈值上沿轻微放大（cap=190/current=142 → 101 > 99），
        该区间产品走不到，喂进来只会制造假阳性。"""
        rng = random.Random(SEED + 1)
        for _ in range(SAMPLES):
            current = rng.randint(0, CAP)
            delta = rng.randint(1, CAP)
            result = diminish_delta(current, delta, CAP)
            self.assertLessEqual(result, delta, f"current={current} delta={delta}")
            self.assertGreaterEqual(result, 1)

            higher = rng.randint(current, CAP)
            self.assertGreaterEqual(
                result,
                diminish_delta(higher, delta, CAP),
                f"收益必须随 current 单调非增: current={current} higher={higher}",
            )


class FavourDecayTest(unittest.TestCase):
    def test_decay_never_crosses_anchor_and_returns_input_when_not_above(self):
        """守「衰减越过锚点或反向增长」：favour > anchor 时结果必须仍 >= anchor 且不超过
        当前值（衰减只能单向靠近锚点）；favour <= anchor 时原样返回。越过锚点会让好感
        跌破人格设定的下限，只有读库比对才发现。"""
        rng = random.Random(SEED + 2)
        for _ in range(SAMPLES):
            anchor = rng.randint(0, 200)
            favour = anchor + rng.randint(1, 400)
            updated_at = NOW - timedelta(hours=rng.uniform(0.1, 24 * 90))
            result = compute_favour_decay(favour, updated_at, anchor=anchor, now=NOW)
            self.assertGreaterEqual(result, anchor)
            self.assertLessEqual(result, favour)

        # 已在锚点或锚点以下：不受时间影响，原样返回
        for favour in (0, 50, 100):
            self.assertEqual(
                compute_favour_decay(favour, NOW - timedelta(days=30), anchor=100, now=NOW),
                favour,
            )
        # 同一时刻（elapsed_ticks <= 0）不得衰减
        self.assertEqual(compute_favour_decay(300, NOW, anchor=100, now=NOW), 300)

    def test_decay_is_monotone_and_converges_to_anchor(self):
        """守「衰减方向/速率写错」：时间越久结果必须越靠近 anchor（单调非增），足够久后
        收敛到 anchor。速率或指数符号写错时数值依然「看起来像在衰减」，只是永远差一截。"""
        anchor, favour = 100, 600
        durations = [timedelta(0), timedelta(hours=1), timedelta(hours=6),
                     timedelta(days=1), timedelta(days=7), timedelta(days=365)]
        results = [
            compute_favour_decay(favour, NOW - elapsed, anchor=anchor, now=NOW)
            for elapsed in durations
        ]
        self.assertEqual(results[0], favour)
        for earlier, later in zip(results, results[1:]):
            self.assertLessEqual(later, earlier, f"时间越久必须越接近锚点: {results}")
        self.assertEqual(results[-1], anchor, "一年后应已衰减到锚点")


class EmotionDecayTest(unittest.TestCase):
    def test_decay_gated_by_min_hours(self):
        """守「未到 min_hours 就衰减」：阈值内的记录必须原样返回空，否则每轮结算都在
        偷偷扣情感，短间隔聊天时数值一路下滑而没有任何提示。"""
        recent = _record(joy=100, trust=100, surprise=100)
        recent.updated_at = utc_now() - timedelta(hours=2)
        decayed, _elapsed = apply_emotion_decay(recent, 0.5, 0.8, 0.95, 6)
        self.assertEqual(decayed, {})

        old = _record(joy=100, trust=100, surprise=100)
        old.updated_at = utc_now() - timedelta(hours=8)
        decayed, _elapsed = apply_emotion_decay(old, 0.5, 0.8, 0.95, 6)
        self.assertTrue(decayed, "超过 min_hours 后必须产生衰减")

    def test_decay_never_raises_emotions_and_zero_record_is_noop(self):
        """守「衰减变成增长/给 0 也生成更新」：衰减后必须 0 <= new <= old；全 0 记录必须
        返回空（否则每轮都会以全 0 delta 刷新 updated_at，把衰减基线一直往后推）。"""
        record = _record(joy=100, trust=37, fear=1, sadness=0, envy=64)
        record.updated_at = utc_now() - timedelta(hours=12)
        decayed, _elapsed = apply_emotion_decay(record, 0.5, 0.8, 0.95, 1)
        self.assertTrue(decayed)
        for dimension, value in decayed.items():
            self.assertLessEqual(value, getattr(record, dimension))
            self.assertGreaterEqual(value, 0)

        empty = _record()
        empty.updated_at = utc_now() - timedelta(hours=12)
        decayed_empty, elapsed = apply_emotion_decay(empty, 0.5, 0.8, 0.95, 1)
        self.assertEqual(decayed_empty, {})
        self.assertGreater(elapsed, 11.0, "返回的 elapsed 应是真实经过小时数")

    def test_emotion_residue_always_converges_to_zero(self):
        """守「低位残留永不归零」：取整方式若让小值四舍五入回自己（信任停在 7、喜悦停在 3、
        惊讶停在 1），残留会永久留在库里，注入面板里一直挂着这几个维度，界面上只是几个
        看起来正常的小数字。按 1h 间隔反复衰减，任何维度都必须收敛到 0。"""
        for dimension in ("trust", "joy", "surprise"):
            value = 100
            for _ in range(400):
                record = _record(**{dimension: value})
                record.updated_at = utc_now() - timedelta(hours=1)
                decayed, _elapsed = apply_emotion_decay(record, 0.7, 0.85, 0.93, 1)
                value = decayed.get(dimension, value)
            self.assertEqual(value, 0, f"{dimension} 反复衰减后必须归零，实际停在 {value}")

    def test_group_rates_apply_to_their_own_dimensions(self):
        """守三档代谢率接错位：volatile/standard/sticky 的倍率必须落在各自声明的维度上。
        接错后瞬时情绪长期不散、稳定情绪迅速消失，数值依旧平滑衰减，只有比对档位才发现。"""
        hours = 4
        rates = {"volatile": 0.5, "standard": 0.8, "sticky": 0.95}
        sample = {"surprise": 100, "joy": 100, "trust": 100}
        record = _record(**sample)
        record.updated_at = utc_now() - timedelta(hours=hours)
        decayed, _elapsed = apply_emotion_decay(
            record, rates["volatile"], rates["standard"], rates["sticky"], 1)

        # 只比"落在哪一档"（±1 容差吸收取整方式），不把取整细节也锁进来
        for dimension in sample:
            if dimension in EMOTION_GROUPS["volatile"]:
                group = "volatile"
            elif dimension in EMOTION_GROUPS["sticky"]:
                group = "sticky"
            else:
                group = "standard"
            self.assertAlmostEqual(
                decayed[dimension], 100 * rates[group] ** hours,
                delta=1.0, msg=dimension,
            )
        self.assertLess(decayed["surprise"], decayed["joy"])
        self.assertLess(decayed["joy"], decayed["trust"])


class TierProgressTest(unittest.TestCase):
    def test_percent_stays_within_bounds_and_max_tier_uses_upper_bound(self):
        """守档位百分比越界与满级档位用错右边界：百分比必须恒在 0-100 且随好感单调不减；
        满级档要用 max_favour_value 而不是 tier_max 作右边界，否则达标前后百分比静默失真
        （这里取 tier_max 与 max 之间的好感值，两种算法结果不同）。"""
        tier_min, tier_max, max_favour_value = 600, 700, 800
        percents = []
        for favour in range(0, 901, 25):
            percent, _hint = compute_tier_progress(
                favour, tier_min=tier_min, tier_max=tier_max,
                max_favour_value=max_favour_value, next_tier_min=701,
            )
            self.assertGreaterEqual(percent, 0.0)
            self.assertLessEqual(percent, 100.0)
            percents.append((favour, percent))
            if favour < tier_min:
                self.assertEqual(percent, 0.0)

        ordered = [percent for _favour, percent in percents]
        self.assertEqual(ordered, sorted(ordered), "百分比必须随好感单调不减")

        max_tier_percent, hint = compute_tier_progress(
            750, tier_min=tier_min, tier_max=tier_max,
            max_favour_value=max_favour_value, next_tier_min=None,
        )
        self.assertEqual(max_tier_percent, 75.0, "满级档右边界应为 max_favour_value")
        self.assertEqual(hint, "已满级")


if __name__ == "__main__":
    unittest.main()
