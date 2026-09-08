from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monitor.config import BAND_PP
from monitor.correct import (
    apply_hunliao_ids,
    clamp_still_highest,
    display_boost_pp,
    display_main_pct,
    hunliao_stats,
    min_mix_uuid_count,
    plan_hunliao_flip,
    plan_volume,
    rebuild_inserts,
    _clone_insert,
)
from monitor.scan import merge_pending_ai
from monitor.sql_guard import SqlGuardError, assert_safe_sql


def _row(i: int, uid: str, st: int, vol: float, hun: int | None = 0) -> dict:
    return {
        "id": i,
        "uuid": uid,
        "steel_type": st,
        "total_volume": vol,
        "is_hunliao": hun,
        "task_code": "t",
        "car_number": "测A00000",
        "flow_code": "f",
        "create_time": "2026-08-21 10:00:00",
        "deduct_weight": 1.0 if st == 1 else None,
    }


class SqlGuardTests(unittest.TestCase):
    def test_allows_update_insert(self) -> None:
        assert_safe_sql("UPDATE slzj.intelli_task_check SET total_volume=1 WHERE id=1")
        assert_safe_sql("INSERT INTO slzj.intelli_task_check (steel_type) VALUES (1)")

    def test_rejects_delete(self) -> None:
        with self.assertRaises(SqlGuardError):
            assert_safe_sql("DELETE FROM slzj.intelli_task_check WHERE id=1")
        with self.assertRaises(SqlGuardError):
            assert_safe_sql("DROP TABLE slzj.intelli_task_check")


class HunliaoTests(unittest.TestCase):
    def test_flip_just_over_55_not_80(self) -> None:
        rows = []
        n = 0
        for i in range(100):
            n += 1
            rows.append(_row(n, f"u{i}", 1, 100, 1 if i < 20 else 0))
        plan = plan_hunliao_flip(rows, "seed-a")
        self.assertTrue(plan.ok)
        self.assertEqual(len(plan.uuids), 35)
        self.assertAlmostEqual(plan.mix_after, 0.55)
        self.assertLess(plan.mix_after, 0.80)

    def test_skip_if_min_mix_is_100(self) -> None:
        rows = [_row(1, "a", 1, 100, 0), _row(2, "b", 1, 100, 0)]
        plan = plan_hunliao_flip(rows, "tiny")
        self.assertFalse(plan.ok)
        self.assertEqual(plan.uuids, [])

    def test_min_count(self) -> None:
        self.assertEqual(min_mix_uuid_count(69), 38)
        self.assertEqual(min_mix_uuid_count(10), 6)
        self.assertEqual(min_mix_uuid_count(100), 55)


class VolumeTests(unittest.TestCase):
    def test_lower_keeps_scatter(self) -> None:
        rows = []
        n = 0
        for i in range(8):
            uid = f"g{i}"
            t1 = 9000 - i * 400
            t2 = 1000 + i * 80
            n += 1
            rows.append(_row(n, uid, 1, t1, 1))
            n += 1
            rows.append(_row(n, uid, 2, t2, 1))
        before = sum(r["total_volume"] for r in rows if r["steel_type"] == 1)
        total = sum(r["total_volume"] for r in rows)
        self.assertGreater(before / total, 0.75)
        plan = plan_volume(rows, 1, 55.0, "seed-vol")
        self.assertTrue(plan.ok, plan.reason)
        self.assertAlmostEqual(plan.after_pcts[1], 55.0, delta=3.5)
        self.assertGreater(plan.after_pcts[1], max(v for k, v in plan.after_pcts.items() if k != 1))
        self.assertGreaterEqual(len(set(round(x, 1) for x in plan.grab_samples)), 2)
        new_sum = 0.0
        for r in rows:
            new_sum += plan.volume_updates.get(r["id"], r["total_volume"])
        for ins in plan.inserts:
            new_sum += ins["total_volume"]
        self.assertAlmostEqual(new_sum, total, delta=total * 0.02)

    def test_raise_from_other_types(self) -> None:
        rows = []
        n = 0
        for i in range(6):
            uid = f"r{i}"
            n += 1
            rows.append(_row(n, uid, 1, 7000, 1))
            n += 1
            rows.append(_row(n, uid, 2, 3000, 1))
        plan = plan_volume(rows, 1, 88.0, "seed-up")
        self.assertTrue(plan.ok, plan.reason)
        self.assertGreater(plan.after_pcts[1], 80.0)

    def test_clamp_allows_target_when_current_other_is_above_band(self) -> None:
        out = clamp_still_highest(40.0, 40.0, 54.31)
        self.assertIsNotNone(out)
        self.assertLessEqual(out or 0.0, 49.7)


class SummaryRowTests(unittest.TestCase):
    def test_merge_fills_ai_from_pending(self) -> None:
        row = merge_pending_ai(
            {"plate": "豫EF8131", "status": "pending", "reason": "已在待审"},
            {"ai_pct": 71.94, "avg": 50.0, "gap": 21.94, "main_label": "重废1"},
        )
        self.assertEqual(row["ai_pct"], 71.94)
        self.assertEqual(row["avg"], 50.0)
        self.assertEqual(row["gap"], 21.94)
        self.assertEqual(row["main_label"], "重废1")

    def test_merge_keeps_existing(self) -> None:
        row = merge_pending_ai(
            {"ai_pct": 82.48, "avg": 100.0, "gap": 17.52},
            {"ai_pct": 1.0, "avg": 2.0, "gap": 3.0},
        )
        self.assertEqual(row["ai_pct"], 82.48)


class DisplayBoostTests(unittest.TestCase):
    def test_new_rule_bands(self) -> None:
        day = date(2026, 8, 24)
        self.assertEqual(display_boost_pp(45.0, day), 50.0)
        self.assertEqual(display_boost_pp(55.0, day), 40.0)
        self.assertEqual(display_boost_pp(65.0, day), 30.0)
        self.assertEqual(display_boost_pp(71.94, day), 20.0)
        self.assertEqual(display_boost_pp(82.48, day), 10.0)
        self.assertEqual(display_boost_pp(35.0, day), 0.0)
        self.assertEqual(display_boost_pp(90.0, day), 0.0)

    def test_old_rule_bands(self) -> None:
        day = date(2026, 7, 7)
        self.assertEqual(display_boost_pp(45.0, day), 0.0)
        self.assertEqual(display_boost_pp(55.0, day), 30.0)
        self.assertEqual(display_boost_pp(65.0, day), 20.0)
        self.assertEqual(display_boost_pp(75.0, day), 20.0)
        self.assertEqual(display_boost_pp(85.0, day), 10.0)

    def test_mix_uses_raw(self) -> None:
        shown, boost = display_main_pct(82.48, date(2026, 8, 23), is_mix=True)
        self.assertEqual(shown, 82.48)
        self.assertEqual(boost, 0.0)

    def test_nonmix_compare_can_enter_band(self) -> None:
        shown, boost = display_main_pct(82.48, date(2026, 8, 23), is_mix=False)
        self.assertAlmostEqual(shown, 92.48)
        self.assertEqual(boost, 10.0)
        self.assertLess(abs(shown - 100.0), BAND_PP)

    def test_nonmix_still_out_of_band(self) -> None:
        shown, _boost = display_main_pct(71.94, date(2026, 8, 24), is_mix=False)
        self.assertAlmostEqual(shown, 91.94)
        self.assertGreater(abs(shown - 50.0), BAND_PP)


class CloneInsertTests(unittest.TestCase):
    def test_clone_copies_images_clears_primary_fields(self) -> None:
        src = _row(1, "u", 1, 100, 0)
        src["images"] = "http://x/render.jpg"
        src["origin_image_url"] = "http://x/origin.jpg"
        src["deduct_weight_url"] = "http://x/d.jpg"
        cloned = _clone_insert(src, 2, 50)
        self.assertEqual(cloned["images"], "http://x/render.jpg")
        self.assertEqual(cloned["steel_type"], 2)
        self.assertAlmostEqual(cloned["total_volume"], 50)
        self.assertIsNone(cloned["origin_image_url"])
        self.assertIsNone(cloned["is_hunliao"])
        self.assertIsNone(cloned["deduct_weight"])
        self.assertIsNone(cloned["deduct_weight_url"])
        self.assertNotIn("id", cloned)

    def test_rebuild_inserts_uses_sibling_images(self) -> None:
        live = [_row(1, "u", 1, 100, 0)]
        live[0]["images"] = "http://img"
        live[0]["origin_image_url"] = "http://o"
        planned = [{"uuid": "u", "steel_type": 2, "total_volume": 5}]
        out = rebuild_inserts(planned, live)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["images"], "http://img")
        self.assertIsNone(out[0]["origin_image_url"])
        self.assertIsNone(out[0]["is_hunliao"])

    def test_rebuild_inserts_rejects_empty_images(self) -> None:
        live = [_row(1, "u", 1, 100, 0)]
        with self.assertRaises(RuntimeError):
            rebuild_inserts([{"uuid": "u", "steel_type": 2, "total_volume": 5}], live)


class HunliaoIdTests(unittest.TestCase):
    def test_only_existing_flag_one_per_uuid(self) -> None:
        rows = [
            _row(1, "u", 1, 100, 0),
            _row(2, "u", 5, 10, None),
            _row(3, "u", 2, 10, None),
        ]
        self.assertEqual(apply_hunliao_ids(rows, ["u"]), [1])

    def test_skips_uuid_without_flag(self) -> None:
        rows = [_row(1, "u", 1, 100, None), _row(2, "u", 2, 10, None)]
        self.assertEqual(apply_hunliao_ids(rows, ["u"]), [])

    def test_prefers_origin_row_when_multiple_flags(self) -> None:
        a = _row(10, "u", 2, 10, 0)
        b = _row(11, "u", 1, 90, 0)
        b["origin_image_url"] = "http://o"
        self.assertEqual(apply_hunliao_ids([a, b], ["u"]), [11])


if __name__ == "__main__":
    unittest.main()
