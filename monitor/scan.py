"""Scan one business day and write pending / skip logs. No writes to business tables."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
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
    unmark_processed,
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
    *,
    min_manuals: int = 1,
) -> list[dict[str, Any]]:
    """Earliest N manuals. Volume allows 1; deduct still passes min_manuals=2."""
    ordered = sorted(group, key=lambda r: (str(r["create_time"]), int(r["id"])))
    need = max(1, int(min_manuals))
    if len(ordered) < need:
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


@dataclass
class HumanGate:
    skip_reason: str = ""
    needs_choice: bool = False
    auto_ok: bool = False
    choosable: tuple[int, ...] = field(default_factory=tuple)
    default_type: int | None = None


def avg_for_chosen_type(humans: list[dict[str, Any]], steel_type: int | None) -> float:
    if steel_type is None:
        return 0.0
    matched = [h for h in humans if h.get("steel_type") == steel_type]
    if not matched:
        return 0.0
    return round(sum(float(h["pct"]) for h in matched) / len(matched), 2)


def default_chosen_type(humans: list[dict[str, Any]], choosable: tuple[int, ...]) -> int:
    allowed = {int(t) for t in choosable}
    counts: dict[int, int] = {}
    for human in humans:
        raw = human.get("steel_type")
        if raw not in allowed:
            continue
        key = int(raw)
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return next(iter(allowed))
    top = max(counts.values())
    majority = {t for t, n in counts.items() if n == top}
    if len(majority) == 1:
        return next(iter(majority))
    for human in humans:
        raw = human.get("steel_type")
        if raw in majority:
            return int(raw)
    return next(iter(majority))


def judge_main_types(humans: list[dict[str, Any]]) -> HumanGate:
    """Who may be aligned, and whether auto-write is allowed."""
    if not humans:
        return HumanGate(skip_reason="未满1条人工")
    types = [h.get("steel_type") for h in humans]
    valid: list[int] = []
    for raw in types:
        if raw in ALLOWED_MAIN_TYPES:
            key = int(raw)
            if key not in valid:
                valid.append(key)
    if not valid:
        first = types[0]
        label = steel_label(int(first)) if first is not None else ""
        return HumanGate(skip_reason=f"主料型不是重废1/2（{label}）")
    choosable = tuple(valid)
    all_typed = all(t is not None for t in types)
    all_same = all_typed and len(set(int(t) for t in types)) == 1
    if all_same and int(types[0]) in ALLOWED_MAIN_TYPES:
        main = int(types[0])
        return HumanGate(
            auto_ok=len(humans) >= 2,
            needs_choice=False,
            choosable=(main,),
            default_type=main,
        )
    return HumanGate(
        auto_ok=False,
        needs_choice=True,
        choosable=choosable,
        default_type=default_chosen_type(humans, choosable),
    )


def attach_human_fields(
    extra: dict[str, Any],
    humans: list[dict[str, Any]],
    chosen_type: int | None = None,
) -> None:
    extra["humans_used"] = len(humans)
    extra["human1"] = humans[0]
    if len(humans) >= 2:
        extra["human2"] = humans[1]
    else:
        extra.pop("human2", None)
    if len(humans) >= 3:
        extra["human3"] = humans[2]
    else:
        extra.pop("human3", None)
    if chosen_type is None and humans:
        typed = {h.get("steel_type") for h in humans}
        if len(typed) == 1 and next(iter(typed)) is not None:
            chosen_type = int(next(iter(typed)))
    extra["avg"] = avg_for_chosen_type(humans, chosen_type)


_AI_KEYS = (
    "ai_pct",
    "display_pct",
    "boost_pp",
    "compare_pct",
    "avg",
    "gap",
    "target",
    "main_label",
    "main_type",
    "task_code",
    "auto_ok",
    "needs_choice",
    "humans_used",
    "in_band",
    "reopen",
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


def plan_type_option(
    *,
    day: date,
    humans: list[dict[str, Any]],
    steel_type: int,
    rows: list[dict[str, Any]],
    pcts: dict[int, float],
    hun: dict[str, Any],
    seed: str,
    flip_uuids: list[str],
    mix_after: float,
) -> dict[str, Any]:
    avg = avg_for_chosen_type(humans, steel_type)
    ai_pct = pcts.get(steel_type, 0.0)
    display_pct, boost_pp = display_main_pct(ai_pct, day, is_mix=bool(hun["is_mix"]))
    gap = abs(display_pct - avg)
    in_band = gap < BAND_PP
    option: dict[str, Any] = {
        "main_type": steel_type,
        "main_label": steel_label(steel_type),
        "avg": avg,
        "ai_pct": round(ai_pct, 2),
        "display_pct": round(display_pct, 2),
        "boost_pp": round(boost_pp, 1),
        "compare_pct": round(display_pct, 2),
        "gap": round(gap, 2),
        "in_band": in_band,
        "ok": False,
        "reason": "",
        "target": None,
        "delta": None,
        "ai_after": None,
        "after_pcts": {},
        "volume_updates": {},
        "inserts": [],
        "hunliao_ids": apply_hunliao_ids(rows, flip_uuids),
        "flip_uuids": flip_uuids,
        "mix_after": round(mix_after * 100.0, 1),
        "grab_samples": [],
        "volume_update_rows": 0,
        "hunliao_update_rows": 0,
        "insert_rows": 0,
        "update_rows": 0,
    }
    if in_band:
        option["reason"] = (
            "真实占比已在±10%带内" if hun["is_mix"] else "非混料展示占比已在±10%带内"
        )
        return option
    target, delta = pick_target_with_jitter(avg, seed)
    other_max = max((v for k, v in pcts.items() if k != steel_type), default=0.0)
    first = clamp_still_highest(target, avg, other_max)
    band_high = min(99.5, avg + 9.7)
    if first is None:
        first = min(band_high, max(0.5, target))
    attempts: list[tuple[float, str, float]] = [(first, seed, delta)]
    if abs(first - band_high) >= 0.05:
        attempts.append((band_high, f"{seed}|top", band_high - avg))
    last_reason = ""
    for cand, cand_seed, cand_delta in attempts:
        vol = plan_volume(rows, steel_type, cand, cand_seed)
        if not vol.ok:
            last_reason = vol.reason or "体积方案失败"
            continue
        after_pcts = {int(k): round(v, 2) for k, v in vol.after_pcts.items()}
        main_after = after_pcts.get(steel_type, 0.0)
        others = [v for k, v in after_pcts.items() if k != steel_type]
        if others and main_after <= max(others):
            last_reason = "无法同时满足仍最高且落在10%带内"
            continue
        hun_ids = option["hunliao_ids"]
        option.update(
            {
                "ok": True,
                "target": round(cand, 2),
                "delta": round(cand_delta, 2),
                "ai_after": after_pcts.get(steel_type, round(cand, 2)),
                "after_pcts": after_pcts,
                "volume_updates": {str(k): v for k, v in vol.volume_updates.items()},
                "inserts": vol.inserts,
                "grab_samples": vol.grab_samples,
                "volume_update_rows": len(vol.volume_updates),
                "hunliao_update_rows": len(hun_ids),
                "insert_rows": len(vol.inserts),
                "update_rows": len(vol.volume_updates) + (1 if hun_ids else 0),
            }
        )
        return option
    option["reason"] = last_reason or "无法同时满足仍最高且落在10%带内"
    return option


def apply_option_to_card(card: dict[str, Any], option: dict[str, Any]) -> None:
    for key in (
        "main_type",
        "main_label",
        "avg",
        "ai_pct",
        "display_pct",
        "boost_pp",
        "compare_pct",
        "gap",
        "in_band",
        "target",
        "delta",
        "ai_after",
        "after_pcts",
        "volume_updates",
        "inserts",
        "hunliao_ids",
        "flip_uuids",
        "mix_after",
        "grab_samples",
        "volume_update_rows",
        "hunliao_update_rows",
        "insert_rows",
        "update_rows",
    ):
        card[key] = option.get(key)
    card["reason"] = option.get("reason") or card.get("reason") or ""


def reopen_processed(weight_bill_no: str, day: str, kind: str = KIND_VOLUME) -> bool:
    """Remove the processed stamp so a later out-of-band scan can plan again."""
    if not is_processed(weight_bill_no, day, kind):
        return False
    unmark_processed(weight_bill_no, day, kind)
    return True


def apply_type_choice(weight_bill_no: str, day: str, main_type: int) -> dict[str, Any]:
    card = load_pending(weight_bill_no, day)
    if card is None:
        raise FileNotFoundError(f"没有待审单 {weight_bill_no} {day}")
    wanted = int(main_type)
    options = card.get("type_options") or []
    match = next((opt for opt in options if int(opt.get("main_type") or 0) == wanted), None)
    if match is None:
        raise ValueError("该料型不可选")
    apply_option_to_card(card, match)
    card["type_chosen"] = True
    save_pending(card)
    return card


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
            reason = "未满1条人工"
            skip_n += 1
            skip_reasons[reason] += 1
            vehicles.append({**base, "status": "skip", "reason": reason})
            append_log("skip", {**base, "reason": reason})
            continue

        humans = humans_from_checks(chosen, details_by_check)
        extra = {**base}
        attach_human_fields(extra, humans)
        gate = judge_main_types(humans)
        extra["auto_ok"] = gate.auto_ok
        extra["needs_choice"] = gate.needs_choice
        extra["choosable"] = list(gate.choosable)
        if gate.skip_reason:
            skip_n += 1
            skip_reasons[gate.skip_reason] += 1
            vehicles.append({**extra, "status": "skip", "reason": gate.skip_reason})
            append_log("skip", {**extra, "reason": gate.skip_reason})
            continue
        assert gate.default_type is not None
        attach_human_fields(extra, humans, gate.default_type)

        was_processed = is_processed(bill, day_s)
        extra["was_processed"] = was_processed
        existing = load_pending(bill, day_s)
        if existing and int(existing.get("humans_used") or 0) == len(humans):
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
        hun = hunliao_stats(rows)
        extra.update(
            {
                "task_code": task_code,
                "car_number": car_number,
                "mix_before": round(hun["ratio"] * 100.0, 1),
                "is_mix": hun["is_mix"],
                "mix_note": "只翻到刚过55%，不拉到80%/100%",
            }
        )
        seed = f"{bill}|{day_s}|{task_code}"
        if was_processed:
            seed = f"{seed}|reopen|{len(humans)}"
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

        options = [
            plan_type_option(
                day=day,
                humans=humans,
                steel_type=steel_type,
                rows=rows,
                pcts=pcts,
                hun=hun,
                seed=seed if steel_type == gate.default_type else f"{seed}|{steel_type}",
                flip_uuids=flip_uuids,
                mix_after=mix_after,
            )
            for steel_type in gate.choosable
        ]
        preferred = gate.default_type
        if existing and existing.get("main_type") is not None:
            try:
                kept = int(existing["main_type"])
            except (TypeError, ValueError):
                kept = None
            if kept in gate.choosable:
                preferred = kept
        selected = next((opt for opt in options if int(opt["main_type"]) == preferred), options[0])
        if not gate.needs_choice and selected.get("in_band"):
            if was_processed:
                reason = "该磅单+日期已处理"
                vehicles.append({**extra, "status": "done", "reason": reason})
                continue
            reason = str(selected.get("reason") or "已在±10%带内")
            skip_n += 1
            skip_reasons[reason] += 1
            vehicles.append({**extra, "status": "skip", "reason": reason})
            append_log("skip", {**extra, "reason": reason})
            continue
        usable = [opt for opt in options if opt.get("ok") or opt.get("in_band")]
        if not usable:
            reason = str(selected.get("reason") or "体积方案失败")
            skip_n += 1
            skip_reasons[reason] += 1
            vehicles.append({**extra, "status": "skip", "reason": reason})
            append_log("skip", {**extra, "reason": reason})
            continue

        tmin, tmax = _time_window(rows)
        other_rows = max(0, int(car_totals.get(car_number, 0)) - int(today_by_car.get(car_number, 0)))
        reopened = reopen_processed(bill, day_s)
        pending_reason = "已处理后续超带，再改" if reopened else (
            "需选主料型" if gate.needs_choice else (
                "1条人工，自动不写" if not gate.auto_ok else "已在待审"
            )
        )
        card = {
            **extra,
            "kind": KIND_VOLUME,
            "status": "pending",
            "type_options": options,
            "type_chosen": bool(existing and existing.get("type_chosen")),
            "reason": pending_reason,
            "time_min": tmin,
            "time_max": tmax,
            "row_count": len(rows),
            "other_date_rows": other_rows,
            "ids": [int(r["id"]) for r in rows],
            "auto": auto,
            "reopen": reopened,
        }
        apply_option_to_card(card, selected)
        card["reason"] = pending_reason
        save_pending(card)
        pending_n += 1
        vehicles.append({
            "weight_bill_no": bill,
            "biz_date": day_s,
            "plate": plate,
            "kind": KIND_VOLUME,
            "status": "pending",
            "reason": pending_reason,
            "main_label": card["main_label"],
            "main_type": card["main_type"],
            "ai_pct": card["ai_pct"],
            "display_pct": card.get("display_pct"),
            "compare_pct": card.get("compare_pct"),
            "boost_pp": card.get("boost_pp"),
            "avg": card["avg"],
            "gap": card["gap"],
            "target": card["target"],
            "task_code": task_code,
            "human1": card.get("human1"),
            "humans_used": card.get("humans_used"),
            "manual_count": card.get("manual_count"),
            "auto_ok": gate.auto_ok,
            "needs_choice": gate.needs_choice,
            "in_band": card.get("in_band"),
            "reopen": reopened,
        })
        append_log("pending", {
            "weight_bill_no": bill,
            "biz_date": day_s,
            "plate": plate,
            "ai_pct": card["ai_pct"],
            "target": card["target"],
            "auto_ok": gate.auto_ok,
            "needs_choice": gate.needs_choice,
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
        chosen = select_manual_checks(group, max_manuals, min_manuals=2)
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
        was_processed = is_processed(bill, day_s, KIND_DEDUCT)
        extra["was_processed"] = was_processed
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
        page = calculate_deduct(rows, net, pre)
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
            if was_processed:
                vehicles.append({**extra, "status": "done", "reason": "该磅单+日期扣重已处理"})
                continue
            reason = "扣重差已在150kg内"
            skip_n += 1
            skip_reasons[reason] += 1
            vehicles.append({**extra, "status": "skip", "reason": reason})
            append_log("deduct_skip", {**extra, "reason": reason})
            continue
        reopened = reopen_processed(bill, day_s, KIND_DEDUCT)
        existing = load_pending(bill, day_s, KIND_DEDUCT)
        deduct_reason = "已处理后续扣重超带，再改" if reopened else "扣重待审"
        card = {
            **(existing or {}),
            **extra,
            "kind": KIND_DEDUCT,
            "status": "pending",
            "reason": deduct_reason,
            "reopen": reopened,
            "time_min": _time_window(rows)[0],
            "time_max": _time_window(rows)[1],
            "row_count": len(rows),
        }
        save_pending(card)
        pending_n += 1
        vehicles.append({**extra, "status": "pending", "reason": deduct_reason, "reopen": reopened})
        append_log("deduct_pending", {**extra, "gap_kg": extra["gap_kg"], "reopen": reopened})

    return {
        "vehicles": vehicles,
        "pending": pending_n,
        "skipped": skip_n,
        "skip_reasons": dict(skip_reasons),
    }
