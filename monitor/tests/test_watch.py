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

from monitor.store import load_ui_mode, save_ui_mode
from monitor.watch import WatchController


class UiModeTests(unittest.TestCase):
    def test_save_load(self) -> None:
        save_ui_mode("review-history", "2026-08-23")
        data = load_ui_mode()
        self.assertEqual(data["mode"], "review-history")
        self.assertEqual(data["date"], "2026-08-23")
        save_ui_mode("choose", "")
        self.assertEqual(load_ui_mode()["mode"], "choose")


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


if __name__ == "__main__":
    unittest.main()
