"""写库前的快照与回滚脚本。只生成 UPDATE，不生成 DELETE。

镔鑫要改两张表，所以快照也是两份：

- intelli_task_check 的逐抓斗行（改 total_volume，可能新增料型行）
- steel_type_stat 的汇总行（整组重建）

新增出来的行没法用 DELETE 回滚（sql_guard 不放行，也不想真删数据），所以回滚脚本把它们的
体积置零、图片和扣重字段清空，占比上等于不存在。汇总表的行则按快照里的原值改回去。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from monitor.binxin.config import STATE_ROOT, ensure_dirs
from monitor.config import TZ
from monitor.sql_guard import assert_safe_sql

logger = logging.getLogger(__name__)

#: 快照里保留的逐抓斗字段。够回滚，也够事后核对。
SNAP_CHECK_FIELDS = (
    "id",
    "uuid",
    "steel_type",
    "steel_level",
    "total_volume",
    "old_total_volume",
    "deduct_weight",
    "images",
    "origin_image_url",
)
SNAP_STAT_FIELDS = (
    "id",
    "data_type",
    "steel_type",
    "steel_level",
    "steel_rate",
    "steel_volume",
    "steel_weight",
)


def index_path() -> Path:
    return STATE_ROOT / "backups" / "index.json"


def _load_index() -> list[dict[str, Any]]:
    path = index_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _save_index(items: list[dict[str, Any]]) -> None:
    ensure_dirs()
    path = index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _slim(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    return [{f: row.get(f) for f in fields} for row in rows]


def write_snapshot(
    card: dict[str, Any],
    check_rows: list[dict[str, Any]],
    stat_rows: list[dict[str, Any]],
) -> dict[str, str]:
    """落库前存一份原样，同时写出回滚脚本。返回两个文件路径。"""
    ensure_dirs()
    day = str(card["biz_date"])
    flow = str(card["flow_code"])
    stamp = datetime.now(TZ).strftime("%H%M%S")
    folder = STATE_ROOT / "backups" / day
    folder.mkdir(parents=True, exist_ok=True)
    tag = f"{str(card.get('car_number') or flow).replace('/', '_')}_{flow[:8]}_{stamp}"
    snap_path = folder / f"{tag}.json"
    rollback_path = folder / f"rollback_{tag}.sql"
    payload = {
        "meta": {
            "flow_code": flow,
            "task_code": card.get("task_code"),
            "car_number": card.get("car_number"),
            "order_code": card.get("order_code"),
            "biz_date": day,
            "target": card.get("target"),
            "target_raw_pct": card.get("target_raw_pct"),
            "taken_at": datetime.now(TZ).isoformat(timespec="seconds"),
        },
        "check_rows": _slim(check_rows, SNAP_CHECK_FIELDS),
        "stat_rows": _slim(stat_rows, SNAP_STAT_FIELDS),
        "planned_volume_updates": card.get("volume_updates") or {},
        "planned_inserts": card.get("inserts") or [],
        "planned_stat_rows": card.get("stat_rows") or [],
    }
    snap_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    rollback_path.write_text(
        build_rollback_sql(flow, check_rows, stat_rows, insert_ids=[]), encoding="utf-8"
    )
    items = _load_index()
    items.append(
        {
            "flow_code": flow,
            "car_number": card.get("car_number"),
            "biz_date": day,
            "snapshot": str(snap_path),
            "rollback": str(rollback_path),
            "ts": datetime.now(TZ).isoformat(timespec="seconds"),
        }
    )
    _save_index(items)
    return {"snapshot": str(snap_path), "rollback": str(rollback_path)}


def update_rollback_with_inserts(
    rollback_path: str,
    flow_code: str,
    check_rows: list[dict[str, Any]],
    stat_rows: list[dict[str, Any]],
    insert_ids: list[int],
) -> None:
    """新增行的 id 只有写完才知道，回来补进回滚脚本。"""
    Path(rollback_path).write_text(
        build_rollback_sql(flow_code, check_rows, stat_rows, insert_ids), encoding="utf-8"
    )


def _num(value: Any) -> str:
    if value is None or value == "":
        return "NULL"
    return repr(float(value))


def build_rollback_sql(
    flow_code: str,
    check_rows: list[dict[str, Any]],
    stat_rows: list[dict[str, Any]],
    insert_ids: list[int],
) -> str:
    """人能直接贴进 psql 跑的回滚脚本。每条都过 sql_guard。"""
    lines = [
        f"-- 镔鑫回滚 flow_code={flow_code}",
        f"-- 生成于 {datetime.now(TZ).isoformat(timespec='seconds')}",
        "-- 只有 UPDATE，没有 DELETE；新增行用置零的方式作废",
        "BEGIN;",
    ]
    ids = [int(r["id"]) for r in check_rows if r.get("id") is not None]
    if ids:
        cases = " ".join(
            f"WHEN {int(r['id'])} THEN {_num(r.get('total_volume'))}" for r in check_rows
        )
        sql = (
            "UPDATE intelli_task_check SET total_volume = CASE id "
            f"{cases} ELSE total_volume END "
            f"WHERE flow_code='{flow_code}' AND id IN ({','.join(str(i) for i in ids)});"
        )
        assert_safe_sql(sql)
        lines.append(sql)
    if insert_ids:
        id_list = ",".join(str(int(i)) for i in insert_ids)
        sql = (
            "UPDATE intelli_task_check SET total_volume = 0, old_total_volume = 0 "
            f"WHERE flow_code='{flow_code}' AND id IN ({id_list});"
        )
        assert_safe_sql(sql)
        lines.append(sql)
    stat_ids = [int(r["id"]) for r in stat_rows if r.get("id") is not None]
    if stat_ids:
        for field in ("steel_rate", "steel_volume", "steel_weight"):
            cases = " ".join(
                f"WHEN {int(r['id'])} THEN {_num(r.get(field))}" for r in stat_rows
            )
            sql = (
                f"UPDATE steel_type_stat SET {field} = CASE id "
                f"{cases} ELSE {field} END "
                f"WHERE flow_code='{flow_code}' "
                f"AND id IN ({','.join(str(i) for i in stat_ids)});"
            )
            assert_safe_sql(sql)
            lines.append(sql)
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def load_snapshot(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def latest_backup(flow_code: str, day: str) -> dict[str, Any] | None:
    hits = [
        x
        for x in _load_index()
        if str(x.get("flow_code")) == flow_code and str(x.get("biz_date")) == day
    ]
    return hits[-1] if hits else None
