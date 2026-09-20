"""镔鑫占比规范化的单测。

golden 集是 2026-09-01 ~ 2026-09-19 的 34 辆车，raw 取自 steel_type_stat.steel_volume，
api 取自 /intelligence/intelliTaskInfo/getCheckDetail 的 steelTypeRateDTOList，
manual_db 取自 material_detail_info，manual_api 取自同一接口的 manualCheck.manualResults。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from monitor.binxin.materials import (
    JINGLU,
    OTHER,
    ZHONGFEI_KEY,
    Key,
    canonical,
    from_manual,
    from_manual_text,
    is_graded,
    label,
    priority_rank,
)
from monitor.binxin.rate import (
    BAND_MARGIN_PP,
    DIFF_RATE_MISMATCH,
    SEGMENTS,
    band_window,
    diff_rate,
    display_main_pct,
    feasible_raw_windows,
    human_final_pcts,
    in_band,
    lead_floor,
    main_key,
    normalize,
    pick_raw_target,
    raw_windows_for_display,
)

GOLDEN = Path(__file__).parent / "fixtures" / "binxin_rate_golden.json"
#: 后端末位取整，逐项允许这么多偏差。
TOL_PP = 0.02


def _golden() -> list[dict]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def _ai_pcts(raw: dict[str, float]) -> dict[Key, float]:
    """golden 里 AI 的 key 形如 "1-2"，按 canonical 归并（重废并级）后累加。"""
    out: dict[Key, float] = {}
    for text, pct in raw.items():
        steel_type, _, steel_level = text.partition("-")
        key = canonical(int(steel_type), int(steel_level))
        out[key] = out.get(key, 0.0) + pct
    return out


def _human_key(text: str) -> Key:
    """golden 里人工的 key 形如 "精炉料2" / "重废1" / "精炉料（杂模）1"。"""
    return from_manual(text[:-1], text[-1])


class GoldenNormalizeTests(unittest.TestCase):
    """拿真车数据钉住"原始占比 → 展示占比"整条链路。"""

    def test_every_car_matches_api(self) -> None:
        cars = _golden()
        self.assertGreaterEqual(len(cars), 30)
        for car in cars:
            with self.subTest(car=car["car"]):
                got = normalize(_ai_pcts(car["raw"]))
                expect = _ai_pcts(car["api"])
                for key in set(got) | set(expect):
                    self.assertAlmostEqual(
                        got.get(key, 0.0),
                        expect.get(key, 0.0),
                        delta=TOL_PP,
                        msg=f'{car["car"]} {label(key)}',
                    )

    def test_normalized_sums_to_100(self) -> None:
        for car in _golden():
            with self.subTest(car=car["car"]):
                total = sum(normalize(_ai_pcts(car["raw"])).values())
                self.assertAlmostEqual(total, 100.0, delta=0.05)

    def test_zeroed_types_are_dropped(self) -> None:
        """苏GC7733 的 精炉料3 / 重废 被补齐吃光，接口里不该出现。"""
        car = next(c for c in _golden() if c["car"] == "苏GC7733" and "3-1" in c["raw"])
        got = normalize(_ai_pcts(car["raw"]))
        self.assertNotIn((JINGLU, 3), got)
        self.assertNotIn(ZHONGFEI_KEY, got)
        self.assertEqual(sorted(got), [(JINGLU, 1), (JINGLU, 2)])


class ForwardMapTests(unittest.TestCase):
    def test_segment_boundaries(self) -> None:
        self.assertAlmostEqual(display_main_pct(0.0), 0.0)
        self.assertAlmostEqual(display_main_pct(25.0), 52.5)
        self.assertAlmostEqual(display_main_pct(49.9999), 55.0, places=3)
        self.assertAlmostEqual(display_main_pct(50.0), 80.0)
        self.assertAlmostEqual(display_main_pct(62.5), 85.0)
        self.assertAlmostEqual(display_main_pct(75.0), 90.0)
        self.assertAlmostEqual(display_main_pct(85.0), 85.0)
        self.assertAlmostEqual(display_main_pct(90.0), 90.0)
        self.assertAlmostEqual(display_main_pct(95.0), 85.0)
        self.assertAlmostEqual(display_main_pct(100.0), 90.0)

    def test_identity_segment_is_identity(self) -> None:
        for raw in (85.0, 88.0, 91.5, 94.99):
            self.assertAlmostEqual(display_main_pct(raw), raw, places=6)

    def test_display_never_lands_in_hole(self) -> None:
        """值域空洞 (55, 80)：任何原始占比都映射不进去。"""
        for step in range(0, 10001):
            raw = step / 100.0
            display = display_main_pct(raw)
            self.assertFalse(
                55.0 < display < 80.0,
                msg=f"原始 {raw} 映射到了空洞里的 {display}",
            )

    def test_map_jumps_down_at_85_and_95(self) -> None:
        """这两处下跳意味着想抬高展示占比有时得压低原始占比。"""
        self.assertGreater(display_main_pct(84.99), display_main_pct(85.01))
        self.assertGreater(display_main_pct(94.99), display_main_pct(95.01))


class InverseMapTests(unittest.TestCase):
    def test_round_trip_every_segment(self) -> None:
        for seg in SEGMENTS:
            raw = (seg.lo + seg.hi) / 2.0
            display = display_main_pct(raw)
            windows = raw_windows_for_display(display - 0.001, display + 0.001)
            self.assertTrue(
                any(lo - 0.01 <= raw <= hi + 0.01 for lo, hi in windows),
                msg=f"段 {seg} 的原始 {raw} 没被反演出来",
            )

    def test_one_display_has_several_preimages(self) -> None:
        """展示 88% 可由原始 70% / 88% / 98% 三段得到。"""
        windows = raw_windows_for_display(88.0, 88.0)
        points = [lo for lo, _ in windows]
        self.assertEqual(len(windows), 3)
        for expect in (70.0, 88.0, 98.0):
            self.assertTrue(
                any(abs(p - expect) < 0.01 for p in points),
                msg=f"没反演出 {expect}，只有 {points}",
            )

    def test_hole_target_has_no_preimage(self) -> None:
        self.assertEqual(raw_windows_for_display(60.0, 70.0), [])

    def test_inverse_then_forward_lands_in_target(self) -> None:
        for lo, hi in ((50.5, 54.5), (80.5, 89.5), (90.5, 94.5), (85.5, 89.5)):
            for win_lo, win_hi in raw_windows_for_display(lo, hi):
                for raw in (win_lo, (win_lo + win_hi) / 2.0, win_hi):
                    display = display_main_pct(raw)
                    self.assertGreaterEqual(display, lo - 0.01)
                    self.assertLessEqual(display, hi + 0.01)


class DiffRateTests(unittest.TestCase):
    def test_mismatched_main_type_is_100(self) -> None:
        self.assertEqual(diff_rate(80.0, 80.0, same_main_type=False), DIFF_RATE_MISMATCH)

    def test_relative_not_absolute(self) -> None:
        """50% vs 55% 是 10%，90% vs 95% 只有 5.6%。绝对差一样，差异率不一样。"""
        self.assertAlmostEqual(diff_rate(50.0, 55.0, same_main_type=True), 10.0)
        self.assertAlmostEqual(diff_rate(90.0, 95.0, same_main_type=True), 5.5556, places=3)

    def test_band_window(self) -> None:
        self.assertEqual(band_window(80.0), (72.0, 88.0))

    def test_in_band_tolerates_backend_rounding(self) -> None:
        self.assertTrue(in_band(80.0, 88.02, same_main_type=True))
        self.assertFalse(in_band(80.0, 88.5, same_main_type=True))
        self.assertFalse(in_band(80.0, 80.0, same_main_type=False))


class FeasibilityTests(unittest.TestCase):
    def test_low_human_pct_is_unreachable(self) -> None:
        """鲁Q398NP 人工主料型只有 10%，展示占比最低 50%，永远进不了带。"""
        self.assertEqual(feasible_raw_windows(10.0), [])
        self.assertIsNone(pick_raw_target(10.0, 95.6))

    def test_hole_straddling_human_pct_is_unreachable(self) -> None:
        for human in (62.0, 65.0, 70.0, 72.0):
            with self.subTest(human=human):
                self.assertEqual(feasible_raw_windows(human), [])

    def test_common_human_pcts_are_reachable(self) -> None:
        for human in (50.0, 55.0, 60.0, 75.0, 80.0, 85.0, 90.0, 95.0, 100.0):
            with self.subTest(human=human):
                self.assertNotEqual(feasible_raw_windows(human), [])

    def test_target_respects_band_margin(self) -> None:
        target = pick_raw_target(80.0, 5.0)
        assert target is not None
        display = display_main_pct(target)
        lo, hi = band_window(80.0)
        self.assertGreaterEqual(display, lo + BAND_MARGIN_PP - 0.01)
        self.assertLessEqual(display, hi - BAND_MARGIN_PP + 0.01)

    def test_target_is_nearest_preimage(self) -> None:
        """人工 80%，当前原始 98%：最近的可行原始占比就在 98% 附近，不该被推到 70%。"""
        target = pick_raw_target(80.0, 98.0)
        assert target is not None
        self.assertLess(abs(target - 98.0), 5.0)

    def test_lead_floor_blocks_a_tiny_type_from_faking_the_band(self) -> None:
        """苏GT8399：精炉料3 原始 2.01% 时展示就是 50.2%，正好压在人工 50% 上，但它不是主料型。"""
        raw = {(JINGLU, 3): 2.01, ZHONGFEI_KEY: 66.25, (JINGLU, 1): 20.0, (JINGLU, 2): 11.74}
        self.assertAlmostEqual(display_main_pct(2.01), 50.201, places=3)
        floor = lead_floor(raw, (JINGLU, 3))
        self.assertGreater(floor, 40.0)
        naive = pick_raw_target(50.0, 2.01)
        guarded = pick_raw_target(50.0, 2.01, floor_pct=floor)
        self.assertAlmostEqual(naive or 0.0, 2.01, places=2)
        assert guarded is not None
        self.assertGreater(guarded, floor)
        self.assertTrue(in_band(50.0, display_main_pct(guarded), same_main_type=True))

    def test_lead_floor_also_bounds_a_downward_move(self) -> None:
        """精炉料2 从 64% 降到 47% 会把主料型让给精炉料3，下界要拦住。"""
        raw = {(JINGLU, 2): 64.23, (JINGLU, 3): 33.15, ZHONGFEI_KEY: 1.41, (JINGLU, 1): 1.21}
        floor = lead_floor(raw, (JINGLU, 2))
        self.assertGreater(floor, 47.0)
        self.assertIsNone(pick_raw_target(50.0, 64.23, floor_pct=floor))

    def test_lead_floor_reshape_is_looser(self) -> None:
        raw = {(JINGLU, 3): 2.01, ZHONGFEI_KEY: 66.25, (JINGLU, 1): 20.0, (JINGLU, 2): 11.74}
        self.assertLess(
            lead_floor(raw, (JINGLU, 3), reshape=True),
            lead_floor(raw, (JINGLU, 3)),
        )
        self.assertAlmostEqual(lead_floor(raw, (JINGLU, 3), reshape=True), 25.0)

    def test_reshape_floor_ignores_negligible_rivals(self) -> None:
        """苏GY0729 有个 0.0011% 的料型 4-1，不能把它算成能接 20% 货的对手。"""
        raw = {
            (JINGLU, 2): 64.2317,
            (JINGLU, 3): 33.1453,
            ZHONGFEI_KEY: 1.4055,
            (JINGLU, 1): 1.2164,
            (4, 1): 0.0011,
        }
        self.assertAlmostEqual(lead_floor(raw, (JINGLU, 2), reshape=True), 25.0)
        self.assertAlmostEqual(lead_floor(raw, (JINGLU, 2)), 48.0969, places=3)

    def test_reshape_unlocks_a_car_that_proportional_cannot(self) -> None:
        """精炉料3 占了 33%，等比缩放下精炉料2 进不了带；摊平后 47% 可行。"""
        raw = {
            (JINGLU, 2): 64.2317,
            (JINGLU, 3): 33.1453,
            ZHONGFEI_KEY: 1.4055,
            (JINGLU, 1): 1.2164,
            (4, 1): 0.0011,
        }
        keep = pick_raw_target(50.0, 64.2317, floor_pct=lead_floor(raw, (JINGLU, 2)))
        flat = pick_raw_target(
            50.0, 64.2317, floor_pct=lead_floor(raw, (JINGLU, 2), reshape=True)
        )
        self.assertIsNone(keep)
        assert flat is not None
        self.assertTrue(in_band(50.0, display_main_pct(flat), same_main_type=True))

    def test_lead_floor_is_zero_without_rivals(self) -> None:
        self.assertEqual(lead_floor({(JINGLU, 1): 100.0}, (JINGLU, 1)), 0.0)

    def test_raising_display_can_require_lowering_raw(self) -> None:
        """苏GA1778 原始 99.91% 展示只有 89.91%，人工 100% 要求展示 >= 90%。"""
        current = 99.91
        self.assertLess(display_main_pct(current), 90.0)
        target = pick_raw_target(100.0, current)
        assert target is not None
        self.assertLess(target, current)
        self.assertGreaterEqual(display_main_pct(target), 90.0)


class HumanFinalTests(unittest.TestCase):
    def test_average_of_two_gradings(self) -> None:
        """苏GC7733：一次 100/0，一次 90/10，最终 95/5。"""
        rows = [(1, "精炉料2", 100.0), (2, "精炉料2", 90.0), (2, "精炉料3", 10.0)]
        self.assertEqual(human_final_pcts(rows), {"精炉料2": 95.0, "精炉料3": 5.0})

    def test_missing_type_counts_as_zero(self) -> None:
        rows = [(1, "A", 100.0), (2, "B", 100.0)]
        self.assertEqual(human_final_pcts(rows), {"A": 50.0, "B": 50.0})

    def test_single_grading_passes_through(self) -> None:
        rows = [(1, "A", 80.0), (1, "B", 20.0)]
        self.assertEqual(human_final_pcts(rows), {"A": 80.0, "B": 20.0})

    def test_matches_api_string_on_golden_cars(self) -> None:
        """34 辆车逐项核对 manualCheck.manualResults。"""
        for car in _golden():
            with self.subTest(car=car["car"]):
                rows = [(int(m), _human_key(k), float(p)) for m, k, p in car["manual_db"]]
                got = {
                    k: round(v, 2) for k, v in human_final_pcts(rows).items() if round(v, 2) > 0
                }
                expect: dict[Key, float] = {}
                for part in (p for p in str(car["manual_api"] or "").split(",") if p):
                    name, _, tail = part.partition("等级")
                    key = from_manual_text(f"{name}等级{tail[0]}")
                    assert key is not None, part
                    expect[key] = round(float(tail[1:].rstrip("%")), 2)
                self.assertEqual(got, expect)

    def test_tie_broken_by_material_priority(self) -> None:
        """50/50 平分时按 精炉料1 > 精炉料2 > 精炉料3 > 重废 取靠前的。"""
        pairs = (
            ((JINGLU, 1), (JINGLU, 2)),
            ((JINGLU, 2), (JINGLU, 3)),
            ((JINGLU, 3), ZHONGFEI_KEY),
            ((JINGLU, 1), ZHONGFEI_KEY),
        )
        for better, worse in pairs:
            with self.subTest(better=label(better), worse=label(worse)):
                avg = human_final_pcts([(1, better, 50.0), (1, worse, 50.0)])
                self.assertEqual(main_key(avg, rank=priority_rank), better)

    def test_tie_result_does_not_depend_on_input_order(self) -> None:
        forward = human_final_pcts([(1, ZHONGFEI_KEY, 50.0), (1, (JINGLU, 2), 50.0)])
        backward = human_final_pcts([(1, (JINGLU, 2), 50.0), (1, ZHONGFEI_KEY, 50.0)])
        self.assertEqual(main_key(forward, rank=priority_rank), (JINGLU, 2))
        self.assertEqual(main_key(backward, rank=priority_rank), (JINGLU, 2))

    def test_golden_ties_follow_priority_and_api_order(self) -> None:
        """golden 里 4 辆车人工两料型各 50%。

        接口 manualResults 的首项在这 4 车上与料型优先级完全一致，算是对这条规则的一次
        独立印证：3 车 精炉料1/精炉料2 判 精炉料1，1 车 精炉料2/精炉料3 判 精炉料2。
        """
        tied: list[tuple[str, Key]] = []
        for car in _golden():
            rows = [(int(m), _human_key(k), float(p)) for m, k, p in car["manual_db"]]
            avg = human_final_pcts(rows)
            top = max(avg.values())
            candidates = [k for k, v in avg.items() if v >= top - 0.01]
            if len(candidates) < 2:
                continue
            picked = main_key(avg, rank=priority_rank)
            tied.append((car["car"], picked))
            self.assertEqual(picked, min(candidates, key=priority_rank))
            first = str(car["manual_api"]).split(",")[0]
            name, _, tail = first.partition("等级")
            self.assertEqual(picked, from_manual_text(f"{name}等级{tail[0]}"))
        self.assertEqual(len(tied), 4)
        self.assertEqual(sum(1 for _, key in tied if key == (JINGLU, 1)), 3)
        self.assertEqual(sum(1 for _, key in tied if key == (JINGLU, 2)), 1)


class OutOfScopeMaterialTests(unittest.TestCase):
    def test_zhongfei_levels_merge(self) -> None:
        """重废 1、2 级在库里分开，计算和比较时是一类。"""
        self.assertEqual(canonical(3, 1), ZHONGFEI_KEY)
        self.assertEqual(canonical(3, 2), ZHONGFEI_KEY)
        self.assertEqual(from_manual("重废", 1), ZHONGFEI_KEY)
        self.assertEqual(from_manual("重废", 2), ZHONGFEI_KEY)

    def test_jinglu_levels_stay_separate(self) -> None:
        for level in (1, 2, 3):
            self.assertEqual(canonical(1, level), (JINGLU, level))
            self.assertTrue(is_graded((JINGLU, level)))

    def test_unlisted_ai_types_keep_their_codes(self) -> None:
        """steel_type 4/5/6 占比极小，不判主料型，但回写时要认得出原来的码。"""
        for code in (4, 5, 6):
            self.assertEqual(canonical(code, 1), (code, 1))
            self.assertFalse(is_graded((code, 1)))

    def test_unlisted_manual_materials_go_to_other(self) -> None:
        for name in ("精炉料（杂模）", "中废", "杂料压块", "08铝压块", "车轮毂", "毛料"):
            with self.subTest(name=name):
                self.assertEqual(from_manual(name, 1), OTHER)
                self.assertFalse(is_graded(from_manual(name, 1)))

    def test_other_keeps_human_total_at_100(self) -> None:
        """鲁Q398NP：杂模 90% + 精炉料三 10%。杂模要占住 90%，不能被丢掉。"""
        rows = [
            (1, from_manual("精炉料（杂模）", 1), 90.0),
            (1, from_manual("精炉料", 3), 10.0),
            (2, from_manual("精炉料（杂模）", 1), 90.0),
            (2, from_manual("精炉料", 3), 10.0),
        ]
        avg = human_final_pcts(rows)
        self.assertEqual(avg, {OTHER: 90.0, (JINGLU, 3): 10.0})
        self.assertAlmostEqual(sum(avg.values()), 100.0)
        self.assertEqual(main_key(avg, rank=priority_rank), OTHER)
        self.assertFalse(is_graded(OTHER))


if __name__ == "__main__":
    unittest.main()
