"""Scan one business day and write pending / skip logs. No writes to business tables."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date
from typing import Any

from monitor.config import ALLOWED_MAIN_TYPES, BAND_PP, KIND_DEDUCT, KIND_VOLUME, steel_label
from monitor.correct import (
    apply_hunliao_ids,
    car_type_pcts,
    clamp_still_highest,
    display_main_pct,
    hunliao_stats,
    plan_hunliao_flip,
    pick_target_with_jitter,
    plan_volume,
)
from monitor.deduct import (
    calculate_deduct,
    deduct_gap_kg,
    deduct_row_count,
    human_deduct_kg,
    is_deduct_unqualified,
    raw_deduct_kg,
    task_preprocess,
)
from monitor.db import (
    count_rows_by_car,
    load_check_rows,
    load_day_check_rows,
    load_manual_checks,
    load_manual_details,
    load_order_net_weights,
    load_task_via_order_car,
    load_tasks_by_flows,
    session,
)
from monitor.notify import notify
from monitor.store import (
    append_log,
    is_processed,
    list_pending,
    load_pending,
    mark_processed,
    remove_pending,
    save_pending,
    write_heartbeat,
    write_scan_summary,
)

logger = logging.getLogger(__name__)


def _main_type(details: list[dict[str, Any]]) -> tuple[int | None, float]:
    if not details:
        return None, 0.0
    best = max(details, key=lambda r: float(r["steel_rate"] or 0))
    return int(best["steel_type"]), float(best["steel_rate"]) * 100.0


def select_manual_checks(
    group: list[dict[str, Any]],
    max_manuals: int,
) -> list[dict[str, Any]]:
    """First 2 by time, or first 3 when reviewing history. Never invent a 3rd."""
    ordered = sorted(group, key=lambda r: (str(r["create_time"]), int(r["id"])))
    if len(ordered) < 2:
        return []
    cap = 3 if int(max_manuals) >= 3 else 2
    return ordered[: min(cap, len(ordered))]


def humans_from_checks(
    checks: list[dict[str, Any]],
    details_by_check: dict[int, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    humans: list[dict[str, Any]] = []
    for row in checks:
        steel_type, pct = _main_type(details_by_check.get(int(row["id"]), []))
        humans.append(
            {
                "name": row.get("operator_name"),
                "steel_type": steel_type,
                "label": steel_label(steel_type) if steel_type else "",
                "pct": round(pct, 2),
            }
        )
    return humans


def judge_main_types(humans: list[dict[str, Any]]) -> str:
    """Empty string = ok. All used humans must share one allowed main type."""
    if len(humans) < 2:
        return "未满2条人工"
    types = [h.get("steel_type") for h in humans]
    if any(t is None for t in types) or len(set(types)) != 1:
        return "三人主料型不一致" if len(humans) >= 3 else "两人主料型不一致"
    main = int(types[0])
    if main not in ALLOWED_MAIN_TYPES:
        return f"主料型不是重废1/2（{steel_label(main)}）"
    return ""


def attach_human_fields(extra: dict[str, Any], humans: list[dict[str, Any]]) -> None:
    extra["humans_used"] = len(humans)
    extra["human1"] = humans[0]
    extra["human2"] = humans[1]
    if len(humans) >= 3:
        extra["human3"] = humans[2]
    extra["avg"] = round(sum(float(h["pct"]) for h in humans) / len(humans), 2)


_AI_KEYS = (
    "ai_pct",
    "display_pct",
    "boost_pp",
    "compare_pct",
    "avg",
    "gap",
    "target",
    "main_label",
    "task_code",
)


def merge_pending_ai(vehicle: dict[str, Any], card: dict[str, Any] | None) -> dict[str, Any]:
    """Copy AI/human numbers from a pending card onto a summary row."""
    out = dict(vehicle)
    if not card:
        return out
    for key in _AI_KEYS:
        if out.get(key) is None and card.get(key) is not None:
            out[key] = card[key]
    if not out.get("main_label"):
        human1 = out.get("human1") or card.get("human1") or {}
        if isinstance(human1, dict) and human1.get("label"):
            out["main_label"] = human1["label"]
    return out


def _time_window(rows: list[dict[str, Any]]) -> tuple[str, str]:
    times = [str(r.get("create_time") or "") for r in rows if r.get("create_time")]
    if not times:
        return "", ""
    return min(times), max(times)


def scan_day(
    day: date,
    *,
    auto: bool = False,
    heartbeat_mode: str = "once",
    notify_pending: bool = True,
    include_deduct: bool = False,
    max_manuals: int = 2,
) -> dict[str, Any]:
    day_s = day.isoformat()
    with session():
        return _scan_day_body(
            day,
            day_s,
            auto=auto,
            heartbeat_mode=heartbeat_mode,
            notify_pending=notify_pending,
            include_deduct=include_deduct,
            max_manuals=max_manuals,
        )


def _scan_day_body(
    day: date,
    day_s: str,
    *,
    auto: bool,
    heartbeat_mode: str,
    notify_pending: bool,
    include_deduct: bool,
    max_manuals: int,
) -> dict[str, Any]:
    checks = load_manual_checks(day)
    by_bill: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in checks:
        by_bill[str(row["weight_bill_no"])].append(row)

    detail_rows = load_manual_details([int(r["id"]) for r in checks])
    details_by_check: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in detail_rows:
        details_by_check[int(row["manual_check_id"])].append(row)

    vehicles: list[dict[str, Any]] = []
    pending_n = 0
    skip_n = 0
    skip_reasons: dict[str, int] = defaultdict(int)
    flows = [
        str((sorted(group, key=lambda r: (str(r["create_time"]), int(r["id"])))[0].get("flow_code") or ""))
        for group in by_bill.values()
        if group
    ]
    tasks_by_flow = load_tasks_by_flows(flows, day)
    day_rows = load_day_check_rows(day)
    today_by_car: dict[str, int] = defaultdict(int)
    for (_, car), rows in day_rows.items():
        today_by_car[car] += len(rows)
    car_totals = count_rows_by_car(list(today_by_car.keys()))

    for bill, group in by_bill.items():
        group = sorted(group, key=lambda r: (str(r["create_time"]), int(r["id"])))
        plate = str(group[0].get("vehicle_no") or "")
        base = {
            "weight_bill_no": bill,
            "biz_date": day_s,
            "plate": plate,
            "manual_count": len(group),
        }
        chosen = select_manual_checks(group, max_manuals)
        if not chosen:
            reason = "未满2条人工"
            skip_n += 1
            skip_reasons[reason] += 1
            vehicles.append({**base, "status": "skip", "reason": reason})
            append_log("skip", {**base, "reason": reason})
            continue

        humans = humans_from_checks(chosen, details_by_check)
        extra = {**base}
        attach_human_fields(extra, humans)
        reason = judge_main_types(humans)
        if reason:
            skip_n += 1
            skip_reasons[reason] += 1
            vehicles.append({**extra, "status": "skip", "reason": reason})
            append_log("skip", {**extra, "reason": reason})
            continue
        t1 = int(humans[0]["steel_type"])

        if is_processed(bill, day_s):
            reason = "该磅单+日期已处理"
            skip_n += 1
            skip_reasons[reason] += 1
            vehicles.append({**extra, "status": "done", "reason": reason})
            continue
        existing = load_pending(bill, day_s)
        if existing and int(existing.get("humans_used") or 2) == len(humans):
            vehicles.append(
                merge_pending_ai({**extra, "status": "pending", "reason": "已在待审"}, existing)
            )
            pending_n += 1
            continue

        flow = str(chosen[0].get("flow_code") or chosen[-1].get("flow_code") or "")
        task = tasks_by_flow.get(flow) if flow else None
        if task is None:
            task = load_task_via_order_car(bill, day)
        if task is None:
            reason = "没有对应当日 AI 任务"
            skip_n += 1
            skip_reasons[reason] += 1
            vehicles.append({**extra, "status": "skip", "reason": reason})
            append_log("skip", {**extra, "reason": reason})
            continue

        task_code = str(task["task_code"])
        car_number = str(task.get("car_number") or plate)
        rows = day_rows.get((task_code, car_number), [])
        if not rows:
            rows = load_check_rows(task_code, car_number, day)
        if not rows:
            reason = "当日该 task 无明细行"
            skip_n += 1
            skip_reasons[reason] += 1
            vehicles.append({**extra, "status": "skip", "reason": reason})
            append_log("skip", {**extra, "reason": reason})
            continue

        pcts = car_type_pcts(rows)
        ai_pct = pcts.get(t1, 0.0)
        avg = float(extra["avg"])
        hun = hunliao_stats(rows)
        display_pct, boost_pp = display_main_pct(ai_pct, day, is_mix=hun["is_mix"])
        # 混料比库内真实占比；非混料比页面加幅后的展示占比
        compare_pct = display_pct
        gap = abs(compare_pct - avg)
        extra.update(
            {
                "task_code": task_code,
                "car_number": car_number,
                "ai_pct": round(ai_pct, 2),
                "display_pct": round(display_pct, 2),
                "boost_pp": round(boost_pp, 1),
                "compare_pct": round(compare_pct, 2),
                "avg": round(avg, 2),
                "gap": round(gap, 2),
                "mix_before": round(hun["ratio"] * 100.0, 1),
                "is_mix": hun["is_mix"],
            }
        )
        if gap < BAND_PP:
            if hun["is_mix"]:
                reason = "真实占比已在±10%带内"
            else:
                reason = "非混料展示占比已在±10%带内"
            skip_n += 1
            skip_reasons[reason] += 1
            vehicles.append({**extra, "status": "skip", "reason": reason})
            append_log("skip", {**extra, "reason": reason})
            continue

        seed = f"{bill}|{day_s}|{task_code}"
        target, delta = pick_target_with_jitter(avg, seed)
        other_max = max((v for k, v in pcts.items() if k != t1), default=0.0)
        clamped = clamp_still_highest(target, avg, other_max)
        if clamped is None:
            reason = "无法同时满足仍最高且落在10%带内"
            skip_n += 1
            skip_reasons[reason] += 1
            vehicles.append({**extra, "status": "skip", "reason": reason})
            append_log("skip", {**extra, "reason": reason})
            continue
        target = clamped

        flip_uuids: list[str] = []
        mix_after = hun["ratio"]
        if not hun["is_mix"]:
            hun_plan = plan_hunliao_flip(rows, seed)
            if not hun_plan.ok:
                reason = hun_plan.reason or "改混料会过高，已跳过"
                skip_n += 1
                skip_reasons[reason] += 1
                vehicles.append({**extra, "status": "skip", "reason": reason})
                append_log("skip", {**extra, "reason": reason})
                continue
            flip_uuids = hun_plan.uuids
            mix_after = hun_plan.mix_after

        vol = plan_volume(rows, t1, target, seed)
        if not vol.ok:
            reason = vol.reason or "体积方案失败"
            skip_n += 1
            skip_reasons[reason] += 1
            vehicles.append({**extra, "status": "skip", "reason": reason})
            append_log("skip", {**extra, "reason": reason})
            continue

        tmin, tmax = _time_window(rows)
        other_rows = max(0, int(car_totals.get(car_number, 0)) - int(today_by_car.get(car_number, 0)))
        hun_ids = apply_hunliao_ids(rows, flip_uuids)
        after_pcts = {int(k): round(v, 2) for k, v in vol.after_pcts.items()}
        card = {
            **extra,
            "kind": KIND_VOLUME,
            "status": "pending",
            "main_type": t1,
            "main_label": steel_label(t1),
            "target": round(target, 2),
            "delta": round(delta, 2),
            "ai_after": after_pcts.get(t1, round(target, 2)),
            "after_pcts": after_pcts,
            "mix_after": round(mix_after * 100.0, 1),
            "mix_note": "只翻到刚过55%，不拉到80%/100%",
            "update_rows": len(vol.volume_updates) + (1 if hun_ids else 0),
            "volume_update_rows": len(vol.volume_updates),
            "hunliao_update_rows": len(hun_ids),
            "insert_rows": len(vol.inserts),
            "time_min": tmin,
            "time_max": tmax,
            "row_count": len(rows),
            "other_date_rows": other_rows,
            "grab_samples": vol.grab_samples,
            "ids": [int(r["id"]) for r in rows],
            "volume_updates": {str(k): v for k, v in vol.volume_updates.items()},
            "hunliao_ids": hun_ids,
            "flip_uuids": flip_uuids,
            "inserts": vol.inserts,
            "auto": auto,
        }
        save_pending(card)
        pending_n += 1
        vehicles.append({
            "weight_bill_no": bill,
            "biz_date": day_s,
            "plate": plate,
            "kind": KIND_VOLUME,
            "status": "pending",
            "main_label": card["main_label"],
            "ai_pct": card["ai_pct"],
            "display_pct": card.get("display_pct"),
            "compare_pct": card.get("compare_pct"),
            "boost_pp": card.get("boost_pp"),
            "avg": card["avg"],
            "gap": card["gap"],
            "target": card["target"],
            "task_code": task_code,
            "human1": card["human1"],
        })
        append_log("pending", {
            "weight_bill_no": bill,
            "biz_date": day_s,
            "plate": plate,
            "ai_pct": card["ai_pct"],
            "target": card["target"],
        })

    deduct_pending_n = 0
    if include_deduct:
        deduct_out = _scan_deduct_day(
            day,
            day_s,
            by_bill=by_bill,
            tasks_by_flow=tasks_by_flow,
            day_rows=day_rows,
            max_manuals=max_manuals,
        )
        vehicles.extend(deduct_out["vehicles"])
        deduct_pending_n = int(deduct_out["pending"])
        skip_n += int(deduct_out["skipped"])
        for reason, n in deduct_out["skip_reasons"].items():
            skip_reasons[reason] += n
        pending_n += deduct_pending_n

    summary = {
        "biz_date": day_s,
        "manual_bills": len(by_bill),
        "vehicles": vehicles,
        "pending": pending_n,
        "deduct_pending": deduct_pending_n,
        "skipped": skip_n,
        "skip_reasons": dict(skip_reasons),
        "pending_cards": len(list_pending(day_s)),
    }
    write_scan_summary(day_s, summary)
    write_heartbeat({"last_scan_date": day_s, "mode": heartbeat_mode, "pending": pending_n})
    if notify_pending and pending_n:
        extra = f"，其中扣重 {deduct_pending_n}" if deduct_pending_n else ""
        notify("检判待审核", f"{day_s} 有 {pending_n} 辆车待审{extra}，打开 http://127.0.0.1:8765")
    return summary


def _scan_deduct_day(
    day: date,
    day_s: str,
    *,
    by_bill: dict[str, list[dict[str, Any]]],
    tasks_by_flow: dict[str, dict[str, Any]],
    day_rows: dict[tuple[str, str], list[dict[str, Any]]],
    max_manuals: int,
) -> dict[str, Any]:
    """Independent deduct queue. Does not require matching main types."""
    vehicles: list[dict[str, Any]] = []
    pending_n = 0
    skip_n = 0
    skip_reasons: dict[str, int] = defaultdict(int)
    nets = load_order_net_weights(list(by_bill.keys()))

    for bill, group in by_bill.items():
        group = sorted(group, key=lambda r: (str(r["create_time"]), int(r["id"])))
        plate = str(group[0].get("vehicle_no") or "")
        base = {
            "kind": KIND_DEDUCT,
            "weight_bill_no": bill,
            "biz_date": day_s,
            "plate": plate,
            "manual_count": len(group),
        }
        chosen = select_manual_checks(group, max_manuals)
        if not chosen:
            continue
        tons = [row.get("deduction") for row in chosen]
        human_kg = human_deduct_kg(tons)
        if human_kg is None:
            reason = "三人扣重都为空" if len(chosen) >= 3 else "两人扣重都为空"
            skip_n += 1
            skip_reasons[reason] += 1
            vehicles.append({**base, "status": "skip", "reason": reason})
            append_log("deduct_skip", {**base, "reason": reason})
            continue
        extra = {
            **base,
            "humans_used": len(chosen),
            "human1_t": None if chosen[0].get("deduction") is None else float(chosen[0]["deduction"]),
            "human2_t": None if chosen[1].get("deduction") is None else float(chosen[1]["deduction"]),
            "human_kg": round(human_kg, 2),
        }
        if len(chosen) >= 3:
            extra["human3_t"] = (
                None if chosen[2].get("deduction") is None else float(chosen[2]["deduction"])
            )
        if is_processed(bill, day_s, KIND_DEDUCT):
            vehicles.append({**extra, "status": "done", "reason": "该磅单+日期扣重已处理"})
            continue
        flow = str(chosen[0].get("flow_code") or chosen[-1].get("flow_code") or "")
        task = tasks_by_flow.get(flow) if flow else None
        if task is None:
            task = load_task_via_order_car(bill, day)
        if task is None:
            reason = "没有对应当日 AI 任务"
            skip_n += 1
            skip_reasons[reason] += 1
            vehicles.append({**extra, "status": "skip", "reason": reason})
            append_log("deduct_skip", {**extra, "reason": reason})
            continue
        task_code = str(task["task_code"])
        car_number = str(task.get("car_number") or plate)
        rows = day_rows.get((task_code, car_number), [])
        if not rows:
            rows = load_check_rows(task_code, car_number, day)
        if not rows:
            reason = "当日该 task 无明细行"
            skip_n += 1
            skip_reasons[reason] += 1
            vehicles.append({**extra, "status": "skip", "reason": reason})
            append_log("deduct_skip", {**extra, "reason": reason})
            continue
        net = nets.get(bill)
        if net is None or net <= 0:
            reason = "没有过磅净重"
            skip_n += 1
            skip_reasons[reason] += 1
            vehicles.append({**extra, "status": "skip", "reason": reason})
            append_log("deduct_skip", {**extra, "reason": reason})
            continue
        n_deduct = deduct_row_count(rows)
        if n_deduct <= 0:
            reason = "没有扣杂行"
            skip_n += 1
            skip_reasons[reason] += 1
            vehicles.append({**extra, "status": "skip", "reason": reason})
            append_log("deduct_skip", {**extra, "reason": reason})
            continue
        pre = task_preprocess(task)
        page = calculate_deduct(rows, net, pre, task.get("deduct_weight_rate"))
        ai_kg = page.ai_kg
        gap = deduct_gap_kg(ai_kg, human_kg)
        extra.update(
            {
                "task_code": task_code,
                "car_number": car_number,
                "net_weight": net,
                "ai_kg": round(ai_kg, 2),
                "original_kg": round(page.original_kg, 2),
                "base_kg": round(page.base_kg, 2),
                "pre_kg": round(page.pre_kg, 2),
                "raw_kg": round(raw_deduct_kg(rows, net), 2),
                "preprocess_add_value": round(pre, 2),
                "preprocess_op": page.preprocess_op,
                "interval": page.interval,
                "random_missing": page.random_missing,
                "deduct_weight_rate": task.get("deduct_weight_rate"),
                "calculation_rule": task.get("calculation_rule") or "",
                "gap_kg": round(gap, 2),
                "deduct_rows": n_deduct,
                "in_band": not is_deduct_unqualified(ai_kg, human_kg),
            }
        )
        if not is_deduct_unqualified(ai_kg, human_kg):
            stale = load_pending(bill, day_s, KIND_DEDUCT)
            if stale:
                if stale.get("written"):
                    mark_processed(
                        bill,
                        day_s,
                        {
                            "kind": KIND_DEDUCT,
                            "task_code": task_code,
                            "car_number": car_number,
                            "ai_kg": extra["ai_kg"],
                            "gap_kg": extra["gap_kg"],
                            "backup": stale.get("original_backup"),
                            "reason": "重扫后核算差已在150kg内",
                        },
                        KIND_DEDUCT,
                    )
                remove_pending(bill, day_s, KIND_DEDUCT)
            reason = "扣重差已在150kg内"
            skip_n += 1
            skip_reasons[reason] += 1
            vehicles.append({**extra, "status": "skip", "reason": reason})
            append_log("deduct_skip", {**extra, "reason": reason})
            continue
        existing = load_pending(bill, day_s, KIND_DEDUCT)
        card = {
            **(existing or {}),
            **extra,
            "kind": KIND_DEDUCT,
            "status": "pending",
            "time_min": _time_window(rows)[0],
            "time_max": _time_window(rows)[1],
            "row_count": len(rows),
        }
        save_pending(card)
        pending_n += 1
        vehicles.append({**extra, "status": "pending", "reason": "扣重待审"})
        append_log("deduct_pending", {**extra, "gap_kg": extra["gap_kg"]})

    return {
        "vehicles": vehicles,
        "pending": pending_n,
        "skipped": skip_n,
        "skip_reasons": dict(skip_reasons),
    }
