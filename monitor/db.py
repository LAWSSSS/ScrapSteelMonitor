"""Read queries and guarded writes. Never emits DELETE."""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterator

import pymysql
from pymysql.cursors import DictCursor

from monitor.config import day_bounds, mysql_settings
from monitor.sql_guard import assert_safe_sql

logger = logging.getLogger(__name__)

RETRYABLE_ERRNOS = {2006, 2013, 2014}
_tls = threading.local()

CHECK_COLUMNS = (
    "id",
    "task_code",
    "flow_code",
    "car_number",
    "xiang_hao",
    "steel_type",
    "status",
    "create_time",
    "duration_time",
    "deduct_weight",
    "dangerous_goods",
    "images",
    "algorithm_id",
    "camera_id",
    "acc_time",
    "alarm_type",
    "acc_timestamp",
    "uuid",
    "station_number",
    "description",
    "steel_count",
    "total_volume",
    "total_volume_old",
    "deduct_weight_url",
    "origin_image_url",
    "chao_biao",
    "is_hunliao",
)
# 扫描不拉 images：text 最大 64KB × 每车几百行，VPN 上容易把连接撑断。
SCAN_COLUMNS = tuple(c for c in CHECK_COLUMNS if c != "images")


def _to_plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return value


def row_plain(row: dict[str, Any]) -> dict[str, Any]:
    return {k: _to_plain(v) for k, v in row.items()}


def is_retryable_db_error(exc: BaseException) -> bool:
    errno = exc.args[0] if getattr(exc, "args", None) else None
    if errno in RETRYABLE_ERRNOS:
        return True
    text = str(exc).lower()
    return "lost connection" in text or "gone away" in text


def format_db_error(exc: BaseException) -> str:
    if is_retryable_db_error(exc):
        return (
            "连库中断（MySQL 2013）。看板刷新不连库；失败发生在连内网 MySQL 握手。"
            "VPN 图标绿着也可能丢包。请动一下鼠标后再点扫描。"
        )
    return str(exc)


def _open_connection(*, writable: bool = False) -> pymysql.connections.Connection:
    """Handshake + optional read-only session. Retry 2013/2006 a few times."""
    settings = mysql_settings()
    last: BaseException | None = None
    for attempt in range(3):
        conn: pymysql.connections.Connection | None = None
        try:
            conn = pymysql.connect(
                host=str(settings["host"]),
                port=int(settings["port"]),
                user=str(settings["user"]),
                password=str(settings["password"]),
                database=str(settings["database"]),
                charset=str(settings["charset"]),
                cursorclass=DictCursor,
                autocommit=False,
                connect_timeout=20,
                read_timeout=180,
                write_timeout=180,
            )
            if not writable:
                with conn.cursor() as cur:
                    cur.execute("SET SESSION TRANSACTION READ ONLY")
            return conn
        except (pymysql.err.OperationalError, pymysql.err.InterfaceError) as exc:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            last = exc
            if not is_retryable_db_error(exc) or attempt >= 2:
                raise
            delay = 0.8 * (attempt + 1)
            logger.warning("握手第 %s 次失败，%.1fs 后重试: %s", attempt + 1, delay, exc)
            time.sleep(delay)
    assert last is not None
    raise last


@contextmanager
def connect(*, writable: bool = False) -> Iterator[pymysql.connections.Connection]:
    conn = _open_connection(writable=writable)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def session(*, writable: bool = False) -> Iterator[pymysql.connections.Connection]:
    """Reuse one connection for a whole scan. Cuts VPN handshake per SQL."""
    with connect(writable=writable) as conn:
        _tls.conn = conn
        try:
            yield conn
        finally:
            _tls.conn = None


def _active_conn() -> pymysql.connections.Connection | None:
    return getattr(_tls, "conn", None)


def _execute(
    conn: pymysql.connections.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    last: BaseException | None = None
    for attempt in range(3):
        try:
            conn.ping(reconnect=True)
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return list(cur.fetchall())
        except (pymysql.err.OperationalError, pymysql.err.InterfaceError) as exc:
            last = exc
            if not is_retryable_db_error(exc) or attempt >= 2:
                raise
            logger.warning("SQL 第 %s 次失败，重试: %s", attempt + 1, exc)
            time.sleep(0.5 * (attempt + 1))
    assert last is not None
    raise last


def fetch_all(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    assert_safe_sql(sql)
    conn = _active_conn()
    if conn is not None:
        return _execute(conn, sql, params)
    last: BaseException | None = None
    for attempt in range(3):
        try:
            with connect(writable=False) as opened:
                return _execute(opened, sql, params)
        except (pymysql.err.OperationalError, pymysql.err.InterfaceError) as exc:
            last = exc
            if not is_retryable_db_error(exc) or attempt >= 2:
                raise
            logger.warning("连库第 %s 次失败，重试: %s", attempt + 1, exc)
            time.sleep(0.5 * (attempt + 1))
    assert last is not None
    raise last


def load_manual_checks(day: date) -> list[dict[str, Any]]:
    start, end = day_bounds(day)
    return fetch_all(
        """
        SELECT id, weight_bill_no, flow_code, vehicle_no, operator_name,
               check_order, create_time, deduction
        FROM manual_check
        WHERE create_time >= %s AND create_time < %s
        ORDER BY weight_bill_no, create_time, id
        """,
        (start, end),
    )


def load_manual_details(check_ids: list[int]) -> list[dict[str, Any]]:
    if not check_ids:
        return []
    placeholders = ",".join(["%s"] * len(check_ids))
    return fetch_all(
        f"""
        SELECT id, manual_check_id, weight_bill_no, steel_type, steel_rate
        FROM manual_check_detail
        WHERE manual_check_id IN ({placeholders})
        """,
        tuple(check_ids),
    )


def load_task_by_flow(flow_code: str, day: date) -> dict[str, Any] | None:
    start, end = day_bounds(day)
    rows = fetch_all(
        """
        SELECT id, task_code, flow_code, car_number, create_time, check_complete_time,
               deduct_weight_rate, preprocess_add_value, calculation_rule
        FROM intelli_task_info
        WHERE flow_code = %s
          AND create_time >= %s AND create_time < %s
        ORDER BY create_time DESC, id DESC
        """,
        (flow_code, start, end),
    )
    if rows:
        return row_plain(rows[0])
    rows = fetch_all(
        """
        SELECT id, task_code, flow_code, car_number, create_time, check_complete_time,
               deduct_weight_rate, preprocess_add_value, calculation_rule
        FROM intelli_task_info
        WHERE flow_code = %s
        ORDER BY create_time DESC, id DESC
        LIMIT 3
        """,
        (flow_code,),
    )
    for row in rows:
        created = row["create_time"]
        if isinstance(created, datetime) and created.date() == day:
            return row_plain(row)
    return row_plain(rows[0]) if rows else None


def load_tasks_by_flows(flows: list[str], day: date) -> dict[str, dict[str, Any]]:
    """One lookup for all flow_codes of the day. Latest task per flow wins."""
    uniq = [f for f in dict.fromkeys(flows) if f]
    if not uniq:
        return {}
    start, end = day_bounds(day)
    placeholders = ",".join(["%s"] * len(uniq))
    rows = fetch_all(
        f"""
        SELECT id, task_code, flow_code, car_number, create_time, check_complete_time,
               deduct_weight_rate, preprocess_add_value, calculation_rule
        FROM intelli_task_info
        WHERE flow_code IN ({placeholders})
          AND create_time >= %s AND create_time < %s
        ORDER BY create_time DESC, id DESC
        """,
        (*uniq, start, end),
    )
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row["flow_code"])
        if key not in out:
            out[key] = row_plain(row)
    return out


def load_order_net_weights(bills: list[str]) -> dict[str, float]:
    """Latest net_weight (kg) per weight_bill_no from order_car."""
    uniq = [b for b in dict.fromkeys(bills) if b]
    if not uniq:
        return {}
    placeholders = ",".join(["%s"] * len(uniq))
    rows = fetch_all(
        f"""
        SELECT weight_bill_no, net_weight
        FROM order_car
        WHERE weight_bill_no IN ({placeholders})
        ORDER BY id DESC
        """,
        tuple(uniq),
    )
    out: dict[str, float] = {}
    for row in rows:
        key = str(row.get("weight_bill_no") or "")
        if key and key not in out and row.get("net_weight") is not None:
            out[key] = float(row["net_weight"])
    return out


def load_task_via_order_car(weight_bill_no: str, day: date) -> dict[str, Any] | None:
    oc = fetch_all(
        """
        SELECT flow_code, car_number, create_time
        FROM order_car
        WHERE weight_bill_no = %s
        ORDER BY id DESC
        LIMIT 1
        """,
        (weight_bill_no,),
    )
    if not oc or not oc[0].get("flow_code"):
        return None
    return load_task_by_flow(str(oc[0]["flow_code"]), day)


def group_check_rows(rows: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    by: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (str(row.get("task_code") or ""), str(row.get("car_number") or ""))
        by[key].append(row_plain(row) if "id" in row else row)
    return by


def load_day_check_rows(day: date) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """One full-table date window, then group in Python. Avoids N scans."""
    start, end = day_bounds(day)
    cols = ", ".join(SCAN_COLUMNS)
    rows = fetch_all(
        f"""
        SELECT {cols}
        FROM intelli_task_check
        WHERE create_time >= %s AND create_time < %s
        ORDER BY id
        """,
        (start, end),
    )
    return group_check_rows(rows)


def load_check_rows(
    task_code: str,
    car_number: str,
    day: date,
    *,
    include_images: bool = False,
) -> list[dict[str, Any]]:
    start, end = day_bounds(day)
    cols = ", ".join(CHECK_COLUMNS if include_images else SCAN_COLUMNS)
    rows = fetch_all(
        f"""
        SELECT {cols}
        FROM intelli_task_check
        WHERE task_code = %s
          AND car_number = %s
          AND create_time >= %s AND create_time < %s
        ORDER BY id
        """,
        (task_code, car_number, start, end),
    )
    return [row_plain(r) for r in rows]


def count_rows_by_car(car_numbers: list[str]) -> dict[str, int]:
    uniq = [c for c in dict.fromkeys(car_numbers) if c]
    if not uniq:
        return {}
    placeholders = ",".join(["%s"] * len(uniq))
    rows = fetch_all(
        f"""
        SELECT car_number, COUNT(*) AS cnt
        FROM intelli_task_check
        WHERE car_number IN ({placeholders})
        GROUP BY car_number
        """,
        tuple(uniq),
    )
    return {str(r["car_number"]): int(r["cnt"]) for r in rows}


def count_other_date_rows(car_number: str, task_code: str, day: date) -> int:
    start, end = day_bounds(day)
    rows = fetch_all(
        """
        SELECT COUNT(*) AS cnt
        FROM intelli_task_check
        WHERE car_number = %s
          AND (create_time < %s OR create_time >= %s)
        """,
        (car_number, start, end),
    )
    return int(rows[0]["cnt"]) if rows else 0


def execute_guarded(
    statements: list[tuple[str, tuple[Any, ...]]],
) -> list[int]:
    """Run UPDATE/INSERT in one transaction. Returns lastrowid for each INSERT."""
    insert_ids: list[int] = []
    with connect(writable=True) as conn:
        try:
            with conn.cursor() as cur:
                for sql, params in statements:
                    assert_safe_sql(sql)
                    cur.execute(sql, params)
                    if sql.lstrip().upper().startswith("INSERT"):
                        insert_ids.append(int(cur.lastrowid))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return insert_ids
