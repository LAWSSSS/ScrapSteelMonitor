"""Restore one car/day using UPDATE only."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from monitor.backup import latest_backup, load_snapshot
from monitor.config import KIND_DEDUCT, day_bounds, parse_day
from monitor.correct import pick_clone_source
from monitor.db import execute_guarded, load_check_rows
from monitor.sql_guard import assert_safe_sql
from monitor.store import append_log, load_processed, unmark_processed

logger = logging.getLogger(__name__)


def restore(*, ticket: str, day: str, file: str | None = None) -> dict[str, Any]:
    if file:
        snap = load_snapshot(file)
        rollback_hint = ""
    else:
        item = latest_backup(ticket, day, kind="volume")
        if item is None:
            raise FileNotFoundError(f"没有 {ticket} {day} 的备份")
        snap = load_snapshot(item["snapshot"])
        rollback_hint = str(item.get("rollback") or "")
    meta = snap.get("card_meta") or {}
    rows = snap.get("rows") or []
    task = meta.get("task_code")
    car = meta.get("car_number")
    biz = parse_day(str(meta.get("biz_date") or day))
    start, end = day_bounds(biz)
    if not rows or not task or not car:
        raise RuntimeError("快照缺少 task/car/行")
    ids = [int(r["id"]) for r in rows]
    case_v = " ".join(["WHEN %s THEN %s"] * len(ids))
    params_v: list[Any] = []
    for r in rows:
        params_v.extend([int(r["id"]), float(r.get("total_volume") or 0)])
    ph = ",".join(["%s"] * len(ids))
    sql_v = (
        "UPDATE slzj.intelli_task_check SET total_volume = CASE id "
        f"{case_v} ELSE total_volume END "
        "WHERE task_code=%s AND car_number=%s AND create_time>=%s AND create_time<%s "
        f"AND id IN ({ph})"
    )
    params_v.extend([task, car, start, end, *ids])
    assert_safe_sql(sql_v)
    case_h = " ".join(["WHEN %s THEN %s"] * len(ids))
    params_h: list[Any] = []
    for r in rows:
        params_h.extend([int(r["id"]), r.get("is_hunliao")])
    sql_h = (
        "UPDATE slzj.intelli_task_check SET is_hunliao = CASE id "
        f"{case_h} ELSE is_hunliao END "
        "WHERE task_code=%s AND car_number=%s AND create_time>=%s AND create_time<%s "
        f"AND id IN ({ph})"
    )
    params_h.extend([task, car, start, end, *ids])
    assert_safe_sql(sql_h)
    stmts = [(sql_v, tuple(params_v)), (sql_h, tuple(params_h))]
    extra_ids: list[int] = []
    rec = load_processed().get(f"{ticket}|{day}") or {}
    extra_ids = [int(x) for x in (rec.get("insert_ids") or [])]
    extra_ids.extend(int(x) for x in (snap.get("insert_ids") or []) if int(x) not in extra_ids)
    if extra_ids:
        live = load_check_rows(str(task), str(car), biz, include_images=True)
        stmts.extend(_sanitize_insert_statements(task, car, start, end, extra_ids, live))
    execute_guarded(stmts)
    unmark_processed(ticket, day)
    append_log("restored", {"weight_bill_no": ticket, "biz_date": day, "rollback": rollback_hint})
    return {"ok": True, "rows": len(ids), "zeroed_inserts": extra_ids}


def restore_deduct(*, ticket: str, day: str, file: str | None = None) -> dict[str, Any]:
    """Restore first-write deduct_weight snapshot. UPDATE only."""
    if file:
        snap = load_snapshot(file)
        rollback_hint = ""
    else:
        item = latest_backup(ticket, day, kind=KIND_DEDUCT)
        if item is None:
            raise FileNotFoundError(f"没有 {ticket} {day} 的扣重备份")
        snap = load_snapshot(item["snapshot"])
        rollback_hint = str(item.get("rollback") or "")
    meta = snap.get("card_meta") or {}
    rows = snap.get("rows") or []
    task = meta.get("task_code")
    car = meta.get("car_number")
    biz = parse_day(str(meta.get("biz_date") or day))
    start, end = day_bounds(biz)
    if not rows or not task or not car:
        raise RuntimeError("扣重快照缺少 task/car/行")
    ids = [int(r["id"]) for r in rows]
    case_sql = " ".join(["WHEN %s THEN %s"] * len(ids))
    params: list[Any] = []
    for r in rows:
        params.extend([int(r["id"]), r.get("deduct_weight")])
    ph = ",".join(["%s"] * len(ids))
    sql = (
        "UPDATE slzj.intelli_task_check SET deduct_weight = CASE id "
        f"{case_sql} ELSE deduct_weight END "
        "WHERE task_code=%s AND car_number=%s AND create_time>=%s AND create_time<%s "
        f"AND id IN ({ph})"
    )
    params.extend([task, car, start, end, *ids])
    assert_safe_sql(sql)
    execute_guarded([(sql, tuple(params))])
    unmark_processed(ticket, day, KIND_DEDUCT)
    append_log("deduct_restored", {"weight_bill_no": ticket, "biz_date": day, "rollback": rollback_hint})
    return {"ok": True, "rows": len(ids)}


def _sanitize_insert_statements(
    task: str,
    car: str,
    start: Any,
    end: Any,
    insert_ids: list[int],
    live_rows: list[dict[str, Any]],
) -> list[tuple[str, tuple[Any, ...]]]:
    """Zero leftover INSERT rows and make them look like normal satellite rows.

    Cannot DELETE. Clear origin/hunliao, copy images from the same uuid.
    """
    ph = ",".join(["%s"] * len(insert_ids))
    sql_clear = (
        "UPDATE slzj.intelli_task_check SET total_volume=0, "
        "origin_image_url=NULL, is_hunliao=NULL, "
        "deduct_weight=NULL, deduct_weight_url=NULL "
        "WHERE task_code=%s AND car_number=%s AND create_time>=%s AND create_time<%s "
        f"AND id IN ({ph})"
    )
    assert_safe_sql(sql_clear)
    stmts: list[tuple[str, tuple[Any, ...]]] = [
        (sql_clear, (task, car, start, end, *insert_ids)),
    ]
    by_uuid: dict[str, list[dict[str, Any]]] = {}
    for row in live_rows:
        by_uuid.setdefault(str(row["uuid"]), []).append(row)
    insert_set = set(insert_ids)
    cases: list[tuple[int, str]] = []
    for row in live_rows:
        rid = int(row["id"])
        if rid not in insert_set or row.get("images"):
            continue
        siblings = [
            item
            for item in (by_uuid.get(str(row["uuid"])) or [])
            if int(item["id"]) not in insert_set
        ]
        src = pick_clone_source(siblings)
        if src and src.get("images"):
            cases.append((rid, str(src["images"])))
    if not cases:
        return stmts
    case_sql = " ".join(["WHEN %s THEN %s"] * len(cases))
    params: list[Any] = []
    for rid, url in cases:
        params.extend([rid, url])
    ids = [rid for rid, _url in cases]
    ph2 = ",".join(["%s"] * len(ids))
    sql_img = (
        "UPDATE slzj.intelli_task_check SET images = CASE id "
        f"{case_sql} ELSE images END "
        "WHERE task_code=%s AND car_number=%s AND create_time>=%s AND create_time<%s "
        f"AND id IN ({ph2})"
    )
    assert_safe_sql(sql_img)
    params.extend([task, car, start, end, *ids])
    stmts.append((sql_img, tuple(params)))
    return stmts


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="按磅单号+日期恢复原结果")
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--file", default=None, help="指定快照 json")
    args = parser.parse_args()
    result = restore(ticket=args.ticket, day=args.date, file=args.file)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
