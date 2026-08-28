"""In-process today-watch loop for the dashboard.

Manual mode leaves cards pending. Auto mode approves volume cards after each scan.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable

from monitor.apply import approve
from monitor.config import KIND_VOLUME, TZ, WATCH_INTERVAL_SEC, today_cn
from monitor.db import is_retryable_db_error
from monitor.notify import notify
from monitor.scan import scan_day
from monitor.store import append_log, list_pending, write_heartbeat

logger = logging.getLogger(__name__)

ScanFn = Callable[..., dict[str, Any]]


class WatchController:
    def __init__(
        self,
        *,
        scan_fn: ScanFn | None = None,
        today_fn: Callable[[], Any] | None = None,
        apply_fn: Callable[[str, str], dict[str, Any]] | None = None,
        pending_fn: Callable[..., list[dict[str, Any]]] | None = None,
        interval_sec: int = WATCH_INTERVAL_SEC,
    ) -> None:
        self._scan_fn = scan_fn or self._default_scan
        self._today_fn = today_fn or today_cn
        self._apply_fn = apply_fn
        self._pending_fn = pending_fn
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
        self._auto = False
        self._last_auto_applied = 0

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
            "auto": self._auto,
            "last_auto_applied": self._last_auto_applied,
        }

    def set_auto(self, auto: bool) -> None:
        with self._lock:
            self._auto = bool(auto)

    def is_auto(self) -> bool:
        with self._lock:
            return self._auto

    def _day_text(self) -> str:
        day = self._today_fn()
        return day.isoformat() if hasattr(day, "isoformat") else str(day)

    def flush_auto(self) -> list[dict[str, Any]]:
        """Approve today's pending volume cards. Deduct cards are left alone."""
        day_s = self._day_text()
        pending_fn = self._pending_fn or (
            lambda d, kind=KIND_VOLUME: list_pending(d, kind=kind)
        )
        apply_fn = self._apply_fn or approve
        applied: list[dict[str, Any]] = []
        for card in pending_fn(day_s, KIND_VOLUME):
            if (card.get("kind") or KIND_VOLUME) != KIND_VOLUME:
                continue
            bill = str(card.get("weight_bill_no") or "")
            biz = str(card.get("biz_date") or day_s)
            try:
                result = apply_fn(bill, biz)
                rec = {
                    "weight_bill_no": bill,
                    "plate": card.get("plate"),
                    "insert_ids": result.get("insert_ids") if isinstance(result, dict) else [],
                }
                applied.append(rec)
                append_log("auto_approved", rec)
            except Exception as exc:
                logger.exception("自动批准失败 %s", bill)
                append_log(
                    "auto_approve_error",
                    {"weight_bill_no": bill, "plate": card.get("plate"), "error": str(exc)},
                )
        with self._lock:
            self._last_auto_applied = len(applied)
        return applied

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
            self._auto = False
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
            auto = self.is_auto()
            if auto and pending:
                applied = self.flush_auto()
                pending = max(0, pending - len(applied))
                if applied:
                    notify(
                        "检判已自动写库",
                        f"{day} 自动批准 {len(applied)} 辆。打开 http://127.0.0.1:8765 看流水",
                    )
            elif pending > prev_pending:
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
                    "mode": "watch-today-auto" if auto else "watch-today",
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
