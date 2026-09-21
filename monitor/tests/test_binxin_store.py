"""镔鑫本地状态、入队、看板取数的单测。不连库。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from monitor.binxin import backup, dashboard, guard, scan, store
from monitor.binxin.materials import JINGLU

DAY = "2026-09-19"


def _vehicle(car: str, flow: str, *, with_plan: bool) -> scan.Vehicle:
    v = scan.Vehicle(
        car_number=car,
        order_code=f"wb-{car}",
        flow_code=flow,
        checked_at=f"{DAY} 10:13:06",
        grabs=20,
        task_code="task-1",
        net_weight=33.66,
        station_number=4,
    )
    v.human_pcts = {(JINGLU, 2): 95.0}
    v.ai_raw_pcts = {(JINGLU, 2): 49.33, (JINGLU, 1): 47.06}
    v.ai_display_pcts = {(JINGLU, 2): 79.86, (JINGLU, 1): 20.14}
    v.human_main = (JINGLU, 2)
    v.ai_main = (JINGLU, 2)
    v.diff_rate_pct = 15.9
    if with_plan:
        v.plan = scan.Plan(
            target_key=(JINGLU, 2),
            human_pct=95.0,
            current_raw_pct=49.33,
            target_raw_pct=64.5,
            target_display_pct=85.8,
            lead_floor_pct=50.2,
            method=scan.PLAN_SCALE,
        )
    else:
        v.ok = True
    return v


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for module in (store, backup, guard):
            p = mock.patch.object(module, "STATE_ROOT", self.root)
            p.start()
            self.addCleanup(p.stop)
            n = mock.patch.object(module, "ensure_dirs", lambda: None)
            n.start()
            self.addCleanup(n.stop)


class PendingTests(StoreTestCase):
    def _card(self, flow: str = "f1", day: str = DAY) -> dict:
        return {"flow_code": flow, "biz_date": day, "car_number": "苏GC7733"}

    def test_save_then_load(self) -> None:
        store.save_pending(self._card())
        self.assertEqual(store.load_pending("f1", DAY)["car_number"], "苏GC7733")

    def test_load_missing_returns_none(self) -> None:
        self.assertIsNone(store.load_pending("nope", DAY))

    def test_remove(self) -> None:
        store.save_pending(self._card())
        store.remove_pending("f1", DAY)
        self.assertIsNone(store.load_pending("f1", DAY))

    def test_remove_missing_is_quiet(self) -> None:
        store.remove_pending("nope", DAY)

    def test_list_filters_by_day(self) -> None:
        store.save_pending(self._card("f1", DAY))
        store.save_pending(self._card("f2", "2026-09-18"))
        self.assertEqual(len(store.list_pending(DAY)), 1)
        self.assertEqual(len(store.list_pending()), 2)

    def test_no_temp_file_left_behind(self) -> None:
        store.save_pending(self._card())
        self.assertEqual(list(store.pending_dir().glob("*.tmp")), [])

    def test_corrupt_pending_file_is_skipped(self) -> None:
        store.save_pending(self._card())
        bad = store.pending_dir() / f"{DAY}_broken.json"
        bad.write_text("{ not json", encoding="utf-8")
        self.assertEqual(len(store.list_pending(DAY)), 1)

    def test_flow_with_slash_is_safe_as_filename(self) -> None:
        store.save_pending(self._card("a/b"))
        self.assertIsNotNone(store.load_pending("a/b", DAY))


class ProcessedTests(StoreTestCase):
    def test_mark_and_check(self) -> None:
        store.mark_processed("f1", DAY, {"car_number": "苏A1"})
        self.assertTrue(store.is_processed("f1", DAY))
        self.assertFalse(store.is_processed("f1", "2026-09-18"))

    def test_mark_adds_timestamp(self) -> None:
        store.mark_processed("f1", DAY, {})
        self.assertIn("ts", store.load_processed()[store.ticket_key("f1", DAY)])

    def test_unmark(self) -> None:
        store.mark_processed("f1", DAY, {})
        store.unmark_processed("f1", DAY)
        self.assertFalse(store.is_processed("f1", DAY))

    def test_second_mark_overwrites(self) -> None:
        store.mark_processed("f1", DAY, {"n": 1})
        store.mark_processed("f1", DAY, {"n": 2})
        self.assertEqual(store.load_processed()[store.ticket_key("f1", DAY)]["n"], 2)


class LogTests(StoreTestCase):
    def test_append_and_read(self) -> None:
        store.append_log("approved", {"car_number": "苏A1"})
        store.append_log("rejected", {"car_number": "苏A2"})
        logs = store.recent_logs()
        self.assertEqual([x["event"] for x in logs], ["approved", "rejected"])

    def test_limit_keeps_latest(self) -> None:
        for i in range(30):
            store.append_log("scanned", {"n": i})
        logs = store.recent_logs(5)
        self.assertEqual(len(logs), 5)
        self.assertEqual(logs[-1]["n"], 29)


class QueueTests(StoreTestCase):
    def _run(self, vehicles: list[scan.Vehicle]) -> dict:
        with mock.patch.object(scan, "scan_day", return_value=vehicles):
            return scan.queue_day(date(2026, 9, 19))

    def test_only_planned_cars_are_queued(self) -> None:
        summary = self._run([_vehicle("苏A1", "f1", with_plan=True),
                             _vehicle("苏A2", "f2", with_plan=False)])
        self.assertEqual(summary["queued"], ["苏A1"])
        self.assertEqual(len(store.list_pending(DAY)), 1)

    def test_already_processed_car_is_not_requeued(self) -> None:
        store.mark_processed("f1", DAY, {})
        summary = self._run([_vehicle("苏A1", "f1", with_plan=True)])
        self.assertEqual(summary["queued"], [])
        self.assertIsNone(store.load_pending("f1", DAY))

    def test_requeue_overwrites_stale_target(self) -> None:
        """第二次扫描的目标占比要覆盖掉旧的，不能拿旧目标去批准。"""
        v = _vehicle("苏A1", "f1", with_plan=True)
        self._run([v])
        v.plan.target_raw_pct = 70.25
        self._run([v])
        self.assertAlmostEqual(store.load_pending("f1", DAY)["target_raw_pct"], 70.25)

    def test_card_carries_everything_approve_needs(self) -> None:
        self._run([_vehicle("苏A1", "f1", with_plan=True)])
        card = store.load_pending("f1", DAY)
        for field in ("flow_code", "task_code", "order_code", "biz_date", "net_weight",
                      "target_key", "target_raw_pct", "human_main_pct"):
            self.assertIn(field, card, field)
        self.assertEqual(card["target_key"], [JINGLU, 2])

    def test_summary_is_saved(self) -> None:
        self._run([_vehicle("苏A1", "f1", with_plan=True)])
        saved = store.load_scan_summary(DAY)
        self.assertEqual(saved["total"], 1)
        self.assertEqual(saved["counts"]["待修正"], 1)

    def test_card_is_json_round_trippable(self) -> None:
        """待审单要能原样落盘再读回来，target_key 不能是元组。"""
        card = scan.pending_card(_vehicle("苏A1", "f1", with_plan=True), date(2026, 9, 19))
        self.assertEqual(json.loads(json.dumps(card))["target_key"], [JINGLU, 2])


class OverviewTests(StoreTestCase):
    def test_empty_day_says_not_scanned(self) -> None:
        data = dashboard.overview(DAY)
        self.assertIn("还没扫过", data["notice"])
        self.assertEqual(data["vehicles"], [])

    def test_lock_state_is_included(self) -> None:
        self.assertIn("allowed", dashboard.overview(DAY)["lock"])
        self.assertFalse(dashboard.overview(DAY)["lock"]["allowed"])

    def test_pending_is_keyed_by_flow(self) -> None:
        with mock.patch.object(
            scan, "scan_day", return_value=[_vehicle("苏A1", "f1", with_plan=True)]
        ):
            scan.queue_day(date(2026, 9, 19))
        data = dashboard.overview(DAY)
        self.assertIn("f1", data["pending"])
        self.assertEqual(data["counts"]["待修正"], 1)

    def test_processed_only_counts_that_day(self) -> None:
        store.mark_processed("f1", DAY, {})
        store.mark_processed("f2", "2026-09-18", {})
        self.assertEqual(list(dashboard.overview(DAY)["processed"]), ["f1"])

    def test_overview_never_connects_to_db(self) -> None:
        """看板刷新不能连库，否则 VPN 一断整个面板就打不开。"""
        with mock.patch("psycopg2.connect", side_effect=AssertionError("不该连库")):
            dashboard.overview(DAY)


if __name__ == "__main__":
    unittest.main()
