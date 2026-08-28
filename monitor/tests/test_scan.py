from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monitor.scan import (
    attach_human_fields,
    humans_from_checks,
    judge_main_types,
    select_manual_checks,
)


def _check(check_id: int, when: str, name: str = "A") -> dict:
    return {"id": check_id, "create_time": when, "operator_name": name}


class SelectManualTests(unittest.TestCase):
    def test_need_two(self) -> None:
        self.assertEqual(select_manual_checks([_check(1, "2026-08-25 10:00:00")], 3), [])

    def test_watch_today_keeps_two_even_if_three_exist(self) -> None:
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
        self.assertEqual(judge_main_types(humans), "")

    def test_two_disagree(self) -> None:
        humans = [
            {"steel_type": 1, "pct": 40.0},
            {"steel_type": 2, "pct": 45.0},
        ]
        self.assertEqual(judge_main_types(humans), "两人主料型不一致")

    def test_three_third_disagrees(self) -> None:
        humans = [
            {"steel_type": 1, "pct": 40.0},
            {"steel_type": 1, "pct": 40.0},
            {"steel_type": 2, "pct": 45.0},
        ]
        self.assertEqual(judge_main_types(humans), "三人主料型不一致")

    def test_three_agree_average(self) -> None:
        humans = [
            {"steel_type": 1, "label": "重废1", "pct": 40.0, "name": "甲"},
            {"steel_type": 1, "label": "重废1", "pct": 50.0, "name": "乙"},
            {"steel_type": 1, "label": "重废1", "pct": 30.0, "name": "丙"},
        ]
        self.assertEqual(judge_main_types(humans), "")
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
        self.assertIn("主料型不是重废1/2", judge_main_types(humans))

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


if __name__ == "__main__":
    unittest.main()
