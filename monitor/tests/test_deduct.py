from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monitor.config import DEDUCT_BAND_KG, KIND_DEDUCT, KIND_VOLUME
from monitor.deduct import (
    apply_page_rules,
    calculate_deduct,
    deduct_gap_kg,
    estimate_deduct_kg,
    human_deduct_kg,
    invert_base_to_accounted,
    is_deduct_unqualified,
    plan_deduct_scale,
    raw_deduct_kg,
    reread_deduct,
)
from monitor.store import card_kind, ticket_key


class PageRuleTests(unittest.TestCase):
    def test_html_calculator_default(self) -> None:
        page = apply_page_rules(0.08 * 20000, 205)
        self.assertAlmostEqual(page.base_kg, 1600.0)
        self.assertAlmostEqual(page.pre_kg, 1450.0)
        self.assertAlmostEqual(page.ai_kg, 2950.0)
        self.assertAlmostEqual(page.original_kg, 590.0)

    def test_aat918_plus_50_then_div2(self) -> None:
        rows = [{"deduct_weight": 0.0022, "total_volume": 1.0}]
        ai = estimate_deduct_kg(rows, 75840, 0.0, 0.0022)
        page = apply_page_rules(0.0022 * 75840, 0.0)
        self.assertAlmostEqual(ai, 216.85, places=2)
        self.assertAlmostEqual(page.original_kg, 108.42, places=2)
        self.assertTrue(is_deduct_unqualified(ai, 525.0))

    def test_p55225_random_then_div2(self) -> None:
        rows = [{"deduct_weight": 0.0033631423372891493, "total_volume": 1.0}]
        self.assertAlmostEqual(raw_deduct_kg(rows, 22040), 74.12, places=2)
        ai = estimate_deduct_kg(rows, 22040, 201.4195093474081)
        page = apply_page_rules(0.0034 * 22040, 201.4195093474081)
        self.assertAlmostEqual(ai, 276.36, places=2)
        self.assertAlmostEqual(page.original_kg, 138.18, places=2)
        self.assertFalse(is_deduct_unqualified(ai, 290.0))

    def test_ef8131_minus_150(self) -> None:
        page = apply_page_rules(0.0141 * 42660, 0.0)
        self.assertAlmostEqual(page.base_kg, 601.51, places=2)
        self.assertAlmostEqual(page.ai_kg, 451.51, places=2)
        self.assertFalse(is_deduct_unqualified(page.ai_kg, 425.0))

    def test_live_base_ignores_stale_rate_nd3039(self) -> None:
        rows = [{"deduct_weight": 0.0141, "total_volume": 1.0}]
        page = calculate_deduct(rows, 39160.0, 0.0, 0.0212)
        self.assertAlmostEqual(page.base_kg, 552.16, places=2)
        self.assertAlmostEqual(page.ai_kg, 402.16, places=2)
        self.assertFalse(is_deduct_unqualified(page.ai_kg, 300.0))

    def test_live_base_ay5106_in_band(self) -> None:
        rows = [{"deduct_weight": 0.0051, "total_volume": 1.0}]
        page = calculate_deduct(rows, 54620.0, 0.0, 0.0152)
        self.assertAlmostEqual(page.ai_kg, 278.56, places=2)
        self.assertFalse(is_deduct_unqualified(page.ai_kg, 246.67))


class HumanTests(unittest.TestCase):
    def test_human_average_tons_to_kg(self) -> None:
        self.assertAlmostEqual(human_deduct_kg([0.40, 0.50]), 450.0)

    def test_human_average_three_tons_to_kg(self) -> None:
        self.assertAlmostEqual(human_deduct_kg([0.30, 0.30, 0.35]), 316.6666666, places=4)

    def test_human_empty(self) -> None:
        self.assertIsNone(human_deduct_kg([None, ""]))


class BandTests(unittest.TestCase):
    def test_over_150_is_unqualified(self) -> None:
        self.assertTrue(is_deduct_unqualified(603.44, 425.0))
        self.assertGreater(deduct_gap_kg(603.44, 425.0), DEDUCT_BAND_KG)

    def test_150_or_less_is_ok(self) -> None:
        self.assertFalse(is_deduct_unqualified(500.0, 350.0))
        self.assertFalse(is_deduct_unqualified(400.0, 400.0))


class ScaleTests(unittest.TestCase):
    def test_scale_keep_band_hits_accounted(self) -> None:
        rows = [
            {"id": 1, "deduct_weight": 0.3, "total_volume": 1.0},
            {"id": 2, "deduct_weight": None, "total_volume": 0},
        ]
        net = 1000.0
        current = estimate_deduct_kg(rows, net)
        self.assertAlmostEqual(current, 300.0)
        plan = plan_deduct_scale(rows, current, 350.0, net_weight=net)
        self.assertTrue(plan.ok, plan.reason)
        scaled = [{**rows[0], "deduct_weight": plan.updates[1]}]
        after = estimate_deduct_kg(scaled, net)
        self.assertAlmostEqual(after, 350.0, places=2)

    def test_invert_p55225_target_290(self) -> None:
        base = invert_base_to_accounted(290.0, 201.4195093474081, prefer_base=74.94)
        self.assertIsNotNone(base)
        page = apply_page_rules(base or 0.0, 201.4195093474081)
        self.assertAlmostEqual(page.ai_kg, 290.0, places=2)

    def test_scale_rejects_zero_base(self) -> None:
        plan = plan_deduct_scale(
            [{"id": 1, "deduct_weight": 0, "total_volume": 10}],
            0,
            100,
            net_weight=1000,
        )
        self.assertFalse(plan.ok)

    def test_scale_rejects_zero_target(self) -> None:
        plan = plan_deduct_scale(
            [{"id": 1, "deduct_weight": 1, "total_volume": 1}],
            300,
            0,
            net_weight=1000,
        )
        self.assertFalse(plan.ok)

    def test_reread_marks_in_band(self) -> None:
        rows = [{"deduct_weight": 0.3, "total_volume": 1.0}]
        out = reread_deduct(rows, 1000, 290)
        self.assertTrue(out["in_band"])
        self.assertAlmostEqual(out["ai_kg"], 300.0)
        self.assertAlmostEqual(out["original_kg"], 150.0)


class StoreKindTests(unittest.TestCase):
    def test_keys_split_volume_and_deduct(self) -> None:
        self.assertEqual(ticket_key("A", "2026-08-24"), "A|2026-08-24")
        self.assertEqual(ticket_key("A", "2026-08-24", KIND_DEDUCT), "A|2026-08-24|deduct")
        self.assertEqual(card_kind({}), KIND_VOLUME)
        self.assertEqual(card_kind({"kind": KIND_DEDUCT}), KIND_DEDUCT)


if __name__ == "__main__":
    unittest.main()
