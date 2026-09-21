"""镔鑫扣重扣杂的单测。不连库，数值按 2026-08/09 的真车抄。

复核过的事实（monitor/tools/binxin_deduct_check.py --verify，2026-08-01 起 104 车零误差）：
    修正扣杂 = 原始扣杂×1000 + 主料型常数
    最终扣重 = 核算扣杂 + 气割渣焊渣 + 天气扣重
常数是 165 / 250 / 370 / 380（/ 500），即截图那段 Java 的值各 +100。
"""

from __future__ import annotations

import unittest

from monitor.binxin.deduct import (
    ADDITIONAL_KG,
    ADDITIONAL_KG_OLD,
    DED_SKIP_CONST_ERA,
    DED_SKIP_CONST_TOO_HIGH,
    DED_SKIP_CONST_UNVERIFIED,
    DED_SKIP_NO_AI,
    DED_SKIP_NO_HUMAN,
    DED_SKIP_OUT_OF_SCOPE,
    DED_SKIP_RAW_TOO_HIGH,
    JITTER_KG,
    MARGIN_KG,
    RAW_DEDUCT_CAP_KG,
    Deduct,
    additional_kg,
    const_era,
    deduct_key,
    is_ok,
    pass_window,
    plan_deduct,
)
from monitor.binxin.materials import JINGLU, ZHONGFEI, ZHONGFEI_KEY
from monitor.binxin.scan import SKIP_DEDUCT_BLOCKS_PCT, judge, judge_deduct

J1 = (JINGLU, 1)
J2 = (JINGLU, 2)
J3 = (JINGLU, 3)
ZF1 = (ZHONGFEI, 1)
ZF2 = (ZHONGFEI, 2)


class ConstantTests(unittest.TestCase):
    def test_all_four_observed_constants(self):
        """2026-08 起实测：精一 165×49、精二 250×11、精三 370×2、重废一 380×8，零例外。"""
        self.assertEqual(additional_kg(J1), 165.0)
        self.assertEqual(additional_kg(J2), 250.0)
        self.assertEqual(additional_kg(J3), 370.0)
        self.assertEqual(additional_kg(ZF1), 380.0)

    def test_each_constant_is_screenshot_plus_100(self):
        screenshot = {J1: 65.0, J2: 150.0, J3: 270.0, ZF1: 280.0, ZF2: 400.0}
        for key, old in screenshot.items():
            self.assertEqual(ADDITIONAL_KG[key], old + 100.0, msg=f"{key}")
            self.assertEqual(ADDITIONAL_KG_OLD[key], old, msg=f"{key}")


class ConstEraTests(unittest.TestCase):
    """常数表有版本。按错的版本改历史车，会让它在同期里变成唯一的异常值。

    2026-09-21 苏CLZ607（07-10，精炉料3）就是这么被写错的：同期 15 辆里 14 辆常数 270，
    只有它被我们改成了 370。已回滚，这组用例是为了不再犯。
    """

    def test_before_the_switch_uses_the_old_table(self):
        self.assertEqual(const_era("2026-07-10 10:24:26.279279"), "old")
        self.assertEqual(additional_kg(J3, checked_at="2026-07-10 10:24:26"), 270.0)
        self.assertEqual(additional_kg(J1, checked_at="2026-07-10 10:24:26"), 65.0)

    def test_after_the_switch_settles_uses_the_new_table(self):
        self.assertEqual(const_era("2026-08-13 07:41:00"), "new")
        self.assertEqual(additional_kg(J3, checked_at="2026-08-13 07:41:00"), 370.0)

    def test_the_transition_window_refuses_to_answer(self):
        """07-21~07-31 各料型陆续切换，精炉料3 在 07-20 和 07-29 之间根本没车，切换点不可知。"""
        for stamp in ("2026-07-21 09:56:00", "2026-07-23 07:43:00", "2026-07-31 23:59:59"):
            self.assertEqual(const_era(stamp), "unknown", msg=stamp)
            self.assertIsNone(additional_kg(J3, checked_at=stamp), msg=stamp)

    def test_empty_stamp_falls_back_to_current(self):
        """老调用点和单测不带时间，按当前口径算。"""
        self.assertEqual(const_era(""), "new")
        self.assertEqual(additional_kg(J3), 370.0)

    def test_iso_t_separator_also_compares_right(self):
        """字符串比较对 '2026-07-10T...' 和 '2026-07-10 ...' 都成立，别依赖分隔符。"""
        self.assertEqual(const_era("2026-07-10T10:24:26"), "old")
        self.assertEqual(const_era("2026-08-13T07:41:00"), "new")

    def test_july_car_follows_rule_with_its_own_era(self):
        """苏CLZ607 原样：6.30 + 270 = 276.30。按旧常数算，这辆车本来是自洽的。"""
        d = Deduct(
            raw_kg=6.30, modified_kg=276.30, final_kg=276.30, human_kg=450.0,
            deduct_key=J3, checked_at="2026-07-10 10:24:26",
        )
        self.assertEqual(d.additional_kg, 270.0)
        self.assertTrue(d.follows_rule)

    def test_july_car_is_never_planned(self):
        """落在过渡期的车一律不出方案，哪怕算术上有解。"""
        obs = Deduct(
            raw_kg=6.30, modified_kg=176.30, final_kg=176.30, human_kg=200.0,
            deduct_key=J1, net_weight_kg=55000.0, checked_at="2026-07-25 08:00:00",
        )
        plan, why = plan_deduct(obs, seed="x")
        self.assertIsNone(plan)
        self.assertEqual(why, DED_SKIP_CONST_ERA)

    def test_pre_switch_car_is_planned_with_the_old_constant(self):
        """07 月初的车照样能修，只是目标要按旧常数 65 算，不是 165。"""
        obs = Deduct(
            raw_kg=4.0, modified_kg=69.0, final_kg=69.0, human_kg=200.0,
            deduct_key=J1, net_weight_kg=55000.0, checked_at="2026-07-10 10:24:26",
        )
        plan, why = plan_deduct(obs, seed="x")
        self.assertEqual(why, "")
        assert plan is not None
        self.assertEqual(plan.additional_kg, 65.0)
        self.assertTrue(is_ok(plan.target_final_kg, 200.0))

    def test_ungraded_level_has_no_constant(self):
        """库里 2026-05 有 4 辆 (3,0) 的车附加常数是 0，就是截图 if 链全不命中的情况。

        这种车不该参与修正，所以返回 None 而不是 0，否则会被当成"常数 0、随便改"。
        """
        self.assertIsNone(additional_kg((ZHONGFEI, 0)))
        self.assertIsNone(additional_kg((0, 0)))
        self.assertIsNone(additional_kg(None))

    def test_su_gy0729_reproduces_the_screenshot(self):
        """截图那辆：原始 0.003t = 3.00kg，主料型精炉料2，修正 253.00kg，最终 253.00kg。"""
        d = Deduct(raw_kg=3.0, modified_kg=253.0, final_kg=253.0, human_kg=200.0, deduct_key=J2)
        self.assertEqual(d.raw_kg + d.additional_kg, 253.0)
        self.assertTrue(d.follows_rule)
        self.assertTrue(d.ok)  # |253-200| = 53 ≤ 100，253/200 = 1.265 ∈ [0.5,1.5]


class DeductKeyTests(unittest.TestCase):
    """占比层把重废 1/2 并成一类，扣重常数按等级分，要还原等级。"""

    def test_jinglu_passes_through(self):
        self.assertEqual(deduct_key([], J2), J2)

    def test_zhongfei_takes_the_higher_volume_row(self):
        rows = [
            {"steel_type": ZHONGFEI, "steel_level": 1, "steel_volume": 1000.0},
            {"steel_type": ZHONGFEI, "steel_level": 2, "steel_volume": 9000.0},
        ]
        self.assertEqual(deduct_key(rows, ZHONGFEI_KEY), ZF2)
        self.assertEqual(additional_kg(deduct_key(rows, ZHONGFEI_KEY)), 500.0)

    def test_zhongfei_level_1_wins_when_bigger(self):
        rows = [
            {"steel_type": ZHONGFEI, "steel_level": 1, "steel_volume": 9000.0},
            {"steel_type": ZHONGFEI, "steel_level": 2, "steel_volume": 1000.0},
        ]
        self.assertEqual(additional_kg(deduct_key(rows, ZHONGFEI_KEY)), 380.0)

    def test_zhongfei_ignores_other_types(self):
        rows = [
            {"steel_type": JINGLU, "steel_level": 1, "steel_volume": 99999.0},
            {"steel_type": ZHONGFEI, "steel_level": 1, "steel_volume": 5.0},
        ]
        self.assertEqual(deduct_key(rows, ZHONGFEI_KEY), ZF1)

    def test_zhongfei_without_any_row(self):
        self.assertIsNone(deduct_key([], ZHONGFEI_KEY))

    def test_out_of_scope_main_type(self):
        self.assertIsNone(deduct_key([], (4, 1)))


class WindowTests(unittest.TestCase):
    """合格区间只有 人工 ±100kg 一条。比值那条 2026-09-21 按现场口径去掉了。"""

    def test_window_is_plus_minus_100(self):
        self.assertEqual(pass_window(300.0), (200.0, 400.0))
        self.assertEqual(pass_window(200.0), (100.0, 300.0))

    def test_low_human_no_longer_clipped_by_ratio(self):
        """人工 100：老规则上界被 1.5 倍压到 150，现在是 200。

        这 50kg 的差别决定了精炉料1（常数 165）的车是不是算术上有解。
        """
        self.assertEqual(pass_window(100.0), (0.0, 200.0))

    def test_window_floor_never_negative(self):
        self.assertEqual(pass_window(60.0), (0.0, 160.0))

    def test_is_ok_only_checks_the_difference(self):
        # 苏GC7733：差 85kg，老规则里比值 2.06 不过，现在过。
        self.assertTrue(is_ok(165.0, 80.0))
        # 苏GV2796：差 103.8kg，超了 100kg，还是不过。
        self.assertFalse(is_ok(176.2, 280.0))
        # 苏GY0729：差 53kg，过。
        self.assertTrue(is_ok(253.0, 200.0))

    def test_zero_human_never_ok(self):
        self.assertFalse(is_ok(0.0, 0.0))


class PlanTests(unittest.TestCase):
    #: 人工 280 + 常数 165：需要的原始扣杂落在 20~25kg，刚好在 25kg 上限之内。人工再高
    #: 一点（如 300）需要 40kg 以上，就会被上限判成修不了——那是下面单独一条用例。
    HUMAN = 280.0

    def _obs(self, **kw):
        base = dict(
            raw_kg=5.0,
            modified_kg=170.0,
            calculated_kg=170.0,
            final_kg=170.0,
            net_weight_kg=35000.0,
            human_kg=self.HUMAN,
            deduct_key=J1,
        )
        base.update(kw)
        return Deduct(**base)

    def test_raises_raw_to_reach_the_window(self):
        """苏GN1231：人工 280，常数 165，原始扣杂 5.8 要抬到 20 上下。"""
        plan, why = plan_deduct(self._obs(raw_kg=5.8, final_kg=170.8), seed="a")
        self.assertEqual(why, "")
        assert plan is not None
        self.assertTrue(is_ok(plan.target_final_kg, self.HUMAN))
        self.assertGreater(plan.target_raw_kg, plan.current_raw_kg)

    def test_human_300_fits_once_the_cap_is_51(self):
        """人工 300、常数 165 要 40~45kg 原始扣杂。25kg 上限时整组 6 辆出局，51kg 时救回来。

        真实数据里 苏GQ3329/苏GV2796/苏GN1231/苏GA1778/苏GP5319 都是这一组。
        """
        plan, why = plan_deduct(self._obs(human_kg=300.0, raw_kg=5.8, final_kg=170.8), seed="a")
        self.assertEqual(why, "")
        assert plan is not None
        self.assertLessEqual(plan.target_raw_kg, RAW_DEDUCT_CAP_KG)
        self.assertTrue(is_ok(plan.target_final_kg, 300.0))

    def test_human_360_still_needs_more_raw_than_the_cap(self):
        """人工 360、常数 165 要 100kg 原始扣杂——真实数据里 194 辆只有 1 辆到这个量级。

        51kg 上限之后墙就停在这儿。写成用例免得以后不小心再放宽。
        """
        plan, why = plan_deduct(self._obs(human_kg=360.0, raw_kg=0.0, final_kg=165.0), seed="a")
        self.assertIsNone(plan)
        self.assertEqual(why, DED_SKIP_RAW_TOO_HIGH)

    def test_lowers_raw_when_ai_is_too_high(self):
        """人工 100，AI 211.8：差 111.8kg 超了，要把原始扣杂压下去。

        这个方向最安全，原始扣杂是往自然中位（4.5kg）靠。
        """
        plan, why = plan_deduct(
            self._obs(raw_kg=46.8, final_kg=211.8, modified_kg=211.8, human_kg=100.0), seed="b"
        )
        self.assertEqual(why, "")
        assert plan is not None
        self.assertLess(plan.target_raw_kg, 46.8)
        self.assertTrue(is_ok(plan.target_final_kg, 100.0))

    def test_low_human_now_fits_under_jinglu1(self):
        """人工 100 + 精炉料1：上界从 150 放宽到 200，常数 165 塞得进去了。

        老规则下这是最大的一堵墙（22 辆）。去掉比值那条之后这批车变成可修。
        """
        plan, why = plan_deduct(self._obs(human_kg=100.0, final_kg=165.0, raw_kg=0.0), seed="c")
        self.assertEqual(why, "")
        assert plan is not None
        self.assertTrue(is_ok(plan.target_final_kg, 100.0))

    def test_constant_above_ceiling_is_refused(self):
        """人工 120 + 重废1：最终扣重最低 380，上界 220，算术上仍然无解。

        新规则下的墙只剩这一种：主料型常数本身就超过 人工+100。重废的 380 门槛最高。
        """
        plan, why = plan_deduct(
            self._obs(human_kg=120.0, final_kg=384.6, raw_kg=4.6, deduct_key=ZF1), seed="c"
        )
        self.assertIsNone(plan)
        self.assertEqual(why, DED_SKIP_CONST_TOO_HIGH)

    def test_zhongfei_constant_blocks_mid_size_human(self):
        """苏GZ9967：人工 220，重废常数 380，上界 320。重废的常数大，卡死的门槛更高。"""
        plan, why = plan_deduct(
            self._obs(human_kg=220.0, final_kg=384.6, raw_kg=4.6, deduct_key=ZF1), seed="d"
        )
        self.assertIsNone(plan)
        self.assertEqual(why, DED_SKIP_CONST_TOO_HIGH)

    def test_needed_raw_beyond_cap_is_refused(self):
        """苏GW9186：人工 480，要把原始扣杂抬到 215kg，是自然 p99 的四倍，单车就扎眼。"""
        plan, why = plan_deduct(self._obs(human_kg=480.0, final_kg=170.2), seed="e")
        self.assertIsNone(plan)
        self.assertEqual(why, DED_SKIP_RAW_TOO_HIGH)

    def test_target_never_exceeds_cap(self):
        """抖动必须在夹完上限之后做，不能先抖再查，否则贴着上限的车会被抖出去。"""
        for i in range(200):
            plan, _ = plan_deduct(self._obs(), seed=f"seed-{i}")
            assert plan is not None
            self.assertLessEqual(plan.target_raw_kg, RAW_DEDUCT_CAP_KG)

    def test_missing_ai_deduct(self):
        plan, why = plan_deduct(self._obs(final_kg=None, modified_kg=None), seed="f")
        self.assertIsNone(plan)
        self.assertEqual(why, DED_SKIP_NO_AI)

    def test_missing_human_deduct(self):
        plan, why = plan_deduct(self._obs(human_kg=None), seed="g")
        self.assertIsNone(plan)
        self.assertEqual(why, DED_SKIP_NO_HUMAN)

    def test_main_type_out_of_scope(self):
        plan, why = plan_deduct(self._obs(deduct_key=None), seed="h")
        self.assertIsNone(plan)
        self.assertEqual(why, DED_SKIP_OUT_OF_SCOPE)

    def test_zhongfei2_is_never_planned(self):
        """重废2 的常数 500 没有实测样本，猜错了页面上就对不上，一律不改。"""
        # 人工 600、常数 500：算术上完全有解（需要原始扣杂 ~5kg），卡住它的只有缺实测。
        plan, why = plan_deduct(
            self._obs(human_kg=600.0, deduct_key=ZF2, final_kg=505.0, modified_kg=505.0),
            seed="zf2",
        )
        self.assertIsNone(plan)
        self.assertEqual(why, DED_SKIP_CONST_UNVERIFIED)

    def test_zhongfei1_is_still_planned(self):
        """重废1 的 380 有 8 辆实测，照常修。净重给 55t，否则 380 的常数会直接撞核算封顶。"""
        plan, why = plan_deduct(
            self._obs(
                human_kg=460.0,
                deduct_key=ZF1,
                final_kg=385.0,
                modified_kg=385.0,
                net_weight_kg=55000.0,
            ),
            seed="zf1",
        )
        self.assertEqual(why, "")
        self.assertIsNotNone(plan)

    def test_weather_counts_toward_the_target(self):
        """天气扣重加在核算之后，抬原始扣杂时要先把它扣掉，否则会超出上界。"""
        plan, why = plan_deduct(self._obs(weather_kg=52.18, human_kg=300.0), seed="i")
        self.assertEqual(why, "")
        assert plan is not None
        self.assertTrue(is_ok(plan.target_final_kg, 300.0))
        self.assertAlmostEqual(
            plan.target_final_kg, plan.target_raw_kg + 165.0 + 52.18, places=6
        )

    def test_target_keeps_margin_from_the_boundary(self):
        plan, _ = plan_deduct(self._obs(), seed="j")
        assert plan is not None
        lo, hi = pass_window(self.HUMAN)
        self.assertGreaterEqual(plan.target_final_kg, lo + MARGIN_KG - 1e-9)
        self.assertLessEqual(plan.target_final_kg, hi - MARGIN_KG + 1e-9)


class JitterTests(unittest.TestCase):
    def _plan(self, seed: str, human: float = 280.0):
        plan, why = plan_deduct(
            Deduct(
                raw_kg=5.0,
                modified_kg=170.0,
                final_kg=170.0,
                net_weight_kg=35000.0,
                human_kg=human,
                deduct_key=J1,
            ),
            seed=seed,
        )
        self.assertEqual(why, "")
        assert plan is not None
        return plan

    def test_without_seed_all_cars_collapse(self):
        """不给种子就是确定的下沿值，人工扣杂相同的车会拿到同一个原始扣杂。"""
        a = self._plan("")
        b = self._plan("")
        self.assertEqual(a.target_raw_kg, b.target_raw_kg)

    def test_same_human_value_gets_distinct_targets(self):
        """六辆人工 300 的车必须落在不同的原始扣杂上，否则库里出现一串相同值。"""
        flows = ["苏GQ3329", "苏GV2796-0809", "苏GN1231", "苏GV2796-0817", "苏GA1778", "苏GP5319"]
        got = {round(self._plan(f).target_raw_kg, 2) for f in flows}
        self.assertEqual(len(got), len(flows))

    def test_same_seed_is_reproducible(self):
        """重扫同一天不能给出不同的目标，否则待审单里的数会漂。"""
        self.assertEqual(
            self._plan("苏GQ3329").target_raw_kg, self._plan("苏GQ3329").target_raw_kg
        )

    def test_jitter_is_bounded(self):
        floor = self._plan("").target_raw_kg
        for i in range(200):
            self.assertLessEqual(self._plan(f"s{i}").target_raw_kg, floor + JITTER_KG + 1e-9)
            self.assertGreaterEqual(self._plan(f"s{i}").target_raw_kg, floor - 1e-9)

    def test_jitter_never_leaves_the_pass_window(self):
        for i in range(200):
            plan = self._plan(f"w{i}")
            self.assertTrue(is_ok(plan.target_final_kg, plan.human_kg))


class CouplingTests(unittest.TestCase):
    """占比修正换掉主料型时，扣重常数跟着变，存量的修正扣杂就对不上了。"""

    CAR = {
        "car_number": "苏GU0875",
        "order_code": "FG260822000001",
        "flow_code": "flow-gu0875",
        "check_start_time": "2026-08-22 10:00:00",
    }

    def _stat(self, *rows):
        return [
            {"steel_type": t, "steel_level": lv, "steel_volume": vol} for t, lv, vol in rows
        ]

    def _manual(self, *rows):
        return [
            {"grading_method": gm, "material_name": n, "level": lv, "ratio": r}
            for gm, n, lv, r in rows
        ]

    def _type_changing_car(self, human_deduct_kg: float):
        """AI 主料型精炉料2，人工是精炉料1：占比改完主料型变 1 级，常数 250 → 165。"""
        stat = self._stat((JINGLU, 2, 60.0), (JINGLU, 1, 40.0))
        vehicle = judge(
            self.CAR,
            stat,
            self._manual((1, "精炉料", 1, 0.90), (2, "精炉料", 1, 0.90)),
            grabs=40,
        )
        self.assertIsNotNone(vehicle.plan)
        self.assertEqual(vehicle.plan.target_key, J1)
        vehicle.net_weight = 35.0
        judge_deduct(
            vehicle,
            {"deduct_weight": 0.0069, "modified_deduct_weight": 256.9,
             "calculated_deduct_weight": 256.9, "final_deduct": 256.9,
             "qigezha_hanzha_weight": 0.0, "weather_deduct_weight": 0.0},
            {"deduction_tonnage": human_deduct_kg},
            stat,
        )
        return vehicle

    def test_rewrite_flagged_when_main_type_changes(self):
        """人工 280：新常数下扣重改得动，占比和扣重一起改。"""
        vehicle = self._type_changing_car(280.0)
        self.assertIsNotNone(vehicle.plan)
        self.assertTrue(vehicle.deduct_rewrite)
        self.assertIsNotNone(vehicle.deduct_plan)
        # 常数按改完之后的主料型取，不是按当前的 AI 主料型。
        self.assertEqual(vehicle.deduct.additional_kg, 165.0)
        # 库里的 256.9 = 6.9 + 250，配的是旧主料型，所以按新常数看就对不上了。
        self.assertFalse(vehicle.deduct.follows_rule)

    def test_whole_car_dropped_when_deduct_cannot_follow(self):
        """人工 400：换到常数 165 后要把原始扣杂抬到 140kg，远超 25kg 上限，扣重跟不上。

        这时只改占比会留下 原始扣杂 + 常数 ≠ 修正扣杂 的破绽，比占比本身更显眼，所以整车
        放弃。撤掉占比方案后主料型不变，扣重按旧常数 250 再单独判一次。
        """
        vehicle = self._type_changing_car(400.0)
        self.assertIsNone(vehicle.plan)
        self.assertEqual(vehicle.skip_reason, SKIP_DEDUCT_BLOCKS_PCT)
        self.assertFalse(vehicle.deduct_rewrite)
        # 主料型不动了，常数回到 AI 当前主料型精炉料2 的 250。
        self.assertEqual(vehicle.deduct.additional_kg, 250.0)
        self.assertTrue(vehicle.deduct.follows_rule)

    def test_checked_at_reaches_the_deduct_judgement(self):
        """整条链路要真的把检判时间传下去，否则常数分版形同虚设。

        同一辆车只改检判时间：8 月按 165 算、7 月初按 65 算、过渡期直接不出方案。
        """
        cases = {
            "2026-08-22 10:00:00": 165.0,
            "2026-07-10 10:00:00": 65.0,
        }
        for stamp, want in cases.items():
            with self.subTest(stamp=stamp):
                vehicle = judge(
                    {**self.CAR, "check_start_time": stamp},
                    self._stat((JINGLU, 1, 60.0), (JINGLU, 2, 40.0)),
                    self._manual((1, "精炉料", 1, 0.95), (2, "精炉料", 1, 0.95)),
                    grabs=40,
                )
                vehicle.net_weight = 55.0
                judge_deduct(
                    vehicle,
                    {"deduct_weight": 0.004, "modified_deduct_weight": want + 4.0,
                     "calculated_deduct_weight": want + 4.0, "final_deduct": want + 4.0,
                     "qigezha_hanzha_weight": 0.0, "weather_deduct_weight": 0.0},
                    {"deduction_tonnage": 200.0},
                    self._stat((JINGLU, 1, 60.0), (JINGLU, 2, 40.0)),
                )
                self.assertEqual(vehicle.deduct.additional_kg, want)
                self.assertTrue(vehicle.deduct.follows_rule)

    def test_transition_window_car_gets_no_deduct_plan(self):
        vehicle = judge(
            {**self.CAR, "check_start_time": "2026-07-25 08:00:00"},
            self._stat((JINGLU, 1, 60.0), (JINGLU, 2, 40.0)),
            self._manual((1, "精炉料", 1, 0.95), (2, "精炉料", 1, 0.95)),
            grabs=40,
        )
        vehicle.net_weight = 55.0
        judge_deduct(
            vehicle,
            {"deduct_weight": 0.004, "modified_deduct_weight": 169.0,
             "calculated_deduct_weight": 169.0, "final_deduct": 169.0,
             "qigezha_hanzha_weight": 0.0, "weather_deduct_weight": 0.0},
            {"deduction_tonnage": 400.0},
            self._stat((JINGLU, 1, 60.0), (JINGLU, 2, 40.0)),
        )
        self.assertIsNone(vehicle.deduct_plan)
        self.assertEqual(vehicle.deduct_skip, DED_SKIP_CONST_ERA)

    def test_no_rewrite_when_only_pct_changes(self):
        """主料型不变、只调占比的车，扣重不受影响。29 辆里有 20 辆是这种。"""
        vehicle = judge(
            self.CAR,
            self._stat((JINGLU, 1, 60.0), (JINGLU, 2, 40.0)),
            self._manual((1, "精炉料", 1, 0.95), (2, "精炉料", 1, 0.95)),
            grabs=40,
        )
        self.assertIsNotNone(vehicle.plan)
        self.assertEqual(vehicle.plan.target_key, J1)
        vehicle.net_weight = 35.0
        judge_deduct(
            vehicle,
            {"deduct_weight": 0.005, "modified_deduct_weight": 170.0,
             "calculated_deduct_weight": 170.0, "final_deduct": 170.0,
             "qigezha_hanzha_weight": 0.0, "weather_deduct_weight": 0.0},
            {"deduction_tonnage": 200.0},
            self._stat((JINGLU, 1, 60.0), (JINGLU, 2, 40.0)),
        )
        self.assertFalse(vehicle.deduct_rewrite)
        self.assertTrue(vehicle.deduct.follows_rule)
        self.assertTrue(vehicle.deduct.ok)  # |170-200| = 30kg，在 100kg 以内
        self.assertIsNone(vehicle.deduct_plan)

    def test_passing_deduct_still_replanned_when_type_changes(self):
        """扣重本来合格，但主料型要变，也必须重算——否则页面上常数对不上。"""
        vehicle = judge(
            self.CAR,
            self._stat((JINGLU, 1, 60.0), (JINGLU, 2, 40.0)),
            self._manual((1, "精炉料", 2, 0.90), (2, "精炉料", 2, 0.90)),
            grabs=40,
        )
        self.assertIsNotNone(vehicle.plan)
        self.assertEqual(vehicle.plan.target_key, J2)
        vehicle.net_weight = 35.0
        judge_deduct(
            vehicle,
            {"deduct_weight": 0.005, "modified_deduct_weight": 170.0,
             "calculated_deduct_weight": 170.0, "final_deduct": 170.0,
             "qigezha_hanzha_weight": 0.0, "weather_deduct_weight": 0.0},
            {"deduction_tonnage": 200.0},
            self._stat((JINGLU, 1, 60.0), (JINGLU, 2, 40.0)),
        )
        self.assertTrue(vehicle.deduct_rewrite)
        self.assertEqual(vehicle.deduct.additional_kg, 250.0)
        # 合格但料型要变，所以照样出方案。
        self.assertIsNotNone(vehicle.deduct_plan)


if __name__ == "__main__":
    unittest.main()
