"""批准一辆待审车并落库。调到这里就意味着人已经在看板上逐车看过并确认了。

落库顺序，每一步失败都不会留下半成品：

1. 回读实时逐抓斗与汇总行。不拿扫描时的旧数据写库。
2. 用实时数据重新判一遍：已经进带了就拒绝批准，免得白改一次。
3. 用实时数据重新出方案，并跑一遍和试跑相同的自检。
4. 存快照、写回滚脚本。
5. 一个事务里改逐抓斗体积、补料型行、重建汇总表。
6. 回读汇总表核对展示占比真的进带了。没进带不回滚（数据是自洽的），但会大声记一条日志
   并把回滚脚本路径带回去，由人决定。
"""

from __future__ import annotations

import logging
from typing import Any

from monitor.binxin import db
from monitor.binxin.backup import update_rollback_with_inserts, write_snapshot
from monitor.binxin.config import CHECKS_DEDUCT
from monitor.binxin.correct import CLONE_CLEAR, GrabPlan, plan_grabs, rebuild_stat_rows
from monitor.binxin.deduct import Deduct, decide, deduct_key, target_fields
from monitor.binxin.materials import Key, label, priority_rank
from monitor.binxin.rate import in_band, main_key, normalize
from monitor.binxin.scan import (
    KIND_BOTH,
    KIND_DEDUCT,
    ai_raw_pcts_of,
    human_pcts_of,
    primary_stat_group,
)
from monitor.binxin.store import (
    append_log,
    load_pending,
    mark_processed,
    remove_pending,
    unmark_processed,
)
from monitor.sql_guard import assert_safe_sql

logger = logging.getLogger(__name__)

#: 插新行时写的列。images / origin_image_url 必须带上，克隆时清掉的字段不在其中。
INSERT_COLUMNS = (
    "task_code",
    "flow_code",
    "car_number",
    "steel_type",
    "steel_level",
    "status",
    "create_time",
    "duration_time",
    "check_type",
    "check_method",
    "station_number",
    "algorithm_id",
    "camera_id",
    "uuid",
    "acc_timestamp",
    "acc_time",
    "total_volume",
    "old_total_volume",
    "origin_image_url",
    "images",
)

#: 汇总表要重写的三列。其余列（flow/task/车牌/岗位/data_type）保持原样。
STAT_VALUE_COLUMNS = ("steel_rate", "steel_volume", "steel_weight")

#: intelli_task_info 扣重要写的列。两个 rate 必须跟着重量一起动，页面上是并排显示的。
DEDUCT_COLUMNS = (
    "deduct_weight",
    "deduct_weight_rate",
    "modified_deduct_weight",
    "modified_deduct_rate",
    "calculated_deduct_weight",
    "final_deduct",
)


class ApproveRefused(RuntimeError):
    """批准前的检查没过。没有发生任何写入。"""


def _key_of(card_key: Any) -> Key:
    pair = tuple(int(x) for x in card_key)
    if len(pair) != 2:
        raise ApproveRefused(f"待确认记录里的料型 {card_key!r} 不合法")
    return (pair[0], pair[1])


def _volume_statements(
    flow_code: str,
    updates: dict[int, float],
) -> list[tuple[str, tuple[Any, ...]]]:
    if not updates:
        return []
    ids = sorted(updates)
    cases = " ".join(["WHEN %s THEN %s"] * len(ids))
    params: list[Any] = []
    for row_id in ids:
        params.extend([row_id, float(updates[row_id])])
    placeholders = ",".join(["%s"] * len(ids))
    sql = (
        "UPDATE intelli_task_check SET total_volume = CASE id "
        f"{cases} ELSE total_volume END "
        f"WHERE flow_code = %s AND id IN ({placeholders})"
    )
    params.append(flow_code)
    params.extend(ids)
    assert_safe_sql(sql)
    return [(sql, tuple(params))]


def _insert_statements(inserts: list[dict[str, Any]]) -> list[tuple[str, tuple[Any, ...]]]:
    """每行一条 INSERT ... RETURNING id，回滚要靠这些 id。"""
    if not inserts:
        return []
    cols = ",".join(INSERT_COLUMNS)
    placeholders = ",".join(["%s"] * len(INSERT_COLUMNS))
    sql = f"INSERT INTO intelli_task_check ({cols}) VALUES ({placeholders}) RETURNING id"
    assert_safe_sql(sql)
    out: list[tuple[str, tuple[Any, ...]]] = []
    for row in inserts:
        leaked = [f for f in CLONE_CLEAR if f in row]
        if leaked:
            raise ApproveRefused(f"新增行带了不该带的字段 {leaked}，已中止")
        if not row.get("images"):
            raise ApproveRefused("新增行没有 images，已中止")
        out.append((sql, tuple(row.get(c) for c in INSERT_COLUMNS)))
    return out


def _stat_statements(
    flow_code: str,
    existing: list[dict[str, Any]],
    wanted: list[dict[str, Any]],
) -> list[tuple[str, tuple[Any, ...]]]:
    """按 (steel_type, steel_level) 对齐：有的改，没有的插，消失的置零。"""
    by_key = {(int(r["steel_type"]), int(r["steel_level"])): r for r in existing}
    if not existing:
        raise ApproveRefused("汇总表里没有这辆车的行，不敢凭空重建")
    data_type = existing[0].get("data_type")
    template = existing[0]
    out: list[tuple[str, tuple[Any, ...]]] = []
    seen: set[tuple[int, int]] = set()
    for row in wanted:
        key = (int(row["steel_type"]), int(row["steel_level"]))
        seen.add(key)
        hit = by_key.get(key)
        if hit is not None:
            sets = ", ".join(f"{c} = %s" for c in STAT_VALUE_COLUMNS)
            sql = f"UPDATE steel_type_stat SET {sets} WHERE id = %s AND flow_code = %s"
            assert_safe_sql(sql)
            params = tuple(row[c] for c in STAT_VALUE_COLUMNS) + (int(hit["id"]), flow_code)
            out.append((sql, params))
            continue
        cols = (
            "flow_code",
            "task_code",
            "car_number",
            "station_number",
            "data_type",
            "create_time",
            "steel_type",
            "steel_level",
            *STAT_VALUE_COLUMNS,
        )
        sql = (
            f"INSERT INTO steel_type_stat ({','.join(cols)}) "
            f"VALUES ({','.join(['%s'] * len(cols))})"
        )
        assert_safe_sql(sql)
        params = (
            flow_code,
            template.get("task_code"),
            template.get("car_number"),
            template.get("station_number"),
            data_type,
            template.get("create_time"),
            key[0],
            key[1],
            *(row[c] for c in STAT_VALUE_COLUMNS),
        )
        out.append((sql, params))
    for key, row in by_key.items():
        if key in seen:
            continue
        sets = ", ".join(f"{c} = %s" for c in STAT_VALUE_COLUMNS)
        sql = f"UPDATE steel_type_stat SET {sets} WHERE id = %s AND flow_code = %s"
        assert_safe_sql(sql)
        out.append((sql, (0, 0, 0, int(row["id"]), flow_code)))
    return out


def _deduct_statements(
    flow_code: str,
    fields: dict[str, Any] | None,
) -> list[tuple[str, tuple[Any, ...]]]:
    """扣重改一行 intelli_task_info。不改扣重时返回空。"""
    if not fields:
        return []
    unknown = [c for c in fields if c not in DEDUCT_COLUMNS]
    if unknown:
        raise ApproveRefused(f"扣重方案里有不认识的列 {unknown}，已中止")
    cols = [c for c in DEDUCT_COLUMNS if c in fields]
    sets = ", ".join(f"{c} = %s" for c in cols)
    sql = f"UPDATE intelli_task_info SET {sets} WHERE flow_code = %s"
    assert_safe_sql(sql)
    return [(sql, tuple(fields[c] for c in cols) + (flow_code,))]


def build_statements(
    flow_code: str,
    plan: GrabPlan | None,
    existing_stat: list[dict[str, Any]],
    wanted_stat: list[dict[str, Any]],
    deduct_fields: dict[str, Any] | None = None,
) -> list[tuple[str, tuple[Any, ...]]]:
    """整车的全部写语句。顺序固定：改体积 → 补料型行 → 重建汇总 → 改扣重。

    扣重放最后：它依赖修正后的主料型，前面几步都成了才轮到它。plan 为 None 是只改扣重的单，
    这时前三步一条语句都不发，占比相关的表原样不动。
    """
    stmts: list[tuple[str, tuple[Any, ...]]] = []
    if plan is not None:
        stmts += _volume_statements(flow_code, plan.volume_updates)
        stmts += _insert_statements(plan.inserts)
        stmts += _stat_statements(flow_code, existing_stat, wanted_stat)
    stmts += _deduct_statements(flow_code, deduct_fields)
    if not stmts:
        raise ApproveRefused("这辆车没有任何可执行的改动")
    return stmts


def replan_live(card: dict[str, Any]) -> dict[str, Any]:
    """回读实时数据并重新出方案。只读，不写。批准和"重新核对"都走这里。

    只改扣重的单（kind = 扣重）不出占比方案，也不要求这车现在不在带内——它的占比本来就
    是合格的，要改的只是 intelli_task_info 那一行。
    """
    flow = str(card["flow_code"])
    target_key = _key_of(card["target_key"])
    deduct_only = str(card.get("kind") or "") == KIND_DEDUCT
    check_rows = db.load_check_rows(flow, include_images=True)
    if not check_rows:
        raise ApproveRefused("回读逐抓斗记录为空，已中止")
    stat_rows = primary_stat_group(db.reread_stat_rows(flow))
    if not stat_rows:
        raise ApproveRefused("回读汇总表为空，已中止")
    manual_rows = db.load_manual_details([str(card.get("order_code") or "")]).get(
        str(card.get("order_code") or ""), []
    )
    human_pcts = human_pcts_of(manual_rows) if manual_rows else {}
    human_pct = float(card.get("human_main_pct") or 0.0)
    if human_pcts:
        live_main = main_key(human_pcts, rank=priority_rank)
        if live_main != target_key:
            raise ApproveRefused(
                f"人工主料型已变成 {label(live_main) if live_main else '空'}，"
                f"与待确认记录的 {label(target_key)} 不符，已中止"
            )
        human_pct = human_pcts[target_key]
    if human_pct <= 0.0:
        raise ApproveRefused("拿不到人工主料型占比，已中止")

    raw_pcts = ai_raw_pcts_of(stat_rows)
    ai_main = main_key(raw_pcts, rank=priority_rank)
    display = normalize(raw_pcts, quantize=False)
    already_in_band = in_band(
        human_pct,
        display.get(ai_main, 0.0) if ai_main else 0.0,
        same_main_type=ai_main == target_key,
    )

    plan: GrabPlan | None = None
    wanted_stat: list[dict[str, Any]] = []
    car_vol = sum(float(r.get("total_volume") or 0.0) for r in check_rows)
    if deduct_only:
        if not already_in_band:
            raise ApproveRefused("这辆车的占比现在已经不在带内了，重扫后按占比+扣重一起改")
    else:
        if already_in_band:
            raise ApproveRefused("这辆车现在已经在带内，不需要改")
        plan = plan_grabs(
            check_rows,
            target_key,
            float(card["target_raw_pct"]),
            seed=f"{card.get('car_number')}|{flow}",
            human_pct=human_pct,
        )
        if not plan.ok:
            raise ApproveRefused(f"实时数据上出不来可用方案：{plan.reason}")
        wanted_stat = rebuild_stat_rows(
            plan.after_raw_pcts,
            car_vol=car_vol,
            net_weight=float(card.get("net_weight") or 0.0),
            template={
                "flow_code": flow,
                "task_code": check_rows[0].get("task_code"),
                "car_number": card.get("car_number"),
                "station_number": card.get("station_number"),
            },
        )
    deduct = _replan_deduct(card, flow, target_key, ai_main, stat_rows)
    if deduct_only and deduct.get("deduct_fields") is None:
        raise ApproveRefused(f"这是一张只改扣重的单，但扣重方案出不来（{deduct['deduct_skip']}）")
    return {
        "plan": plan,
        "deduct_only": deduct_only,
        "check_rows": check_rows,
        "stat_rows": stat_rows,
        "wanted_stat": wanted_stat,
        "human_pct": human_pct,
        "car_vol": car_vol,
        **deduct,
    }


def _replan_deduct(
    card: dict[str, Any],
    flow: str,
    target_key: Key,
    ai_main: Key | None,
    stat_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """回读实时扣重并重新出方案。CHECKS_DEDUCT 关着就什么都不做。

    常数按 target_key（改完之后的主料型）取，不是按当前的 ai_main。主料型要变的车即便扣重
    已经合格也得重写，否则页面上 原始扣杂 + 常数 ≠ 修正扣杂。
    """
    if not CHECKS_DEDUCT:
        return {"deduct_row": None, "deduct_plan": None, "deduct_fields": None, "deduct_skip": ""}
    bill = str(card.get("order_code") or "")
    deduct_row = db.load_deduct_rows([flow]).get(flow)
    manual_row = db.load_manual_deducts([bill]).get(bill) if bill else None
    net_weight_t = float(card.get("net_weight") or 0.0)
    observed = Deduct(
        raw_kg=float((deduct_row or {}).get("deduct_weight") or 0.0) * 1000.0,
        modified_kg=_opt_float((deduct_row or {}).get("modified_deduct_weight")),
        calculated_kg=_opt_float((deduct_row or {}).get("calculated_deduct_weight")),
        final_kg=_opt_float((deduct_row or {}).get("final_deduct")),
        qigezha_kg=float((deduct_row or {}).get("qigezha_hanzha_weight") or 0.0),
        weather_kg=float((deduct_row or {}).get("weather_deduct_weight") or 0.0),
        net_weight_kg=net_weight_t * 1000.0,
        human_kg=_opt_float((manual_row or {}).get("deduction_tonnage")),
        deduct_key=deduct_key(stat_rows, target_key),
        modified_rate=float((deduct_row or {}).get("modified_deduct_rate") or 0.0),
        checked_at=str(card.get("checked_at") or ""),
    )
    changed = ai_main != target_key
    plan, skip = decide(observed, main_type_changed=changed, seed=flow)
    fields = (
        target_fields(plan, observed, net_weight_t=net_weight_t) if plan is not None else None
    )
    if changed and plan is None:
        # 主料型要变却出不了扣重方案，写下去页面上常数就会对不上。宁可整车不批。
        raise ApproveRefused(
            f"主料型要从 {label(ai_main) if ai_main else '空'} 改成 {label(target_key)}，"
            f"扣重常数会跟着变，但扣重方案出不来（{skip or '原因未知'}）。"
            "只改占比会让页面上 原始扣杂 + 常数 ≠ 修正扣杂，已中止。"
        )
    return {
        "deduct_row": deduct_row,
        "deduct_plan": plan,
        "deduct_fields": fields,
        "deduct_skip": skip,
        "deduct_observed": observed,
    }


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def approve(flow_code: str, day: str) -> dict[str, Any]:
    """批准并落库。调用方（看板）负责确认，这里不再问第二次。

    误批的兜底不是拦住调用，而是第 4 步的快照 + 回滚脚本：任何一次落库都能原样还回去。
    """
    card = load_pending(flow_code, day)
    if card is None:
        raise FileNotFoundError(f"没有待确认记录 {flow_code} {day}")
    live = replan_live(card)
    plan: GrabPlan | None = live["plan"]
    target_key = _key_of(card["target_key"])

    card_exec = {
        **card,
        "volume_updates": (
            {str(k): v for k, v in plan.volume_updates.items()} if plan is not None else {}
        ),
        "inserts": plan.inserts if plan is not None else [],
        "stat_rows": live["wanted_stat"],
        "deduct_fields": live.get("deduct_fields"),
    }
    paths = write_snapshot(
        card_exec, live["check_rows"], live["stat_rows"], live.get("deduct_row")
    )
    stmts = build_statements(
        flow_code, plan, live["stat_rows"], live["wanted_stat"], live.get("deduct_fields")
    )
    insert_ids = db.execute_guarded(stmts)
    update_rollback_with_inserts(
        paths["rollback"],
        flow_code,
        live["check_rows"],
        live["stat_rows"],
        insert_ids,
        live.get("deduct_row"),
    )

    after = verify_after_write(flow_code, target_key, live["human_pct"])
    after["deduct"] = verify_deduct_after_write(
        flow_code, target_key, live.get("deduct_observed")
    )
    mark_processed(
        flow_code,
        day,
        {
            "car_number": card.get("car_number"),
            "order_code": card.get("order_code"),
            "kind": card.get("kind") or KIND_BOTH,
            "target": label(target_key),
            "target_raw_pct": round(plan.target_raw_pct, 2) if plan is not None else None,
            "human_main_pct": round(live["human_pct"], 2),
            "after_raw_pct": after["raw_pct"],
            "after_display_pct": after["display_pct"],
            "in_band": after["in_band"],
            "updated_rows": len(plan.volume_updates) if plan is not None else 0,
            "insert_ids": insert_ids,
            "deduct": after.get("deduct"),
            "deduct_written": live.get("deduct_fields"),
            "backup": paths,
        },
    )
    remove_pending(flow_code, day)
    append_log(
        "approved" if after["in_band"] else "approved_but_off_band",
        {
            "flow_code": flow_code,
            "biz_date": day,
            "car_number": card.get("car_number"),
            "target": label(target_key),
            "after_display_pct": after["display_pct"],
            "in_band": after["in_band"],
            "deduct": after.get("deduct"),
            "insert_ids": insert_ids,
            "backup": paths,
        },
    )
    if not after["in_band"]:
        # 只改扣重的单本来就不该动占比，这里不在带内说明库里的占比在批准期间被人改过。
        logger.error(
            "%s 应用后重新核对仍不在带内：展示 %.2f%% vs 人工 %.2f%%。恢复文件 %s",
            card.get("car_number"),
            after["display_pct"],
            live["human_pct"],
            paths["rollback"],
        )
    ded = after.get("deduct")
    if ded and not ded.get("follows_rule", True):
        # 页面上 原始扣杂 + 常数 应该等于 修正扣杂。这条破了比扣重不合格更容易被发现。
        logger.error(
            "%s 应用后重新核对：修正扣杂 %s ≠ 原始 %s + 常数 %s。恢复文件 %s",
            card.get("car_number"),
            ded.get("modified_kg"),
            ded.get("raw_kg"),
            ded.get("additional_kg"),
            paths["rollback"],
        )
    return {
        "ok": True,
        "in_band": after["in_band"],
        "after": after,
        "backup": paths,
        "insert_ids": insert_ids,
        "updated_rows": len(plan.volume_updates) if plan is not None else 0,
        "inserted_rows": len(plan.inserts) if plan is not None else 0,
        "deduct_written": live.get("deduct_fields") is not None,
    }


def verify_deduct_after_write(
    flow_code: str,
    target_key: Key,
    observed: Deduct | None,
) -> dict[str, Any] | None:
    """写完从库里回读扣重重算。不信发出去的值。

    两件事都要核：一是和人工的差合格了，二是 修正扣杂 = 原始 + 常数 仍然成立——后者是
    页面上并排显示的关系，破了比不合格更显眼。

    返回的形状刻意和扫描时的 Deduct.as_card() 一致：看板把它整块盖到过期的扫描值上，
    不然落库之后页面还显示改之前的扣重，得重扫一遍才对。
    """
    if not CHECKS_DEDUCT or observed is None:
        return None
    row = db.load_deduct_rows([flow_code]).get(flow_code)
    if row is None:
        return {"ok": False, "reason": "回读扣重行为空"}
    stat_rows = primary_stat_group(db.reread_stat_rows(flow_code))
    live = Deduct(
        raw_kg=float(row.get("deduct_weight") or 0.0) * 1000.0,
        modified_kg=_opt_float(row.get("modified_deduct_weight")),
        calculated_kg=_opt_float(row.get("calculated_deduct_weight")),
        final_kg=_opt_float(row.get("final_deduct")),
        qigezha_kg=float(row.get("qigezha_hanzha_weight") or 0.0),
        weather_kg=float(row.get("weather_deduct_weight") or 0.0),
        net_weight_kg=observed.net_weight_kg,
        human_kg=observed.human_kg,
        deduct_key=deduct_key(stat_rows, target_key),
        modified_rate=float(row.get("modified_deduct_rate") or 0.0),
        checked_at=observed.checked_at,
    )
    return live.as_card()


def verify_after_write(flow_code: str, target_key: Key, human_pct: float) -> dict[str, Any]:
    """写完从库里回读重算，不信发出去的值。"""
    stat_rows = primary_stat_group(db.reread_stat_rows(flow_code))
    raw = ai_raw_pcts_of(stat_rows)
    display = normalize(raw, quantize=False)
    live_main = main_key(raw, rank=priority_rank)
    return {
        "raw_pct": round(raw.get(target_key, 0.0), 2),
        "display_pct": round(display.get(target_key, 0.0), 2),
        "main_is_target": live_main == target_key,
        "in_band": bool(
            live_main == target_key
            and in_band(human_pct, display.get(target_key, 0.0), same_main_type=True)
        ),
        "raw_pcts": {label(k): round(v, 2) for k, v in raw.items()},
        "display_pcts": {label(k): round(v, 2) for k, v in display.items()},
    }


def reject(flow_code: str, day: str) -> dict[str, Any]:
    """退回待审单。不碰库。"""
    card = load_pending(flow_code, day)
    if card is None:
        raise FileNotFoundError(f"没有待确认记录 {flow_code} {day}")
    remove_pending(flow_code, day)
    unmark_processed(flow_code, day)
    append_log(
        "rejected",
        {"flow_code": flow_code, "biz_date": day, "car_number": card.get("car_number")},
    )
    return {"ok": True, "rejected": True}
