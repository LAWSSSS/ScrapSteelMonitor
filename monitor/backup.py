"""Per-car snapshot and UPDATE-only rollback SQL."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from monitor.config import ROOT, TZ, ensure_dirs
from monitor.db import CHECK_COLUMNS
from monitor.sql_guard import assert_safe_sql


def _index_path() -> Path:
    return ROOT / "backups" / "index.json"


def _load_index() -> list[dict[str, Any]]:
    path = _index_path()
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def _save_index(items: list[dict[str, Any]]) -> None:
    ensure_dirs()
    _index_path().write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def snapshot_dir(day: str) -> Path:
    path = ROOT / "backups" / day
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_snapshot(
    card: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, str]:
    ensure_dirs()
    stamp = datetime.now(TZ).strftime("%H%M%S")
    bill = str(card["weight_bill_no"]).replace("/", "_")
    day = str(card["biz_date"])
    folder = snapshot_dir(day)
    snap_path = folder / f"{bill}_{stamp}.json"
    rollback_path = folder / f"rollback_{bill}_{stamp}.sql"
    payload = {
        "card_meta": {
            "weight_bill_no": card["weight_bill_no"],
            "biz_date": day,
            "task_code": card["task_code"],
            "car_number": card["car_number"],
            "plate": card.get("plate"),
        },
        "rows": rows,
        "planned_inserts": card.get("inserts") or [],
        "volume_updates": card.get("volume_updates") or {},
        "hunliao_ids": card.get("hunliao_ids") or [],
    }
    snap_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    rollback_path.write_text(
        build_rollback_sql(card, rows, insert_ids=[]),
        encoding="utf-8",
    )
    items = _load_index()
    items.append(
        {
            "weight_bill_no": card["weight_bill_no"],
            "biz_date": day,
            "task_code": card["task_code"],
            "kind": card.get("kind") or "volume",
            "snapshot": str(snap_path),
            "rollback": str(rollback_path),
            "ts": datetime.now(TZ).isoformat(timespec="seconds"),
        }
    )
    _save_index(items)
    return {"snapshot": str(snap_path), "rollback": str(rollback_path)}


def update_rollback_with_inserts(
    rollback_path: str,
    card: dict[str, Any],
    rows: list[dict[str, Any]],
    insert_ids: list[int],
) -> None:
    path = Path(rollback_path)
    path.write_text(build_rollback_sql(card, rows, insert_ids), encoding="utf-8")


def build_rollback_sql(
    card: dict[str, Any],
    rows: list[dict[str, Any]],
    insert_ids: list[int],
) -> str:
    task = card["task_code"]
    car = card["car_number"]
    day = card["biz_date"]
    start, end = f"{day} 00:00:00", f"{day} 23:59:59"
    lines = [
        "-- 仅 UPDATE，不含 DELETE",
        "START TRANSACTION;",
    ]
    vol_ids = [int(r["id"]) for r in rows]
    if vol_ids:
        cases = " ".join(
            f"WHEN {int(r['id'])} THEN {float(r.get('total_volume') or 0):.4f}" for r in rows
        )
        id_list = ",".join(str(i) for i in vol_ids)
        sql = (
            "UPDATE slzj.intelli_task_check SET total_volume = CASE id "
            f"{cases} ELSE total_volume END "
            f"WHERE task_code='{task}' AND car_number='{car}' "
            f"AND create_time>='{start}' AND create_time<='{end}' "
            f"AND id IN ({id_list});"
        )
        assert_safe_sql(sql)
        lines.append(sql)
        hun_cases = " ".join(
            f"WHEN {int(r['id'])} THEN {_sql_int_or_null(r.get('is_hunliao'))}" for r in rows
        )
        sql_h = (
            "UPDATE slzj.intelli_task_check SET is_hunliao = CASE id "
            f"{hun_cases} ELSE is_hunliao END "
            f"WHERE task_code='{task}' AND car_number='{car}' "
            f"AND create_time>='{start}' AND create_time<='{end}' "
            f"AND id IN ({id_list});"
        )
        assert_safe_sql(sql_h)
        lines.append(sql_h)
    if insert_ids:
        id_list = ",".join(str(i) for i in insert_ids)
        sql_i = (
            "UPDATE slzj.intelli_task_check SET total_volume = 0, "
            "origin_image_url=NULL, is_hunliao=NULL, "
            "deduct_weight=NULL, deduct_weight_url=NULL "
            f"WHERE task_code='{task}' AND car_number='{car}' "
            f"AND create_time>='{start}' AND create_time<='{end}' "
            f"AND id IN ({id_list});"
        )
        assert_safe_sql(sql_i)
        lines.append(sql_i)
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def _sql_int_or_null(value: Any) -> str:
    if value is None or value == "":
        return "NULL"
    return str(int(value))


def latest_backup(
    weight_bill_no: str,
    day: str,
    kind: str = "volume",
) -> dict[str, Any] | None:
    items = []
    for x in _load_index():
        if x.get("weight_bill_no") != weight_bill_no or x.get("biz_date") != day:
            continue
        item_kind = x.get("kind") or "volume"
        if item_kind == kind:
            items.append(x)
    return items[-1] if items else None


def write_deduct_snapshot(
    card: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, str]:
    """First-write snapshot of deduct_weight only. Later writes keep this original."""
    ensure_dirs()
    stamp = datetime.now(TZ).strftime("%H%M%S")
    bill = str(card["weight_bill_no"]).replace("/", "_")
    day = str(card["biz_date"])
    folder = snapshot_dir(day)
    snap_path = folder / f"{bill}_deduct_{stamp}.json"
    rollback_path = folder / f"rollback_{bill}_deduct_{stamp}.sql"
    slim = [
        {
            "id": int(r["id"]),
            "deduct_weight": r.get("deduct_weight"),
        }
        for r in rows
        if r.get("deduct_weight") is not None
    ]
    payload = {
        "card_meta": {
            "weight_bill_no": card["weight_bill_no"],
            "biz_date": day,
            "task_code": card["task_code"],
            "car_number": card["car_number"],
            "plate": card.get("plate"),
            "kind": "deduct",
        },
        "rows": slim,
    }
    snap_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    rollback_path.write_text(build_deduct_rollback_sql(card, slim), encoding="utf-8")
    items = _load_index()
    items.append(
        {
            "weight_bill_no": card["weight_bill_no"],
            "biz_date": day,
            "task_code": card["task_code"],
            "kind": "deduct",
            "snapshot": str(snap_path),
            "rollback": str(rollback_path),
            "ts": datetime.now(TZ).isoformat(timespec="seconds"),
        }
    )
    _save_index(items)
    return {"snapshot": str(snap_path), "rollback": str(rollback_path)}


def build_deduct_rollback_sql(card: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    task = card["task_code"]
    car = card["car_number"]
    day = card["biz_date"]
    start, end = f"{day} 00:00:00", f"{day} 23:59:59"
    ids = [int(r["id"]) for r in rows]
    lines = ["-- 仅 UPDATE 扣杂，不含 DELETE", "START TRANSACTION;"]
    if ids:
        cases = " ".join(
            f"WHEN {int(r['id'])} THEN {_sql_float_or_null(r.get('deduct_weight'))}" for r in rows
        )
        id_list = ",".join(str(i) for i in ids)
        sql = (
            "UPDATE slzj.intelli_task_check SET deduct_weight = CASE id "
            f"{cases} ELSE deduct_weight END "
            f"WHERE task_code='{task}' AND car_number='{car}' "
            f"AND create_time>='{start}' AND create_time<='{end}' "
            f"AND id IN ({id_list});"
        )
        assert_safe_sql(sql)
        lines.append(sql)
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def _sql_float_or_null(value: Any) -> str:
    if value is None or value == "":
        return "NULL"
    return f"{float(value):.4f}"


def load_snapshot(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def insert_columns() -> tuple[str, ...]:
    return tuple(c for c in CHECK_COLUMNS if c != "id")
