"""镔鑫修正规划的单测。行数据按真库形状手写：一个 uuid 多行料型，共享 images。"""

from __future__ import annotations

import unittest

from monitor.binxin.correct import (
    CLONE_CLEAR,
    LEAD_GAP_PP,
    MIN_SPREAD_PP,
    TARGET_TOL_PP,
    car_key_pcts,
    group_by_uuid,
    pick_clone_source,
    plan_grabs,
    preview,
    rebuild_stat_rows,
)
from monitor.binxin.materials import JINGLU, OTHER, ZHONGFEI_KEY
from monitor.binxin.rate import in_band, main_key, normalize
from monitor.binxin.materials import priority_rank

IMG = "http://172.31.1.102:19000/scrape-steel/2026-09-19/abc_render_img_1.jpg"
ORIG = "http://172.31.1.102:19000/scrape-steel/2026-09-19/abc_origin_1.jpg"


def _row(
    row_id: int,
    uid: str,
    steel_type: int,
    steel_level: int,
    volume: float,
    *,
    deduct: float | None = None,
) -> dict[str, object]:
    return {
        "id": row_id,
        "uuid": uid,
        "flow_code": "flow-1",
        "task_code": "task-1",
        "car_number": "苏GC7733",
        "steel_type": steel_type,
        "steel_level": steel_level,
        "total_volume": volume,
        "old_total_volume": volume,
        "images": IMG,
        "origin_image_url": ORIG,
        "status": 2,
        "check_type": 1,
        "station_number": 4,
        "acc_time": 1789783379625,
        "deduct_weight": deduct,
    }


def _car() -> list[dict[str, object]]:
    """4 个抓斗，精炉料2 占比偏低，形如苏GC7733。"""
    rows: list[dict[str, object]] = []
    next_id = 1
    layout = (
        ("u1", ((1, 1, 500.0), (1, 2, 300.0), (3, 1, 40.0))),
        ("u2", ((1, 1, 420.0), (1, 2, 380.0), (3, 1, 30.0), (1, 3, 10.0))),
        ("u3", ((1, 1, 300.0), (1, 2, 260.0))),
        ("u4", ((1, 1, 280.0), (1, 2, 240.0), (3, 1, 20.0))),
    )
    for uid, entries in layout:
        first = True
        for steel_type, steel_level, volume in entries:
            rows.append(
                _row(
                    next_id,
                    uid,
                    steel_type,
                    steel_level,
                    volume,
                    deduct=100.0 if first else None,
                )
            )
            next_id += 1
            first = False
    return rows


def _after_rows(rows: list[dict[str, object]], plan) -> list[dict[str, object]]:
    """把方案套回行上，得到改后的行集合，用来独立复算占比。"""
    out: list[dict[str, object]] = []
    for row in rows:
        clone = dict(row)
        if int(row["id"]) in plan.volume_updates:
            clone["total_volume"] = plan.volume_updates[int(row["id"])]
        out.append(clone)
    out.extend(dict(ins) for ins in plan.inserts)
    return out


class GroupingTests(unittest.TestCase):
    def test_group_by_uuid_merges_zhongfei_levels(self) -> None:
        rows = [_row(1, "u1", 3, 1, 10.0), _row(2, "u1", 3, 2, 20.0), _row(3, "u1", 1, 1, 70.0)]
        grouped = group_by_uuid(rows)
        self.assertEqual(len(grouped["u1"][ZHONGFEI_KEY]), 2)
        self.assertEqual(len(grouped["u1"][(JINGLU, 1)]), 1)

    def test_car_pcts_match_volume_share(self) -> None:
        pcts = car_key_pcts([_row(1, "u1", 1, 1, 75.0), _row(2, "u1", 1, 2, 25.0)])
        self.assertAlmostEqual(pcts[(JINGLU, 1)], 75.0)
        self.assertAlmostEqual(pcts[(JINGLU, 2)], 25.0)

    def test_clone_source_prefers_row_with_image(self) -> None:
        blank = _row(1, "u1", 1, 1, 10.0)
        blank["images"] = None
        good = _row(2, "u1", 1, 2, 10.0)
        self.assertIs(pick_clone_source([blank, good]), good)


class PlanTests(unittest.TestCase):
    def test_raises_target_share(self) -> None:
        rows = _car()
        before = car_key_pcts(rows)[(JINGLU, 2)]
        plan = plan_grabs(rows, (JINGLU, 2), 64.5, seed="苏GC7733", human_pct=95.0)
        self.assertTrue(plan.ok, plan.reason)
        self.assertLess(before, 64.5)
        self.assertAlmostEqual(
            plan.after_raw_pcts[(JINGLU, 2)], 64.5, delta=TARGET_TOL_PP
        )

    def test_lowers_target_share(self) -> None:
        rows = _car()
        plan = plan_grabs(rows, (JINGLU, 1), 47.0, seed="s", human_pct=50.0)
        self.assertTrue(plan.ok, plan.reason)
        self.assertAlmostEqual(plan.after_raw_pcts[(JINGLU, 1)], 47.0, delta=TARGET_TOL_PP)

    def test_recomputing_from_rows_reproduces_plan(self) -> None:
        """独立复算：把方案套回行上，重新算占比，必须和方案自己报的一致。"""
        rows = _car()
        plan = plan_grabs(rows, (JINGLU, 2), 64.5, seed="s", human_pct=95.0)
        self.assertTrue(plan.ok, plan.reason)
        recomputed = car_key_pcts(_after_rows(rows, plan))
        for key, pct in plan.after_raw_pcts.items():
            self.assertAlmostEqual(recomputed.get(key, 0.0), pct, delta=0.02)

    def test_target_is_highest_after(self) -> None:
        rows = _car()
        plan = plan_grabs(rows, (JINGLU, 2), 64.5, seed="s", human_pct=95.0)
        self.assertTrue(plan.ok, plan.reason)
        self.assertEqual(main_key(plan.after_raw_pcts, rank=priority_rank), (JINGLU, 2))
        rivals = [v for k, v in plan.after_raw_pcts.items() if k != (JINGLU, 2)]
        self.assertLessEqual(max(rivals), plan.after_raw_pcts[(JINGLU, 2)] - LEAD_GAP_PP + 0.5)

    def test_display_lands_in_band(self) -> None:
        rows = _car()
        plan = plan_grabs(rows, (JINGLU, 2), 64.5, seed="s", human_pct=95.0)
        self.assertTrue(plan.ok, plan.reason)
        display = plan.after_display_pcts[(JINGLU, 2)]
        self.assertTrue(in_band(95.0, display, same_main_type=True))
        # 与直接对改后占比跑规范化的结果一致
        self.assertAlmostEqual(
            normalize(plan.after_raw_pcts, quantize=False)[(JINGLU, 2)], display, places=6
        )

    def test_volume_is_conserved(self) -> None:
        """只搬不造：改前后整车总体积不变。"""
        rows = _car()
        plan = plan_grabs(rows, (JINGLU, 2), 64.5, seed="s", human_pct=95.0)
        self.assertTrue(plan.ok, plan.reason)
        before = sum(float(r["total_volume"]) for r in rows)
        after = sum(float(r["total_volume"]) for r in _after_rows(rows, plan))
        self.assertAlmostEqual(before, after, delta=before * 1e-6)

    def test_each_grab_shows_the_target_as_its_head(self) -> None:
        """详情是一斗一斗看的。整车对了但多数斗仍是旧料型占大头，页面会对不上。"""
        rows = _car()
        plan = plan_grabs(rows, (JINGLU, 2), 64.5, seed="s", human_pct=95.0)
        self.assertTrue(plan.ok, plan.reason)
        after = _after_rows(rows, plan)
        by_uuid: dict[str, dict[tuple[int, int], float]] = {}
        for row in after:
            uid = str(row["uuid"])
            key = (int(row["steel_type"]), int(row["steel_level"]))
            by_uuid.setdefault(uid, {})
            by_uuid[uid][key] = by_uuid[uid].get(key, 0.0) + float(row["total_volume"])
        for uid, vols in by_uuid.items():
            target = vols.get((JINGLU, 2), 0.0)
            rivals = [amount for key, amount in vols.items() if key != (JINGLU, 2)]
            self.assertGreater(target, max(rivals, default=0.0), msg=uid)

    def test_grabs_stay_uneven(self) -> None:
        rows = _car()
        plan = plan_grabs(rows, (JINGLU, 2), 64.5, seed="s", human_pct=95.0)
        self.assertTrue(plan.ok, plan.reason)
        self.assertGreaterEqual(plan.spread_pp, MIN_SPREAD_PP)
        self.assertGreater(len(set(plan.grab_shares)), 1)

    def test_same_seed_same_plan(self) -> None:
        first = plan_grabs(_car(), (JINGLU, 2), 64.5, seed="s")
        second = plan_grabs(_car(), (JINGLU, 2), 64.5, seed="s")
        self.assertEqual(first.volume_updates, second.volume_updates)

    def test_different_seed_different_split(self) -> None:
        first = plan_grabs(_car(), (JINGLU, 2), 64.5, seed="a")
        second = plan_grabs(_car(), (JINGLU, 2), 64.5, seed="b")
        self.assertNotEqual(first.volume_updates, second.volume_updates)


class InsertTests(unittest.TestCase):
    def test_insert_carries_image_and_drops_deduct(self) -> None:
        """精炉料3 只在 u2 出现，其余 uuid 要补行；补的行必须带图、不能带扣重。"""
        rows = _car()
        plan = plan_grabs(rows, (JINGLU, 3), 55.0, seed="s")
        self.assertTrue(plan.ok, plan.reason)
        self.assertTrue(plan.inserts)
        for ins in plan.inserts:
            self.assertEqual(ins["images"], IMG)
            self.assertEqual(ins["origin_image_url"], ORIG)
            for dropped in CLONE_CLEAR:
                self.assertNotIn(dropped, ins)

    def test_insert_keeps_uuid_metadata(self) -> None:
        plan = plan_grabs(_car(), (JINGLU, 3), 55.0, seed="s")
        self.assertTrue(plan.ok, plan.reason)
        for ins in plan.inserts:
            self.assertIn(ins["uuid"], {"u1", "u2", "u3", "u4"})
            self.assertEqual(ins["flow_code"], "flow-1")
            self.assertEqual(ins["acc_time"], 1789783379625)

    def test_deduct_total_unchanged(self) -> None:
        """一个 uuid 只有一行挂 deduct_weight，改完整车扣重之和不能变。"""
        rows = _car()
        plan = plan_grabs(rows, (JINGLU, 3), 55.0, seed="s")
        self.assertTrue(plan.ok, plan.reason)
        before = sum(float(r["deduct_weight"] or 0) for r in rows)
        after = sum(float(r.get("deduct_weight") or 0) for r in _after_rows(rows, plan))
        self.assertAlmostEqual(before, after)

    def test_refuses_to_insert_without_image(self) -> None:
        rows = [
            _row(1, "u1", 1, 1, 100.0),
            _row(2, "u2", 1, 1, 100.0),
        ]
        for row in rows:
            row["images"] = None
        plan = plan_grabs(rows, (JINGLU, 2), 55.0, seed="s")
        self.assertFalse(plan.ok)
        self.assertIn("images", plan.reason)


class RejectTests(unittest.TestCase):
    def test_empty_rows(self) -> None:
        plan = plan_grabs([], (JINGLU, 2), 64.5, seed="s")
        self.assertFalse(plan.ok)

    def test_zero_volume(self) -> None:
        plan = plan_grabs([_row(1, "u1", 1, 1, 0.0)], (JINGLU, 2), 64.5, seed="s")
        self.assertFalse(plan.ok)

    def test_ungraded_target(self) -> None:
        plan = plan_grabs(_car(), OTHER, 64.5, seed="s")
        self.assertFalse(plan.ok)
        self.assertIn("不在可判定料型内", plan.reason)

    def test_rejects_when_display_misses_band(self) -> None:
        """目标占比自己就不对时，人工带的校验要拦住，不能出一个没用的方案。"""
        plan = plan_grabs(_car(), (JINGLU, 2), 64.5, seed="s", human_pct=50.0)
        self.assertFalse(plan.ok)
        self.assertIn("不在人工", plan.reason)

    def test_preview_says_why_when_unusable(self) -> None:
        plan = plan_grabs([], (JINGLU, 2), 64.5, seed="s")
        self.assertIn("方案不可用", preview(plan))


class StatRebuildTests(unittest.TestCase):
    def test_weight_is_rate_times_net_weight(self) -> None:
        rows = rebuild_stat_rows(
            {(JINGLU, 2): 49.33, (JINGLU, 1): 47.06, ZHONGFEI_KEY: 3.33, (JINGLU, 3): 0.28},
            car_vol=3_250_050_755.0,
            net_weight=33.66,
            template={
                "flow_code": "flow-1",
                "task_code": "task-1",
                "car_number": "苏GC7733",
                "station_number": 4,
            },
        )
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertAlmostEqual(
                float(row["steel_weight"]), float(row["steel_rate"]) * 33.66, places=6
            )
        self.assertEqual(rows[0]["steel_type"], JINGLU)
        self.assertEqual(rows[0]["steel_level"], 2)

    def test_volume_sums_back_to_car(self) -> None:
        car_vol = 1_000_000.0
        rows = rebuild_stat_rows(
            {(JINGLU, 1): 60.0, (JINGLU, 2): 40.0},
            car_vol=car_vol,
            net_weight=50.0,
            template={},
        )
        self.assertAlmostEqual(sum(float(r["steel_volume"]) for r in rows), car_vol, delta=1.0)

    def test_rate_is_four_decimals(self) -> None:
        rows = rebuild_stat_rows(
            {(JINGLU, 1): 49.33350, (JINGLU, 2): 50.66650},
            car_vol=100.0,
            net_weight=10.0,
            template={},
        )
        for row in rows:
            self.assertEqual(row["steel_rate"], round(float(row["steel_rate"]), 4))


if __name__ == "__main__":
    unittest.main()
