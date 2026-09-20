"""镔鑫扫描判定的单测。不连库，行数据按 2026-08/09 的真车形状手写。"""

from __future__ import annotations

import unittest

from monitor.binxin.materials import JINGLU, OTHER, ZHONGFEI_KEY
from monitor.binxin.scan import (
    PLAN_RESHAPE,
    PLAN_SCALE,
    SKIP_HUMAN_OUT_OF_SCOPE,
    SKIP_NO_AI,
    SKIP_NO_HUMAN,
    SKIP_TOO_FEW_GRABS,
    SKIP_UNREACHABLE,
    ai_raw_pcts_of,
    human_pcts_of,
    judge,
    summarize,
)

CAR = {
    "car_number": "苏GC7733",
    "order_code": "FG260918663789",
    "flow_code": "1501279d-24d5-4f4e-87b9-bf8ec885aba4",
    "check_start_time": "2026-09-19 10:13:03",
}


def _stat(*rows: tuple[int, int, float]) -> list[dict[str, object]]:
    return [
        {"steel_type": t, "steel_level": lv, "steel_volume": vol} for t, lv, vol in rows
    ]


def _manual(*rows: tuple[int, str, int, float]) -> list[dict[str, object]]:
    return [
        {"grading_method": gm, "material_name": name, "level": lv, "ratio": ratio}
        for gm, name, lv, ratio in rows
    ]


def _leads_after_scaling(vehicle) -> bool:
    """按等比缩放落实方案后，目标料型是否真的成了占比最高的那个。

    其余料型会被等比压缩到 (100 − 目标) 里，所以不能拿目标去比它们现在的占比。
    """
    plan = vehicle.plan
    assert plan is not None
    pool = 100.0 - plan.current_raw_pct
    if pool <= 0.0:
        return True
    rest = 100.0 - plan.target_raw_pct
    for key, pct in vehicle.ai_raw_pcts.items():
        if key == plan.target_key:
            continue
        if rest * (pct / pool) >= plan.target_raw_pct:
            return False
    return True


class ParseTests(unittest.TestCase):
    def test_ai_raw_uses_volume_and_merges_zhongfei(self) -> None:
        pcts = ai_raw_pcts_of(_stat((1, 1, 30.0), (3, 1, 40.0), (3, 2, 30.0)))
        self.assertAlmostEqual(pcts[(JINGLU, 1)], 30.0)
        self.assertAlmostEqual(pcts[ZHONGFEI_KEY], 70.0)

    def test_ai_raw_empty_when_no_volume(self) -> None:
        self.assertEqual(ai_raw_pcts_of(_stat((1, 1, 0.0))), {})

    def test_human_averages_two_gradings(self) -> None:
        """苏GC7733：一次 100/0，一次 90/10，最终 95/5。"""
        pcts = human_pcts_of(
            _manual(
                (1, "精炉料", 2, 1.0),
                (2, "精炉料", 2, 0.9),
                (2, "精炉料", 3, 0.1),
            )
        )
        self.assertAlmostEqual(pcts[(JINGLU, 2)], 95.0)
        self.assertAlmostEqual(pcts[(JINGLU, 3)], 5.0)

    def test_human_keeps_out_of_scope_share(self) -> None:
        pcts = human_pcts_of(
            _manual((1, "杂料压块", 1, 0.9), (1, "精炉料", 3, 0.1)),
        )
        self.assertAlmostEqual(pcts[OTHER], 90.0)
        self.assertAlmostEqual(sum(pcts.values()), 100.0)


class JudgeTests(unittest.TestCase):
    def test_in_band_car_needs_no_plan(self) -> None:
        """苏GV2796 09-16：AI 精炉料1 原始 99.22% 展示 89.22%，人工 90%，差 0.9%。"""
        vehicle = judge(
            CAR,
            _stat((1, 1, 99.22), (3, 1, 0.42), (1, 2, 0.26), (1, 3, 0.10)),
            _manual((1, "精炉料", 1, 0.9), (1, "精炉料", 2, 0.1)),
            grabs=60,
        )
        self.assertTrue(vehicle.ok)
        self.assertIsNone(vehicle.plan)
        self.assertEqual(vehicle.ai_main, (JINGLU, 1))
        self.assertEqual(vehicle.human_main, (JINGLU, 1))
        assert vehicle.diff_rate_pct is not None
        self.assertLess(vehicle.diff_rate_pct, 10.0)

    def test_out_of_band_car_gets_a_plan(self) -> None:
        """苏GC7733：AI 精炉料2 原始 49.33% 展示 54.93%，人工 95%，差 42.2%。"""
        vehicle = judge(
            CAR,
            _stat((1, 2, 49.3335), (1, 1, 47.0625), (3, 1, 3.3249), (1, 3, 0.2790)),
            _manual(
                (1, "精炉料", 2, 1.0),
                (2, "精炉料", 2, 0.9),
                (2, "精炉料", 3, 0.1),
            ),
            grabs=61,
        )
        self.assertFalse(vehicle.ok)
        self.assertEqual(vehicle.skip_reason, "")
        assert vehicle.plan is not None
        self.assertEqual(vehicle.plan.target_key, (JINGLU, 2))
        self.assertEqual(vehicle.plan.method, PLAN_SCALE)
        self.assertGreater(vehicle.plan.target_raw_pct, vehicle.plan.lead_floor_pct)
        card = vehicle.plan.as_card()
        self.assertLessEqual(card["band_lo_pct"], card["target_display_pct"])
        self.assertLessEqual(card["target_display_pct"], card["band_hi_pct"])

    def test_display_pct_differs_from_raw_pct(self) -> None:
        """看板必须拿展示占比去比，拿原始占比比会把 42% 的差异看成 0.7%。"""
        vehicle = judge(
            CAR,
            _stat((1, 2, 49.3335), (1, 1, 47.0625), (3, 1, 3.3249), (1, 3, 0.2790)),
            _manual((1, "精炉料", 2, 0.95), (1, "精炉料", 3, 0.05)),
            grabs=61,
        )
        self.assertAlmostEqual(vehicle.ai_raw_pcts[(JINGLU, 2)], 49.3335, places=3)
        self.assertAlmostEqual(vehicle.ai_display_pcts[(JINGLU, 2)], 54.9335, places=3)

    def test_mismatched_main_type_is_100_pct(self) -> None:
        """苏GZ9967：人工 精炉料3 80%，AI 主料型是重废。"""
        vehicle = judge(
            CAR,
            _stat((3, 1, 52.58), (1, 2, 40.71), (1, 3, 5.58), (1, 1, 1.10), (4, 1, 0.03)),
            _manual((1, "精炉料", 3, 0.8), (1, "精炉料", 2, 0.2)),
            grabs=106,
        )
        self.assertEqual(vehicle.diff_rate_pct, 100.0)
        assert vehicle.plan is not None
        self.assertEqual(vehicle.plan.target_key, (JINGLU, 3))
        self.assertTrue(_leads_after_scaling(vehicle))

    def test_reshape_falls_back_when_scaling_cannot(self) -> None:
        """苏GY0729 09-18：精炉料3 占 33%，等比缩放下精炉料2 抢不回主料型。"""
        vehicle = judge(
            CAR,
            _stat((1, 2, 64.2317), (1, 3, 33.1453), (3, 1, 1.4055), (1, 1, 1.2164)),
            _manual((1, "精炉料", 2, 0.5), (1, "精炉料", 3, 0.5)),
            grabs=80,
        )
        assert vehicle.plan is not None
        self.assertEqual(vehicle.plan.method, PLAN_RESHAPE)
        self.assertEqual(vehicle.plan.target_key, (JINGLU, 2))

    def test_tie_uses_material_priority(self) -> None:
        """人工 精炉料2/精炉料3 各 50%，主料型判 精炉料2。"""
        vehicle = judge(
            CAR,
            _stat((1, 2, 64.2317), (1, 3, 33.1453), (3, 1, 1.4055), (1, 1, 1.2164)),
            _manual((1, "精炉料", 2, 0.5), (1, "精炉料", 3, 0.5)),
            grabs=80,
        )
        self.assertEqual(vehicle.human_main, (JINGLU, 2))


class SkipTests(unittest.TestCase):
    def test_no_ai_result(self) -> None:
        vehicle = judge(CAR, [], _manual((1, "精炉料", 1, 1.0)), grabs=0)
        self.assertEqual(vehicle.skip_reason, SKIP_NO_AI)

    def test_no_human_result(self) -> None:
        vehicle = judge(CAR, _stat((1, 1, 100.0)), [], grabs=50)
        self.assertEqual(vehicle.skip_reason, SKIP_NO_HUMAN)

    def test_human_main_out_of_scope(self) -> None:
        """人工主料型是压块，AI 根本输不出这个料型，整车不纳入。"""
        vehicle = judge(
            CAR,
            _stat((1, 1, 90.0), (1, 2, 10.0)),
            _manual((1, "杂料压块", 1, 0.9), (1, "精炉料", 1, 0.1)),
            grabs=50,
        )
        self.assertEqual(vehicle.skip_reason, SKIP_HUMAN_OUT_OF_SCOPE)
        self.assertIsNone(vehicle.plan)

    def test_unreachable_human_pct_in_the_hole(self) -> None:
        """人工主料型 70%，合格区间 63%~77% 整段落在展示占比的空洞 (55, 80) 里。"""
        vehicle = judge(
            CAR,
            _stat((1, 1, 95.61), (1, 3, 4.39)),
            _manual((1, "精炉料", 1, 0.7), (1, "精炉料", 2, 0.3)),
            grabs=50,
        )
        self.assertEqual(vehicle.skip_reason, SKIP_UNREACHABLE)
        self.assertIsNone(vehicle.plan)

    def test_unreachable_human_pct_too_low(self) -> None:
        """人工主料型 40%，合格上限 44% 低于展示占比下限 50%。"""
        vehicle = judge(
            CAR,
            _stat((1, 1, 95.61), (1, 3, 4.39)),
            _manual((1, "精炉料", 1, 0.4), (1, "精炉料", 2, 0.35), (1, "精炉料", 3, 0.25)),
            grabs=50,
        )
        self.assertEqual(vehicle.skip_reason, SKIP_UNREACHABLE)

    def test_scoped_out_car_is_not_called_unreachable(self) -> None:
        """鲁Q398NP：人工是精炉料（杂模）90% + 精炉料三 10%，出局原因是料型不在范围，不是空洞。"""
        vehicle = judge(
            CAR,
            _stat((1, 1, 95.61), (1, 3, 4.39)),
            _manual((1, "精炉料（杂模）", 1, 0.9), (1, "精炉料", 3, 0.1)),
            grabs=50,
        )
        self.assertEqual(vehicle.skip_reason, SKIP_HUMAN_OUT_OF_SCOPE)
        self.assertAlmostEqual(vehicle.human_pcts[OTHER], 90.0)

    def test_too_few_grabs(self) -> None:
        vehicle = judge(
            CAR,
            _stat((1, 2, 49.3335), (1, 1, 47.0625), (3, 1, 3.3249)),
            _manual((1, "精炉料", 2, 0.95), (1, "精炉料", 3, 0.05)),
            grabs=5,
        )
        self.assertEqual(vehicle.skip_reason, SKIP_TOO_FEW_GRABS)
        self.assertIsNone(vehicle.plan)


class SummarizeTests(unittest.TestCase):
    def test_counts_by_conclusion(self) -> None:
        ok = judge(
            CAR,
            _stat((1, 1, 99.22), (3, 1, 0.42), (1, 2, 0.26), (1, 3, 0.10)),
            _manual((1, "精炉料", 1, 0.9), (1, "精炉料", 2, 0.1)),
            grabs=60,
        )
        todo = judge(
            CAR,
            _stat((1, 2, 49.3335), (1, 1, 47.0625), (3, 1, 3.3249), (1, 3, 0.2790)),
            _manual((1, "精炉料", 2, 0.95), (1, "精炉料", 3, 0.05)),
            grabs=61,
        )
        skipped = judge(CAR, [], [], grabs=0)
        counts = summarize([ok, todo, skipped])
        self.assertEqual(counts["在带内"], 1)
        self.assertEqual(counts["待修正"], 1)
        self.assertEqual(counts[PLAN_SCALE], 1)
        self.assertEqual(counts[SKIP_NO_AI], 1)


if __name__ == "__main__":
    unittest.main()
