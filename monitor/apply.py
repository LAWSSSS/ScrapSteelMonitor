"""Approve a pending card: snapshot then guarded UPDATE/INSERT."""

from __future__ import annotations

from typing import Any

from monitor.backup import (
    insert_columns,
    update_rollback_with_inserts,
    write_deduct_snapshot,
    write_snapshot,
)
from monitor.config import KIND_DEDUCT, KIND_VOLUME, day_bounds, parse_day
from monitor.correct import apply_hunliao_ids, rebuild_inserts
from monitor.deduct import plan_deduct_scale, reread_deduct
from monitor.db import execute_guarded, load_check_rows, load_task_via_order_car
from monitor.sql_guard import assert_safe_sql
from monitor.store import (
    append_log,
    load_pending,
    mark_processed,
    remove_pending,
    save_pending,
    unmark_processed,
)


def _window(card: dict[str, Any]) -> tuple[Any, Any]:
    return day_bounds(parse_day(str(card["biz_date"])))


def _volume_statements(card: dict[str, Any]) -> list[tuple[str, tuple[Any, ...]]]:
    updates = {int(k): float(v) for k, v in (card.get("volume_updates") or {}).items()}
    if not updates:
        return []
    task = card["task_code"]
    car = card["car_number"]
    start, end = _window(card)
    ids = list(updates.keys())
    case_sql = " ".join(["WHEN %s THEN %s"] * len(ids))
    params: list[Any] = []
    for i in ids:
        params.extend([i, updates[i]])
    id_ph = ",".join(["%s"] * len(ids))
    sql = (
        "UPDATE slzj.intelli_task_check SET total_volume = CASE id "
        f"{case_sql} ELSE total_volume END "
        "WHERE task_code=%s AND car_number=%s "
        "AND create_time>=%s AND create_time<%s "
        f"AND id IN ({id_ph})"
    )
    params.extend([task, car, start, end, *ids])
    assert_safe_sql(sql)
    return [(sql, tuple(params))]


def _hunliao_statements(card: dict[str, Any]) -> list[tuple[str, tuple[Any, ...]]]:
    ids = [int(x) for x in (card.get("hunliao_ids") or [])]
    if not ids:
        return []
    task = card["task_code"]
    car = card["car_number"]
    start, end = _window(card)
    ph = ",".join(["%s"] * len(ids))
    sql = (
        "UPDATE slzj.intelli_task_check SET is_hunliao=1 "
        "WHERE task_code=%s AND car_number=%s "
        "AND create_time>=%s AND create_time<%s "
        "AND is_hunliao IS NOT NULL "
        f"AND id IN ({ph})"
    )
    params = (task, car, start, end, *ids)
    assert_safe_sql(sql)
    return [(sql, params)]


def _insert_statements(card: dict[str, Any]) -> list[tuple[str, tuple[Any, ...]]]:
    cols = insert_columns()
    ph = ",".join(["%s"] * len(cols))
    sql = f"INSERT INTO slzj.intelli_task_check ({','.join(cols)}) VALUES ({ph})"
    assert_safe_sql(sql)
    out: list[tuple[str, tuple[Any, ...]]] = []
    for row in card.get("inserts") or []:
        out.append((sql, tuple(row.get(c) for c in cols)))
    return out


def approve(weight_bill_no: str, day: str) -> dict[str, Any]:
    card = load_pending(weight_bill_no, day)
    if card is None:
        raise FileNotFoundError(f"没有待确认记录 {weight_bill_no} {day}")
    if card.get("in_band"):
        raise ValueError("该料型展示占比已在±10%带内，无需再次应用")
    biz = parse_day(day)
    rows = load_check_rows(
        str(card["task_code"]),
        str(card["car_number"]),
        biz,
        include_images=True,
    )
    if not rows:
        raise RuntimeError("应用前重新读取明细为空，已中止")
    inserts = rebuild_inserts(card.get("inserts") or [], rows)
    flip_uuids = [str(x) for x in (card.get("flip_uuids") or [])]
    if not flip_uuids and card.get("hunliao_ids"):
        wanted_ids = {int(x) for x in card["hunliao_ids"]}
        flip_uuids = sorted({str(r["uuid"]) for r in rows if int(r["id"]) in wanted_ids})
    hunliao_ids = apply_hunliao_ids(rows, flip_uuids)
    card_exec = {**card, "inserts": inserts, "hunliao_ids": hunliao_ids}
    paths = write_snapshot(card_exec, rows)
    stmts = (
        _volume_statements(card_exec)
        + _hunliao_statements(card_exec)
        + _insert_statements(card_exec)
    )
    if not stmts:
        raise RuntimeError("待确认记录没有可执行的调整")
    insert_ids = execute_guarded(stmts)
    update_rollback_with_inserts(paths["rollback"], card_exec, rows, insert_ids)
    mark_processed(
        weight_bill_no,
        day,
        {
            "task_code": card_exec["task_code"],
            "car_number": card_exec["car_number"],
            "target": card_exec.get("target"),
            "avg": card_exec.get("avg"),
            "delta": card_exec.get("delta"),
            "humans_used": card_exec.get("humans_used"),
            "backup": paths,
            "insert_ids": insert_ids,
        },
    )
    remove_pending(weight_bill_no, day)
    append_log(
        "approved",
        {
            "weight_bill_no": weight_bill_no,
            "biz_date": day,
            "plate": card_exec.get("plate"),
            "insert_ids": insert_ids,
            "backup": paths,
        },
    )
    return {"ok": True, "backup": paths, "insert_ids": insert_ids, "card": card_exec}


def reject(weight_bill_no: str, day: str, kind: str = KIND_VOLUME) -> dict[str, Any]:
    if kind == KIND_DEDUCT:
        return reject_deduct(weight_bill_no, day)
    card = load_pending(weight_bill_no, day)
    if card is None:
        raise FileNotFoundError(f"没有待确认记录 {weight_bill_no} {day}")
    remove_pending(weight_bill_no, day)
    append_log(
        "rejected",
        {"weight_bill_no": weight_bill_no, "biz_date": day, "plate": card.get("plate")},
    )
    return {"ok": True, "rejected": True}


def _deduct_statements(card: dict[str, Any], updates: dict[int, float]) -> list[tuple[str, tuple[Any, ...]]]:
    if not updates:
        return []
    task = card["task_code"]
    car = card["car_number"]
    start, end = _window(card)
    ids = list(updates.keys())
    case_sql = " ".join(["WHEN %s THEN %s"] * len(ids))
    params: list[Any] = []
    for i in ids:
        params.extend([i, updates[i]])
    id_ph = ",".join(["%s"] * len(ids))
    sql = (
        "UPDATE slzj.intelli_task_check SET deduct_weight = CASE id "
        f"{case_sql} ELSE deduct_weight END "
        "WHERE task_code=%s AND car_number=%s "
        "AND create_time>=%s AND create_time<%s "
        "AND deduct_weight IS NOT NULL AND deduct_weight > 0 "
        f"AND id IN ({id_ph})"
    )
    params.extend([task, car, start, end, *ids])
    assert_safe_sql(sql)
    return [(sql, tuple(params))]


def _deduct_preprocess(card: dict[str, Any]) -> float:
    if card.get("preprocess_add_value") is not None:
        return float(card.get("preprocess_add_value") or 0)
    biz = parse_day(str(card["biz_date"]))
    task = load_task_via_order_car(str(card["weight_bill_no"]), biz)
    if task and task.get("preprocess_add_value") is not None:
        return float(task["preprocess_add_value"] or 0)
    return 0.0


def _deduct_live(card: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    biz = parse_day(str(card["biz_date"]))
    rows = load_check_rows(str(card["task_code"]), str(card["car_number"]), biz)
    if not rows:
        raise RuntimeError("回读明细为空，已中止")
    human_kg = float(card["human_kg"])
    net = float(card["net_weight"])
    pre = _deduct_preprocess(card)
    return rows, reread_deduct(rows, net, human_kg, pre)


def approve_deduct(weight_bill_no: str, day: str, target_kg: float) -> dict[str, Any]:
    card = load_pending(weight_bill_no, day, KIND_DEDUCT)
    if card is None:
        raise FileNotFoundError(f"没有扣重待确认记录 {weight_bill_no} {day}")
    rows, before = _deduct_live(card)
    plan = plan_deduct_scale(
        rows,
        before["ai_kg"],
        float(target_kg),
        preprocess_add_value=float(before.get("preprocess_add_value") or 0),
        net_weight=float(card["net_weight"]),
    )
    if not plan.ok:
        raise RuntimeError(plan.reason)
    original = card.get("original_backup")
    if not original:
        original = write_deduct_snapshot(card, rows)
    stmts = _deduct_statements(card, plan.updates)
    execute_guarded(stmts)
    _, after = _deduct_live(card)
    card.update(
        {
            "kind": KIND_DEDUCT,
            "written": True,
            "original_backup": original,
            "last_target_kg": round(float(target_kg), 2),
            "ai_kg_before": before["ai_kg"],
            "ai_kg": after["ai_kg"],
            "original_kg": after.get("original_kg"),
            "base_kg": after.get("base_kg"),
            "pre_kg": after.get("pre_kg"),
            "raw_kg": after.get("raw_kg"),
            "preprocess_add_value": after.get("preprocess_add_value"),
            "preprocess_op": after.get("preprocess_op"),
            "interval": after.get("interval"),
            "gap_kg": after["gap_kg"],
            "in_band": after["in_band"],
            "deduct_rows": after["deduct_rows"],
            "factor": round(plan.factor, 6),
        }
    )
    if after["in_band"]:
        mark_processed(
            weight_bill_no,
            day,
            {
                "kind": KIND_DEDUCT,
                "task_code": card["task_code"],
                "car_number": card["car_number"],
                "target_kg": target_kg,
                "ai_kg": after["ai_kg"],
                "gap_kg": after["gap_kg"],
                "humans_used": card.get("humans_used"),
                "backup": original,
            },
            KIND_DEDUCT,
        )
        remove_pending(weight_bill_no, day, KIND_DEDUCT)
        status = "done"
    else:
        save_pending(card)
        status = "pending"
    append_log(
        "deduct_approved",
        {
            "weight_bill_no": weight_bill_no,
            "biz_date": day,
            "plate": card.get("plate"),
            "target_kg": target_kg,
            "ai_kg": after["ai_kg"],
            "gap_kg": after["gap_kg"],
            "in_band": after["in_band"],
        },
    )
    return {
        "ok": True,
        "status": status,
        "in_band": after["in_band"],
        "card": card,
        "before": before,
        "after": after,
        "backup": original,
    }


def refresh_deduct(weight_bill_no: str, day: str) -> dict[str, Any]:
    card = load_pending(weight_bill_no, day, KIND_DEDUCT)
    if card is None:
        raise FileNotFoundError(f"没有扣重待确认记录 {weight_bill_no} {day}")
    _rows, after = _deduct_live(card)
    card.update(
        {
            "ai_kg": after["ai_kg"],
            "original_kg": after.get("original_kg"),
            "base_kg": after.get("base_kg"),
            "pre_kg": after.get("pre_kg"),
            "raw_kg": after.get("raw_kg"),
            "preprocess_add_value": after.get("preprocess_add_value"),
            "preprocess_op": after.get("preprocess_op"),
            "interval": after.get("interval"),
            "gap_kg": after["gap_kg"],
            "in_band": after["in_band"],
            "deduct_rows": after["deduct_rows"],
        }
    )
    if after["in_band"] and card.get("written"):
        mark_processed(
            weight_bill_no,
            day,
            {
                "kind": KIND_DEDUCT,
                "task_code": card.get("task_code"),
                "car_number": card.get("car_number"),
                "ai_kg": after["ai_kg"],
                "gap_kg": after["gap_kg"],
                "backup": card.get("original_backup"),
            },
            KIND_DEDUCT,
        )
        remove_pending(weight_bill_no, day, KIND_DEDUCT)
        status = "done"
    else:
        save_pending(card)
        status = "pending"
    return {"ok": True, "status": status, "in_band": after["in_band"], "card": card, "after": after}


def reject_deduct(weight_bill_no: str, day: str) -> dict[str, Any]:
    from monitor.restore import restore_deduct

    card = load_pending(weight_bill_no, day, KIND_DEDUCT)
    if card is None:
        raise FileNotFoundError(f"没有扣重待确认记录 {weight_bill_no} {day}")
    restored = False
    if card.get("written") and card.get("original_backup"):
        restore_deduct(ticket=weight_bill_no, day=day, file=card["original_backup"].get("snapshot"))
        restored = True
    remove_pending(weight_bill_no, day, KIND_DEDUCT)
    unmark_processed(weight_bill_no, day, KIND_DEDUCT)
    append_log(
        "deduct_rejected",
        {
            "weight_bill_no": weight_bill_no,
            "biz_date": day,
            "plate": card.get("plate"),
            "restored": restored,
        },
    )
    return {"ok": True, "rejected": True, "restored": restored}
