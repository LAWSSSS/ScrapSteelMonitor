from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from datetime import date

from monitor.correct import car_type_pcts, clamp_still_highest
from monitor.config import KIND_DEDUCT, KIND_VOLUME
from monitor.scan import (
    apply_type_choice,
    attach_human_fields,
    correctable_type_options,
    avg_for_chosen_type,
    consensus_type_avgs,
    default_chosen_type,
    humans_from_checks,
    judge_main_types,
    plan_type_option,
    reopen_processed,
    select_manual_checks,
)


def _check(check_id: int, when: str, name: str = "A") -> dict:
    return {"id": check_id, "create_time": when, "operator_name": name}


class SelectManualTests(unittest.TestCase):
    def test_one_is_enough_for_volume(self) -> None:
        picked = select_manual_checks([_check(1, "2026-08-25 10:00:00")], 3)
        self.assertEqual([int(r["id"]) for r in picked], [1])

    def test_deduct_still_needs_two(self) -> None:
        self.assertEqual(
            select_manual_checks([_check(1, "2026-08-25 10:00:00")], 3, min_manuals=2),
            [],
        )

    def test_watch_today_keeps_two_if_capped(self) -> None:
        group = [
            _check(1, "2026-08-25 10:00:00", "甲"),
            _check(2, "2026-08-25 10:00:10", "乙"),
            _check(3, "2026-08-25 10:00:20", "丙"),
        ]
        picked = select_manual_checks(group, 2)
        self.assertEqual([int(r["id"]) for r in picked], [1, 2])

    def test_history_uses_three(self) -> None:
        group = [
            _check(3, "2026-08-25 10:00:20", "丙"),
            _check(1, "2026-08-25 10:00:00", "甲"),
            _check(2, "2026-08-25 10:00:10", "乙"),
        ]
        picked = select_manual_checks(group, 3)
        self.assertEqual([int(r["id"]) for r in picked], [1, 2, 3])

    def test_history_with_only_two_stays_two(self) -> None:
        group = [_check(1, "2026-08-25 10:00:00"), _check(2, "2026-08-25 10:00:10")]
        self.assertEqual(len(select_manual_checks(group, 3)), 2)

    def test_history_caps_at_three(self) -> None:
        group = [_check(i, f"2026-08-25 10:00:0{i}") for i in range(1, 5)]
        picked = select_manual_checks(group, 3)
        self.assertEqual([int(r["id"]) for r in picked], [1, 2, 3])


class JudgeMainTests(unittest.TestCase):
    def test_two_agree(self) -> None:
        humans = [
            {"steel_type": 1, "pct": 40.0, "name": "甲"},
            {"steel_type": 1, "pct": 40.0, "name": "乙"},
        ]
        gate = judge_main_types(humans)
        self.assertEqual(gate.skip_reason, "")
        self.assertTrue(gate.auto_ok)
        self.assertFalse(gate.needs_choice)
        self.assertEqual(gate.choosable, (1,))
        self.assertEqual(gate.default_type, 1)

    def test_one_allowed_not_auto(self) -> None:
        humans = [{"steel_type": 2, "pct": 70.0, "name": "甲"}]
        gate = judge_main_types(humans)
        self.assertEqual(gate.skip_reason, "")
        self.assertFalse(gate.auto_ok)
        self.assertFalse(gate.needs_choice)
        self.assertEqual(gate.choosable, (2,))
        extra: dict = {}
        attach_human_fields(extra, humans, gate.default_type)
        self.assertEqual(extra["humans_used"], 1)
        self.assertNotIn("human2", extra)
        self.assertEqual(extra["avg"], 70.0)

    def test_two_disagree_needs_choice(self) -> None:
        humans = [
            {"steel_type": 1, "pct": 40.0},
            {"steel_type": 2, "pct": 45.0},
        ]
        gate = judge_main_types(humans)
        self.assertEqual(gate.skip_reason, "")
        self.assertFalse(gate.auto_ok)
        self.assertTrue(gate.needs_choice)
        self.assertEqual(gate.choosable, (1, 2))
        self.assertEqual(gate.default_type, 1)

    def test_three_third_disagrees_majority(self) -> None:
        humans = [
            {"steel_type": 1, "pct": 80.0},
            {"steel_type": 1, "pct": 70.0},
            {"steel_type": 2, "pct": 90.0},
        ]
        gate = judge_main_types(humans)
        self.assertTrue(gate.needs_choice)
        self.assertFalse(gate.auto_ok)
        self.assertEqual(gate.default_type, 1)
        self.assertEqual(avg_for_chosen_type(humans, 1), 75.0)
        self.assertEqual(avg_for_chosen_type(humans, 2), 90.0)

    def test_three_agree_average(self) -> None:
        humans = [
            {"steel_type": 1, "label": "重废1", "pct": 40.0, "name": "甲"},
            {"steel_type": 1, "label": "重废1", "pct": 50.0, "name": "乙"},
            {"steel_type": 1, "label": "重废1", "pct": 30.0, "name": "丙"},
        ]
        gate = judge_main_types(humans)
        self.assertEqual(gate.skip_reason, "")
        self.assertTrue(gate.auto_ok)
        extra: dict = {}
        attach_human_fields(extra, humans)
        self.assertEqual(extra["humans_used"], 3)
        self.assertEqual(extra["avg"], 40.0)
        self.assertEqual(extra["human3"]["pct"], 30.0)

    def test_not_allowed_type(self) -> None:
        humans = [
            {"steel_type": 3, "pct": 80.0},
            {"steel_type": 3, "pct": 80.0},
        ]
        gate = judge_main_types(humans)
        self.assertIn("主料型不是重废1/2", gate.skip_reason)

    def test_zhongfei_plus_one_only_allows_heavy(self) -> None:
        humans = [
            {"steel_type": 11, "pct": 60.0},
            {"steel_type": 11, "pct": 55.0},
            {"steel_type": 1, "pct": 80.0},
        ]
        gate = judge_main_types(humans)
        self.assertEqual(gate.choosable, (1,))
        self.assertTrue(gate.needs_choice)
        self.assertEqual(gate.default_type, 1)

    def test_tie_picks_earliest(self) -> None:
        humans = [
            {"steel_type": 2, "pct": 90.0},
            {"steel_type": 1, "pct": 80.0},
        ]
        self.assertEqual(default_chosen_type(humans, (1, 2)), 2)

    def test_humans_from_checks_picks_highest_rate(self) -> None:
        checks = [_check(10, "2026-08-25 10:00:00", "甲")]
        details = {
            10: [
                {"steel_type": 2, "steel_rate": 0.30},
                {"steel_type": 1, "steel_rate": 0.40},
            ]
        }
        humans = humans_from_checks(checks, details)
        self.assertEqual(humans[0]["steel_type"], 1)
        self.assertEqual(humans[0]["pct"], 40.0)
        self.assertEqual(humans[0]["rates"], {1: 40.0, 2: 30.0})


class ChooseTypeTests(unittest.TestCase):
    def test_apply_type_choice_switches_option(self) -> None:
        card = {
            "weight_bill_no": "JC1",
            "biz_date": "2026-08-28",
            "main_type": 1,
            "type_options": [
                {"main_type": 1, "main_label": "重废1", "avg": 75.0, "target": 80.0, "ok": True},
                {"main_type": 2, "main_label": "重废2", "avg": 90.0, "target": 88.0, "ok": True},
            ],
        }

        saved: dict = {}

        with patch("monitor.scan.load_pending", return_value=card), patch(
            "monitor.scan.save_pending",
            side_effect=lambda updated: saved.update(updated),
        ):
            out = apply_type_choice("JC1", "2026-08-28", 2)
        self.assertEqual(out["main_type"], 2)
        self.assertEqual(out["avg"], 90.0)
        self.assertEqual(out["target"], 88.0)
        self.assertTrue(out["type_chosen"])
        self.assertEqual(saved.get("main_type"), 2)

    def test_apply_type_choice_rejects_unknown(self) -> None:
        card = {
            "type_options": [{"main_type": 1, "main_label": "重废1", "ok": True}],
        }
        with patch("monitor.scan.load_pending", return_value=card):
            with self.assertRaises(ValueError):
                apply_type_choice("JC1", "2026-08-28", 2)


class ClampAndRaiseTests(unittest.TestCase):
    def test_clamp_keeps_band_when_current_leader_is_above(self) -> None:
        out = clamp_still_highest(40.0, 40.0, 92.04)
        self.assertIsNotNone(out)
        self.assertAlmostEqual(out or 0.0, 40.0, places=2)

    def test_raise_second_type_steals_from_leader(self) -> None:
        rows: list[dict] = []
        n = 0
        for i in range(8):
            uid = f"g{i}"  # one grab per loop
            for st, vol in ((1, 92.0), (2, 5.0), (3, 3.0)):
                n += 1
                rows.append(
                    {
                        "id": n,
                        "uuid": uid,
                        "steel_type": st,
                        "total_volume": vol,
                        "is_hunliao": 0,
                        "task_code": "t",
                        "car_number": "测A",
                        "flow_code": "f",
                        "create_time": "2026-09-08 08:00:00",
                    }
                )
        humans = [
            {"steel_type": 2, "pct": 40.0, "name": "甲", "label": "重废2"},
            {"steel_type": 1, "pct": 40.0, "name": "乙", "label": "重废1"},
        ]
        opt = plan_type_option(
            day=date(2026, 9, 8),
            humans=humans,
            steel_type=2,
            rows=rows,
            pcts=car_type_pcts(rows),
            hun={"is_mix": False},
            seed="nc8136-like",
            flip_uuids=[],
            mix_after=0.1,
        )
        self.assertTrue(opt["ok"], opt.get("reason"))
        self.assertGreater(opt["after_pcts"][2], opt["after_pcts"].get(1, 0))


class ConsensusAvgTests(unittest.TestCase):
    """桂E60521: personal max differs, all-manual average still 重废2."""

    def _e60521_humans(self) -> list[dict]:
        return [
            {
                "steel_type": 2,
                "pct": 35.0,
                "name": "徐星星",
                "rates": {2: 35.0, 11: 32.0, 1: 23.0, 3: 10.0},
            },
            {
                "steel_type": 1,
                "pct": 40.0,
                "name": "陈魁",
                "rates": {1: 40.0, 2: 30.0, 3: 20.0, 11: 10.0},
            },
        ]

    def test_all_manual_avg_not_just_voters(self) -> None:
        humans = self._e60521_humans()
        self.assertEqual(avg_for_chosen_type(humans, 2), 32.5)
        self.assertEqual(avg_for_chosen_type(humans, 1), 31.5)
        main, avg, pcts = consensus_type_avgs(humans, (1, 2))
        self.assertEqual(main, 2)
        self.assertEqual(avg, 32.5)
        self.assertEqual(pcts, {1: 31.5, 2: 32.5})

    def test_default_follows_consensus_not_earliest_max(self) -> None:
        humans = [
            {
                "steel_type": 1,
                "pct": 40.0,
                "rates": {1: 40.0, 2: 30.0},
            },
            {
                "steel_type": 2,
                "pct": 35.0,
                "rates": {2: 35.0, 1: 23.0},
            },
        ]
        self.assertEqual(default_chosen_type(humans, (1, 2)), 2)

    def test_ai_vs_voter_in_band_but_consensus_out(self) -> None:
        humans = self._e60521_humans()
        opt = plan_type_option(
            day=date(2026, 9, 16),
            humans=humans,
            steel_type=2,
            rows=[],
            pcts={2: 44.08, 1: 25.0, 11: 20.0, 3: 10.92},
            hun={"is_mix": True},
            seed="e60521",
            flip_uuids=[],
            mix_after=0.44,
        )
        self.assertEqual(opt["avg"], 32.5)
        self.assertAlmostEqual(opt["gap"], 11.58, places=2)
        self.assertFalse(opt["in_band"])
        self.assertGreaterEqual(opt["gap"], 10.0)
        self.assertLess(abs(44.08 - 35.0), 10.0)

    def test_consensus_in_band_when_ai_lead_matches(self) -> None:
        humans = self._e60521_humans()
        opt = plan_type_option(
            day=date(2026, 9, 16),
            humans=humans,
            steel_type=2,
            rows=[],
            pcts={2: 36.0, 1: 25.0, 11: 20.0, 3: 19.0},
            hun={"is_mix": True},
            seed="e60521-in",
            flip_uuids=[],
            mix_after=0.44,
        )
        self.assertTrue(opt["in_band"])
        self.assertAlmostEqual(opt["gap"], 3.5, places=2)


class CorrectableOptionTests(unittest.TestCase):
    def test_both_in_band_not_correctable(self) -> None:
        options = [
            {"main_type": 2, "ok": False, "in_band": True},
            {"main_type": 1, "ok": False, "in_band": True},
        ]
        self.assertEqual(correctable_type_options(options), [])

    def test_out_of_band_ok_is_correctable(self) -> None:
        options = [
            {"main_type": 2, "ok": False, "in_band": True},
            {"main_type": 1, "ok": True, "in_band": False},
        ]
        picked = correctable_type_options(options)
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0]["main_type"], 1)


class ReopenProcessedTests(unittest.TestCase):
    def test_reopen_clears_volume_stamp(self) -> None:
        with patch("monitor.scan.is_processed", return_value=True), patch(
            "monitor.scan.unmark_processed"
        ) as unmark:
            self.assertTrue(reopen_processed("JC1", "2026-09-08"))
            unmark.assert_called_once_with("JC1", "2026-09-08", KIND_VOLUME)

    def test_reopen_clears_deduct_stamp(self) -> None:
        with patch("monitor.scan.is_processed", return_value=True), patch(
            "monitor.scan.unmark_processed"
        ) as unmark:
            self.assertTrue(reopen_processed("JC1", "2026-09-08", KIND_DEDUCT))
            unmark.assert_called_once_with("JC1", "2026-09-08", KIND_DEDUCT)

    def test_reopen_noop_when_not_stamped(self) -> None:
        with patch("monitor.scan.is_processed", return_value=False), patch(
            "monitor.scan.unmark_processed"
        ) as unmark:
            self.assertFalse(reopen_processed("JC1", "2026-09-08"))
            unmark.assert_not_called()


if __name__ == "__main__":
    unittest.main()
