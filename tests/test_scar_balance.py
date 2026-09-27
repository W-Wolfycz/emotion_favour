"""domain.py 伤痕机制数值规则的静默失效点。

伤痕只影响注入给模型的情境与库里的数值，判错既不抛异常也不影响界面：极性反了会让
「被哄好的信任」累积伤痕、真正被激怒的愤怒不留痕；因子方向反了会让长期关系越处越脆；
门槛写错则伤痕要么永不出现、要么为一点小事就道歉。这些都要专门对着历史数值比对才
看得出来，所以逐条守。

运行：
    python3 -m unittest tests.test_scar_balance -v
"""
import importlib.util
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace


def _load_shared():
    """按路径加载 tests/_shared.py（pytest 下不能按包名 import）。"""
    path = Path(__file__).resolve().parent / "_shared.py"
    spec = importlib.util.spec_from_file_location("ef_tests_shared", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

_load_shared()  # 只为把插件目录放进 sys.path，下面的 domain 直接 import

from domain import (  # noqa: E402
    EMOTION_DIMENSIONS,
    apply_scar,
    apply_scar_decay,
    build_scar_hint,
    utc_now,
)


def _record(**overrides):
    """12 维情感 + 12 维伤痕齐全的记录替身（默认全 0 = 无历史）。"""
    values = {dim: 0 for dim in EMOTION_DIMENSIONS}
    values.update({f"scar_{dim}": 0 for dim in EMOTION_DIMENSIONS})
    values["favour"] = 100
    values["updated_at"] = datetime(2026, 1, 1)
    values.update(overrides)
    return SimpleNamespace(**values)


class ScarDirectionTest(unittest.TestCase):
    def test_damage_direction_follows_dimension_polarity(self):
        """守极性表：正向维下跌、负向维上涨才算受伤。方向反了不会报错，只会让被哄好的
        信任累积伤痕、而真正被激怒的愤怒不留痕——机制整体失效，数值却都看着合理。"""
        _, trust_scar = apply_scar(_record(), {"trust": -6}, k=2.0, k2=0.5)
        self.assertEqual(trust_scar, {"trust": -6})

        _, anger_scar = apply_scar(_record(), {"anger": 5}, k=2.0, k2=0.5)
        self.assertEqual(anger_scar, {"anger": -5})

        # 同一维的反方向是修复：只按 repair_rate 侵蚀伤痕，不记新伤
        _, healed = apply_scar(_record(scar_trust=-40), {"trust": 6}, k=2.0, k2=0.5)
        self.assertEqual(healed, {"trust": 2})

        _, calmed = apply_scar(_record(scar_anger=-40), {"anger": -5}, k=2.0, k2=0.5)
        self.assertEqual(calmed, {"anger": 2})

        # 没有伤痕时，善意互动不产生任何伤痕更新（没有"情分"侧可攒）
        _, nothing = apply_scar(_record(), {"trust": 6}, k=2.0, k2=0.5)
        self.assertEqual(nothing, {})

    def test_neutral_dimensions_pass_through_untouched(self):
        """守「不参与」三格：惊讶/内疚/害羞涨跌都不该改 delta、也不该动伤痕。
        把角色自身的情绪记成被伤害，会得出「因为害羞所以记仇」这类行为。"""
        for dim, delta in (("surprise", 5), ("guilt", -4), ("shame", 3)):
            adjusted, changes = apply_scar(
                _record(**{f"scar_{dim}": -80}), {dim: delta}, k=2.0, k2=0.5
            )
            self.assertEqual(adjusted, {dim: delta})
            self.assertEqual(changes, {})


class ScarFactorTest(unittest.TestCase):
    def test_deeper_scar_amplifies_damage_and_suppresses_repair(self):
        """守单调性：伤痕越深，同向伤害放大越多、正向修复压制越强。反向不会报错，
        只会让长期关系越处越脆或越处越甜，只有对着历史数值才看得出来。"""
        light, _ = apply_scar(_record(scar_trust=-20), {"trust": -6}, k=2.0, k2=0.5)
        deep, _ = apply_scar(_record(scar_trust=-80), {"trust": -6}, k=2.0, k2=0.5)
        self.assertLess(deep["trust"], light["trust"])

        light_heal, _ = apply_scar(_record(scar_trust=-20), {"trust": 4}, k=2.0, k2=0.5)
        deep_heal, _ = apply_scar(_record(scar_trust=-80), {"trust": 4}, k=2.0, k2=0.5)
        self.assertLess(deep_heal["trust"], light_heal["trust"])

    def test_k_and_k2_are_the_bounds_at_full_scar(self):
        """守 k / k2 的语义就是"满痕时几倍"：信任权重 1.0，满痕时伤害必须正好 k 倍、
        修复正好 k2 倍。若按旧语义（1+k）实现，配置填 2 实际会打出 3 倍，
        而日志与界面只显示"配置 2"，光看数值看不出来。"""
        hurt, _ = apply_scar(_record(scar_trust=-100), {"trust": -10}, k=2.0, k2=0.5)
        self.assertEqual(hurt["trust"], -20)

        heal, _ = apply_scar(_record(scar_trust=-100), {"trust": 10}, k=2.0, k2=0.5)
        self.assertEqual(heal["trust"], 5)

    def test_repair_factor_never_goes_negative(self):
        """守修复因子不为负：k2 拉到 0（伤痕满时善意完全无效）也必须停在 0，
        不能翻成负数——那会让「安慰」反而扣分，而数值上只是一个小负数。"""
        adjusted, _ = apply_scar(_record(scar_trust=-100), {"trust": 10}, k=2.0, k2=0.0)
        self.assertEqual(adjusted["trust"], 0)

    def test_no_history_keeps_deltas_bit_for_bit(self):
        """守「无历史 = 因子恒为 1」：伤痕全 0 时两个方向的 delta 都必须逐位原样返回。
        这里一旦引入偏移，所有人升级后的手感立刻变化，而配置界面与日志都看不出异常。
        （伤痕增量仍会照常累积——那正是第一次伤害留下的痕迹。）"""
        for delta in (-10, -3, 1, 5):
            adjusted, _changes = apply_scar(
                _record(), {"trust": delta, "anger": -delta}, k=2.0, k2=0.5
            )
            self.assertEqual(adjusted, {"trust": delta, "anger": -delta})

    def test_scar_caps_at_both_ends_without_overflow(self):
        """守伤痕值域：向下封顶 -100，修复最多还清到 0、不会溢出成正数（没有"情分"侧）。
        两头越界都会让后续因子偏到设计之外，而库里只是一个不起眼的整数。"""
        _, bottom = apply_scar(_record(scar_trust=-98), {"trust": -10}, k=4.0, k2=0.5)
        self.assertEqual(-98 + bottom["trust"], -100)

        _, paid_off = apply_scar(_record(scar_trust=-1), {"trust": 5}, k=2.0, k2=0.5)
        self.assertEqual(-1 + paid_off["trust"], 0)


class ScarDecayTest(unittest.TestCase):
    def test_scar_decays_and_converges_to_zero(self):
        """守低位残留必然归零：截断方式若让小值停在原地，伤痕会永久留在库里、
        注入面板里一直挂着，而界面上只是几个看起来正常的小数字。"""
        value = -60
        for _ in range(600):
            record = _record(scar_trust=value)
            record.updated_at = utc_now() - timedelta(hours=1)
            changes, _ = apply_scar_decay(record, min_hours=1.0)
            value += changes.get("trust", 0)
        self.assertEqual(value, 0)


class ScarHintTest(unittest.TestCase):
    def test_hint_lists_only_dimensions_over_threshold(self):
        """守注入痕迹的门槛与措辞：未达阈值的维度不得出现（否则模型会为一点小事道歉），
        且必须声明是历史残留——少了这句，模型会把旧伤痕当成当前情绪来演。"""
        self.assertEqual(build_scar_hint(_record()), "")

        hint = build_scar_hint(_record(scar_trust=-65, scar_joy=-31, scar_anger=-20))
        self.assertIn("信任严重受损", hint)
        self.assertIn("喜悦受损", hint)
        self.assertNotIn("愤怒", hint)
        self.assertIn("历史残留", hint)

    def test_neutral_dimension_never_shows_a_hint(self):
        """不参与的维度即使伤痕被写得再深，也不该出现在注入里。"""
        self.assertEqual(build_scar_hint(_record(scar_surprise=-90)), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
