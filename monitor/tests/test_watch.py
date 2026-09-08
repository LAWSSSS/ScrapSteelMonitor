from __future__ import annotations

import sys
import time
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pymysql.err import OperationalError

from monitor.store import is_watch_mode, load_ui_mode, save_ui_mode
from monitor.watch import WatchController


class UiModeTests(unittest.TestCase):
    def test_save_load(self) -> None:
        save_ui_mode("review-history", "2026-08-23")
        data = load_ui_mode()
        self.assertEqual(data["mode"], "review-history")
        self.assertEqual(data["date"], "2026-08-23")
        save_ui_mode("choose", "")
        self.assertEqual(load_ui_mode()["mode"], "choose")

    def test_watch_auto_mode_roundtrip(self) -> None:
        save_ui_mode("watch-today-auto", "2026-08-28")
        data = load_ui_mode()
        self.assertEqual(data["mode"], "watch-today-auto")
        self.assertTrue(is_watch_mode(data["mode"]))
        self.assertFalse(is_watch_mode("review-history"))
        save_ui_mode("choose", "")


class WatchControllerTests(unittest.TestCase):
    def test_start_scans_once_then_stop(self) -> None:
        calls: list[date] = []

        def fake_scan(day: date, **kwargs: object) -> dict:
            calls.append(day)
            return {"pending": 1, "skipped": 0}

        ctl = WatchController(
            scan_fn=fake_scan,
            today_fn=lambda: date(2026, 8, 25),
            interval_sec=60,
        )
        snap = ctl.start()
        self.assertTrue(snap["watching"])
        self._wait_scanned(ctl)
        snap = ctl.snapshot()
        self.assertEqual(calls, [date(2026, 8, 25)])
        self.assertEqual(snap["last_pending"], 1)
        self.assertGreaterEqual(snap["interval_sec"], 30)
        stopped = ctl.stop()
        self.assertFalse(stopped["watching"])
        self.assertEqual(len(calls), 1)

    def _wait_scanned(self, ctl: WatchController, timeout: float = 2.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            snap = ctl.snapshot()
            if snap["last_scan_at"] or snap["last_error"]:
                return
            time.sleep(0.01)
        self.fail("watch scan did not finish")

    def test_start_twice_does_not_double_scan(self) -> None:
        calls = []

        def fake_scan(day: date, **kwargs: object) -> dict:
            calls.append(day)
            return {"pending": 0}

        ctl = WatchController(
            scan_fn=fake_scan,
            today_fn=lambda: date(2026, 8, 25),
            interval_sec=60,
        )
        ctl.start()
        ctl.start()
        self._wait_scanned(ctl)
        self.assertEqual(len(calls), 1)
        ctl.stop()

    def test_retries_2013_then_ok(self) -> None:
        calls = {"n": 0}

        def flaky(day: date, **kwargs: object) -> dict:
            calls["n"] += 1
            if calls["n"] == 1:
                raise OperationalError(2013, "Lost connection")
            return {"pending": 2, "skipped": 0}

        ctl = WatchController(
            scan_fn=flaky,
            today_fn=lambda: date(2026, 8, 27),
            interval_sec=60,
        )
        ctl._retry_sleep_sec = 0
        err = ctl._run_once()
        self.assertEqual(err, "")
        self.assertEqual(calls["n"], 2)
        self.assertEqual(ctl.snapshot()["last_pending"], 2)

    def test_non_2013_does_not_retry(self) -> None:
        calls = {"n": 0}

        def boom(day: date, **kwargs: object) -> dict:
            calls["n"] += 1
            raise RuntimeError("not a db blip")

        ctl = WatchController(
            scan_fn=boom,
            today_fn=lambda: date(2026, 8, 27),
            interval_sec=60,
        )
        ctl._retry_sleep_sec = 0
        err = ctl._run_once()
        self.assertIn("not a db blip", err)
        self.assertEqual(calls["n"], 1)

    def test_set_auto_without_restart(self) -> None:
        ctl = WatchController(
            scan_fn=lambda day, **kwargs: {"pending": 0},
            today_fn=lambda: date(2026, 8, 28),
            interval_sec=60,
        )
        ctl.set_auto(False)
        self.assertFalse(ctl.snapshot()["auto"])
        ctl.set_auto(True)
        self.assertTrue(ctl.snapshot()["auto"])
        ctl.set_auto(False)
        self.assertFalse(ctl.snapshot()["auto"])

    def test_auto_scan_approves_pending(self) -> None:
        applied: list[tuple[str, str]] = []

        def fake_scan(day: date, **kwargs: object) -> dict:
            return {"pending": 1, "skipped": 0}

        def fake_apply(ticket: str, day: str) -> dict:
            applied.append((ticket, day))
            return {"ok": True, "insert_ids": [1]}

        def fake_pending(day: str, kind: str = "volume") -> list[dict]:
            return [
                {
                    "weight_bill_no": "JC1",
                    "biz_date": "2026-08-28",
                    "plate": "桂A",
                    "kind": "volume",
                    "auto_ok": True,
                }
            ]

        ctl = WatchController(
            scan_fn=fake_scan,
            today_fn=lambda: date(2026, 8, 28),
            apply_fn=fake_apply,
            pending_fn=fake_pending,
            interval_sec=60,
        )
        ctl._retry_sleep_sec = 0
        ctl.set_auto(True)
        err = ctl._run_once()
        self.assertEqual(err, "")
        self.assertEqual(applied, [("JC1", "2026-08-28")])
        self.assertEqual(ctl.snapshot()["last_auto_applied"], 1)
        self.assertEqual(ctl.snapshot()["last_pending"], 0)

    def test_manual_scan_does_not_approve(self) -> None:
        applied: list[tuple[str, str]] = []

        def fake_apply(ticket: str, day: str) -> dict:
            applied.append((ticket, day))
            return {"ok": True}

        ctl = WatchController(
            scan_fn=lambda day, **kwargs: {"pending": 1, "skipped": 0},
            today_fn=lambda: date(2026, 8, 28),
            apply_fn=fake_apply,
            pending_fn=lambda day, kind="volume": [
                {"weight_bill_no": "JC1", "biz_date": "2026-08-28", "kind": "volume"}
            ],
            interval_sec=60,
        )
        ctl._retry_sleep_sec = 0
        ctl.set_auto(False)
        ctl._run_once()
        self.assertEqual(applied, [])
        self.assertEqual(ctl.snapshot()["last_pending"], 1)

    def test_auto_skips_cards_not_auto_ok(self) -> None:
        applied: list[tuple[str, str]] = []

        def fake_apply(ticket: str, day: str) -> dict:
            applied.append((ticket, day))
            return {"ok": True}

        ctl = WatchController(
            scan_fn=lambda day, **kwargs: {"pending": 2, "skipped": 0},
            today_fn=lambda: date(2026, 8, 28),
            apply_fn=fake_apply,
            pending_fn=lambda day, kind="volume": [
                {
                    "weight_bill_no": "ONE",
                    "biz_date": "2026-08-28",
                    "kind": "volume",
                    "auto_ok": False,
                },
                {
                    "weight_bill_no": "OK",
                    "biz_date": "2026-08-28",
                    "kind": "volume",
                    "auto_ok": True,
                },
            ],
            interval_sec=60,
        )
        ctl._retry_sleep_sec = 0
        ctl.set_auto(True)
        ctl._run_once()
        self.assertEqual(applied, [("OK", "2026-08-28")])
        self.assertEqual(ctl.snapshot()["last_auto_applied"], 1)
        self.assertEqual(ctl.snapshot()["last_pending"], 1)

    def test_watch_scan_asks_for_three_manuals(self) -> None:
        seen: list[int] = []

        def fake_scan(day: date, **kwargs: object) -> dict:
            seen.append(int(kwargs.get("max_manuals") or 0))
            return {"pending": 0, "skipped": 0}

        ctl = WatchController(
            scan_fn=fake_scan,
            today_fn=lambda: date(2026, 8, 28),
            interval_sec=60,
        )
        ctl._retry_sleep_sec = 0
        ctl._run_once()
        self.assertEqual(seen, [3])


if __name__ == "__main__":
    unittest.main()

