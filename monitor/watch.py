"""In-process today-watch loop for the dashboard. Never auto-writes the business DB."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable

from monitor.config import TZ, WATCH_INTERVAL_SEC, today_cn
from monitor.db import is_retryable_db_error
from monitor.notify import notify
from monitor.scan import scan_day
from monitor.store import write_heartbeat

logger = logging.getLogger(__name__)

ScanFn = Callable[..., dict[str, Any]]


class WatchController:
    def __init__(
        self,
        *,
        scan_fn: ScanFn | None = None,
        today_fn: Callable[[], Any] | None = None,
        interval_sec: int = WATCH_INTERVAL_SEC,
    ) -> None:
        self._scan_fn = scan_fn or self._default_scan
        self._today_fn = today_fn or today_cn
        self._interval = max(30, int(interval_sec))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._scanning = False
        self._last_error = ""
        self._last_scan_at = ""
        self._next_scan_at = ""
        self._last_pending = 0
        self._last_summary: dict[str, Any] | None = None
        self._retry_sleep_sec = 3.0

    def _default_scan(self, day: Any, **kwargs: Any) -> dict[str, Any]:
        return scan_day(day, auto=False, **kwargs)

    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_unlocked()

    def _snapshot_unlocked(self) -> dict[str, Any]:
        return {
            "watching": self.is_running(),
            "scanning": self._scanning,
            "interval_sec": self._interval,
            "last_error": self._last_error,
            "last_scan_at": self._last_scan_at,
            "next_scan_at": self._next_scan_at,
            "last_pending": self._last_pending,
            "last_summary": self._last_summary,
        }

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self.is_running():
                return self._snapshot_unlocked()
            self._stop.clear()
            self._scanning = True
            self._last_error = ""
            self._thread = threading.Thread(
                target=self._loop,
                name="watch-today",
                daemon=True,
            )
            self._thread.start()
        return self.snapshot()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        with self._lock:
            self._thread = None
            self._scanning = False
            self._next_scan_at = ""
            self._last_error = ""
        write_heartbeat({"mode": "dashboard"})
        return self.snapshot()

    def run_once_now(self) -> dict[str, Any]:
        error = self._run_once()
        snap = self.snapshot()
        if error:
            snap["error"] = error
        return snap

    def _loop(self) -> None:
        self._run_once()
        while not self._stop.wait(self._interval):
            self._run_once()

    def _run_once(self) -> str:
        day = self._today_fn()
        with self._lock:
            self._scanning = True
            self._last_error = ""
        try:
            prev_pending = self._last_pending
            try:
                summary = self._scan_fn(
                    day,
                    heartbeat_mode="watch-today",
                    notify_pending=False,
                )
            except Exception as first:
                if not is_retryable_db_error(first):
                    raise
                logger.warning("当天监控连库失败，%.1fs 后再试一次: %s", self._retry_sleep_sec, first)
                time.sleep(self._retry_sleep_sec)
                summary = self._scan_fn(
                    day,
                    heartbeat_mode="watch-today",
                    notify_pending=False,
                )
            pending = int(summary.get("pending") or 0)
            if pending > prev_pending:
                notify(
                    "检判待审核",
                    f"{day} 有 {pending} 辆车待审，打开 http://127.0.0.1:8765",
                )
            now = datetime.now(TZ)
            nxt = now + timedelta(seconds=self._interval)
            with self._lock:
                self._last_summary = summary
                self._last_pending = pending
                self._last_scan_at = now.isoformat(timespec="seconds")
                self._next_scan_at = nxt.isoformat(timespec="seconds")
                self._scanning = False
            write_heartbeat(
                {
                    "last_scan_date": day.isoformat(),
                    "mode": "watch-today",
                    "pending": pending,
                }
            )
            logger.info(
                "当天监控扫完 %s pending=%s skipped=%s",
                day,
                pending,
                summary.get("skipped"),
            )
            return ""
        except Exception as exc:
            logger.exception("当天监控扫描失败")
            with self._lock:
                self._last_error = str(exc)
                self._scanning = False
            return str(exc)


CONTROLLER = WatchController()
