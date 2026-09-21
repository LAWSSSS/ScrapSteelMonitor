"""批准一辆待审车并落库。写库封锁闸不开就在开连接那一步直接抛。

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
from monitor.binxin.correct import CLONE_CLEAR, GrabPlan, plan_grabs, rebuild_stat_rows
from monitor.binxin.guard import assert_write_allowed
from monitor.binxin.materials import Key, label, priority_rank
from monitor.binxin.rate import in_band, main_key, normalize
from monitor.binxin.scan import ai_raw_pcts_of, human_pcts_of, primary_stat_group
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


class ApproveRefused(RuntimeError):
    """批准前的检查没过。没有发生任何写入。"""


def _key_of(card_key: Any) -> Key:
    pair = tuple(int(x) for x in card_key)
    if len(pair) != 2:
        raise ApproveRefused(f"待审单里的料型 {card_key!r} 不合法")
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


def build_statements(
    flow_code: str,
    plan: GrabPlan,
    existing_stat: list[dict[str, Any]],
    wanted_stat: list[dict[str, Any]],
) -> list[tuple[str, tuple[Any, ...]]]:
    """整车的全部写语句。顺序固定：改体积 → 补料型行 → 重建汇总。"""
    stmts = _volume_statements(flow_code, plan.volume_updates)
    stmts += _insert_statements(plan.inserts)
    stmts += _stat_statements(flow_code, existing_stat, wanted_stat)
    if not stmts:
        raise ApproveRefused("这辆车没有任何可执行的改动")
    return stmts


def replan_live(card: dict[str, Any]) -> dict[str, Any]:
    """回读实时数据并重新出方案。只读，不写。批准和"重新核对"都走这里。"""
    flow = str(card["flow_code"])
    target_key = _key_of(card["target_key"])
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
                f"与待审单的 {label(target_key)} 不符，已中止"
            )
        human_pct = human_pcts[target_key]
    if human_pct <= 0.0:
        raise ApproveRefused("拿不到人工主料型占比，已中止")

    raw_pcts = ai_raw_pcts_of(stat_rows)
    ai_main = main_key(raw_pcts, rank=priority_rank)
    display = normalize(raw_pcts, quantize=False)
    if in_band(
        human_pct,
        display.get(ai_main, 0.0) if ai_main else 0.0,
        same_main_type=ai_main == target_key,
    ):
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
    car_vol = sum(float(r.get("total_volume") or 0.0) for r in check_rows)
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
    return {
        "plan": plan,
        "check_rows": check_rows,
        "stat_rows": stat_rows,
        "wanted_stat": wanted_stat,
        "human_pct": human_pct,
        "car_vol": car_vol,
    }


def approve(flow_code: str, day: str) -> dict[str, Any]:
    """批准并落库。封锁闸没开时在 execute_guarded 里抛，此前不改任何数据。"""
    # 闸的真正保证在 db._open_connection 里，这里先问一次是为了锁着的时候快速失败，
    # 不白跑一遍回读、也不留下一个没用的快照文件。
    assert_write_allowed()
    card = load_pending(flow_code, day)
    if card is None:
        raise FileNotFoundError(f"没有待审单 {flow_code} {day}")
    live = replan_live(card)
    plan: GrabPlan = live["plan"]
    target_key = _key_of(card["target_key"])

    card_exec = {
        **card,
        "volume_updates": {str(k): v for k, v in plan.volume_updates.items()},
        "inserts": plan.inserts,
        "stat_rows": live["wanted_stat"],
    }
    paths = write_snapshot(card_exec, live["check_rows"], live["stat_rows"])
    stmts = build_statements(flow_code, plan, live["stat_rows"], live["wanted_stat"])
    insert_ids = db.execute_guarded(stmts)
    update_rollback_with_inserts(
        paths["rollback"], flow_code, live["check_rows"], live["stat_rows"], insert_ids
    )

    after = verify_after_write(flow_code, target_key, live["human_pct"])
    mark_processed(
        flow_code,
        day,
        {
            "car_number": card.get("car_number"),
            "order_code": card.get("order_code"),
            "target": label(target_key),
            "target_raw_pct": round(plan.target_raw_pct, 2),
            "human_main_pct": round(live["human_pct"], 2),
            "after_raw_pct": after["raw_pct"],
            "after_display_pct": after["display_pct"],
            "in_band": after["in_band"],
            "updated_rows": len(plan.volume_updates),
            "insert_ids": insert_ids,
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
            "insert_ids": insert_ids,
            "backup": paths,
        },
    )
    if not after["in_band"]:
        logger.error(
            "%s 写完回读仍不在带内：展示 %.2f%% vs 人工 %.2f%%。回滚脚本 %s",
            card.get("car_number"),
            after["display_pct"],
            live["human_pct"],
            paths["rollback"],
        )
    return {
        "ok": True,
        "in_band": after["in_band"],
        "after": after,
        "backup": paths,
        "insert_ids": insert_ids,
        "updated_rows": len(plan.volume_updates),
        "inserted_rows": len(plan.inserts),
    }


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
        raise FileNotFoundError(f"没有待审单 {flow_code} {day}")
    remove_pending(flow_code, day)
    unmark_processed(flow_code, day)
    append_log(
        "rejected",
        {"flow_code": flow_code, "biz_date": day, "car_number": card.get("car_number")},
    )
    return {"ok": True, "rejected": True}
