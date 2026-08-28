"""Read-only probe: where a day scan dies and which SQL is heavy."""

from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monitor.config import day_bounds, parse_day
from monitor.db import (
    CHECK_COLUMNS,
    connect,
    count_other_date_rows,
    fetch_all,
    load_check_rows,
    load_manual_checks,
    load_manual_details,
    load_task_by_flow,
)


def _step(name: str, fn):
    t0 = time.perf_counter()
    try:
        result = fn()
        ms = (time.perf_counter() - t0) * 1000
        extra = ""
        if isinstance(result, list):
            extra = f" n={len(result)}"
        elif isinstance(result, int):
            extra = f" n={result}"
        print(f"OK  {name:40s} {ms:8.0f} ms{extra}")
        return result
    except Exception as exc:
        ms = (time.perf_counter() - t0) * 1000
        print(f"FAIL {name:40s} {ms:8.0f} ms  {type(exc).__name__}: {exc}")
        raise


def main() -> None:
    day = parse_day(sys.argv[1] if len(sys.argv) > 1 else "2026-08-24")
    print(f"probe day={day.isoformat()}")
    _step("SELECT 1", lambda: fetch_all("SELECT 1 AS ok"))
    checks = _step("load_manual_checks", lambda: load_manual_checks(day))
    ids = [int(r["id"]) for r in checks]
    _step("load_manual_details", lambda: load_manual_details(ids))
    start, end = day_bounds(day)

    def _meta(sql: str, params: tuple = ()):
        with connect(writable=False) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return list(cur.fetchall())

    explain = _step(
        "EXPLAIN intelli_task_check date window",
        lambda: _meta(
            """
            EXPLAIN SELECT id FROM intelli_task_check
            WHERE create_time >= %s AND create_time < %s
            LIMIT 1
            """,
            (start, end),
        ),
    )
    print("  explain:", explain)
    indexes = _step(
        "SHOW INDEX intelli_task_check",
        lambda: _meta("SHOW INDEX FROM intelli_task_check"),
    )
    print("  indexes:", sorted({r.get("Key_name") for r in indexes}))
    cols = _step(
        "image-like columns",
        lambda: fetch_all(
            """
            SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'intelli_task_check'
              AND COLUMN_NAME IN ('images','origin_image_url','deduct_weight_url')
            """
        ),
    )
    print("  cols:", cols)
    print("  CHECK_COLUMNS count:", len(CHECK_COLUMNS))

    sampled = 0
    for row in checks:
        flow = str(row.get("flow_code") or "")
        if not flow:
            continue
        task = _step(
            f"task flow {flow[:12]}",
            lambda f=flow: load_task_by_flow(f, day),
        )
        if not task:
            continue
        plate = str(task.get("car_number") or row.get("vehicle_no") or "")
        code = str(task["task_code"])
        rows = _step(
            f"check_rows {plate}",
            lambda c=code, p=plate: load_check_rows(c, p, day),
        )
        _step(
            f"other_date_count {plate}",
            lambda p=plate, c=code: count_other_date_rows(p, c, day),
        )
        sampled += 1
        if sampled >= 3:
            break
    print("done")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(1)
