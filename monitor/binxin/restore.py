"""从快照回滚一辆车。逐抓斗体积、新增行、汇总表、扣重四样一起复原。

只发 UPDATE。新增出来的行不删，把体积和历史体积置零，占比上等于不存在。
和落库一样走 execute_guarded，每条语句都过 sql_guard。
"""

from __future__ import annotations

import argparse
import logging
from typing import Any

from monitor.binxin import db
from monitor.binxin.backup import ROLLBACK_DEDUCT_COLUMNS, latest_backup, load_snapshot
from monitor.binxin.store import append_log, load_processed, ticket_key, unmark_processed
from monitor.sql_guard import assert_safe_sql

logger = logging.getLogger(__name__)

STAT_VALUE_COLUMNS = ("steel_rate", "steel_volume", "steel_weight")


def _num(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    return float(value)


def _check_statements(
    flow_code: str,
    rows: list[dict[str, Any]],
) -> list[tuple[str, tuple[Any, ...]]]:
    known = [r for r in rows if r.get("id") is not None]
    if not known:
        return []
    ids = [int(r["id"]) for r in known]
    cases = " ".join(["WHEN %s THEN %s"] * len(known))
    params: list[Any] = []
    for row in known:
        params.extend([int(row["id"]), _num(row.get("total_volume"))])
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


def _void_statements(
    flow_code: str,
    insert_ids: list[int],
) -> list[tuple[str, tuple[Any, ...]]]:
    """作废新增行。不 DELETE，置零就不再参与占比。"""
    if not insert_ids:
        return []
    placeholders = ",".join(["%s"] * len(insert_ids))
    sql = (
        "UPDATE intelli_task_check SET total_volume = 0, old_total_volume = 0 "
        f"WHERE flow_code = %s AND id IN ({placeholders})"
    )
    assert_safe_sql(sql)
    return [(sql, (flow_code, *[int(i) for i in insert_ids]))]


def _stat_statements(
    flow_code: str,
    rows: list[dict[str, Any]],
) -> list[tuple[str, tuple[Any, ...]]]:
    out: list[tuple[str, tuple[Any, ...]]] = []
    sets = ", ".join(f"{c} = %s" for c in STAT_VALUE_COLUMNS)
    sql = f"UPDATE steel_type_stat SET {sets} WHERE id = %s AND flow_code = %s"
    assert_safe_sql(sql)
    for row in rows:
        if row.get("id") is None:
            continue
        out.append(
            (
                sql,
                (
                    *(_num(row.get(c)) for c in STAT_VALUE_COLUMNS),
                    int(row["id"]),
                    flow_code,
                ),
            )
        )
    return out


def _deduct_statements(
    flow_code: str,
    row: dict[str, Any] | None,
) -> list[tuple[str, tuple[Any, ...]]]:
    """把扣重字段改回快照里的原值。老快照没有这一节，跳过。

    原值可能是 NULL（AI 没出修正扣杂的车），所以不能用 _num 兜成 0，要原样写回去。
    """
    if not row:
        return []
    sets = ", ".join(f"{c} = %s" for c in ROLLBACK_DEDUCT_COLUMNS)
    sql = f"UPDATE intelli_task_info SET {sets} WHERE flow_code = %s"
    assert_safe_sql(sql)
    return [(sql, tuple(row.get(c) for c in ROLLBACK_DEDUCT_COLUMNS) + (flow_code,))]


def build_restore_statements(
    snapshot: dict[str, Any],
    insert_ids: list[int],
) -> list[tuple[str, tuple[Any, ...]]]:
    flow = str(snapshot["meta"]["flow_code"])
    stmts = _check_statements(flow, snapshot.get("check_rows") or [])
    stmts += _void_statements(flow, insert_ids)
    stmts += _stat_statements(flow, snapshot.get("stat_rows") or [])
    stmts += _deduct_statements(flow, snapshot.get("deduct_row"))
    if not stmts:
        raise RuntimeError("快照里没有可回滚的内容")
    return stmts


def restore(flow_code: str, day: str, *, file: str = "") -> dict[str, Any]:
    """回滚一辆车。不传 file 就用该车最近一次快照。"""
    path = file
    if not path:
        hit = latest_backup(flow_code, day)
        if hit is None:
            raise FileNotFoundError(f"没有 {flow_code} {day} 的快照")
        path = str(hit["snapshot"])
    snapshot = load_snapshot(path)
    insert_ids = _insert_ids_of(flow_code, day)
    stmts = build_restore_statements(snapshot, insert_ids)
    db.execute_guarded(stmts)
    unmark_processed(flow_code, day)
    append_log(
        "restored",
        {
            "flow_code": flow_code,
            "biz_date": day,
            "car_number": snapshot["meta"].get("car_number"),
            "snapshot": path,
            "voided_inserts": insert_ids,
        },
    )
    return {
        "ok": True,
        "restored": True,
        "snapshot": path,
        "check_rows": len(snapshot.get("check_rows") or []),
        "stat_rows": len(snapshot.get("stat_rows") or []),
        "deduct_restored": bool(snapshot.get("deduct_row")),
        "voided_inserts": insert_ids,
    }


def _insert_ids_of(flow_code: str, day: str) -> list[int]:
    """新增行的 id 记在 processed 里，回滚时要把它们置零。"""
    rec = load_processed().get(ticket_key(flow_code, day)) or {}
    return [int(x) for x in (rec.get("insert_ids") or [])]


def main() -> int:
    parser = argparse.ArgumentParser(description="镔鑫回滚一辆车")
    parser.add_argument("--flow", required=True, help="flow_code")
    parser.add_argument("--date", required=True, help="业务日 YYYY-MM-DD")
    parser.add_argument("--file", default="", help="指定快照文件，默认取最近一次")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = restore(args.flow, args.date, file=args.file)
    print(
        f"已回滚 {result['check_rows']} 行逐抓斗、{result['stat_rows']} 行汇总，"
        f"作废新增 {len(result['voided_inserts'])} 行"
        + ("，扣重已复原" if result["deduct_restored"] else "，快照里没有扣重")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
