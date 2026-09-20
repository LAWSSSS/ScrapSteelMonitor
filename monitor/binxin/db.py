"""镔鑫 PostgreSQL 读写。复用 sql_guard，绝不发 DELETE。

和盛隆 monitor/db.py 同形状（session / fetch_all / execute_guarded），但驱动是 psycopg2，
占位符用 %s 时 psycopg2 也认，所以 SQL 写法可以保持一致。
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterator

import psycopg2
import psycopg2.extras

from monitor.binxin.config import pg_settings
from monitor.config import day_bounds
from monitor.sql_guard import assert_safe_sql

logger = logging.getLogger(__name__)

_tls = threading.local()

#: 逐抓斗表。images / check_result 这类大字段扫描时不拉。
CHECK_COLUMNS = (
    "id",
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
    "steel_count",
    "steel_rate",
    "total_volume",
    "total_volume_old",
    "old_total_volume",
    "origin_image_url",
    "images",
)
SCAN_COLUMNS = tuple(c for c in CHECK_COLUMNS if c not in ("images", "origin_image_url"))


def _to_plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return value


def row_plain(row: dict[str, Any]) -> dict[str, Any]:
    return {k: _to_plain(v) for k, v in row.items()}


def is_retryable_db_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        hint in text
        for hint in ("server closed", "connection reset", "terminating connection", "timeout")
    )


def format_db_error(exc: BaseException) -> str:
    if is_retryable_db_error(exc):
        return "连镔鑫库中断。看板刷新不连库；失败发生在握手。确认能通 172.31.1.102:5432 后再点扫描。"
    return str(exc)


def _open_connection(*, writable: bool = False) -> psycopg2.extensions.connection:
    settings = pg_settings()
    last: BaseException | None = None
    for attempt in range(3):
        conn: psycopg2.extensions.connection | None = None
        try:
            conn = psycopg2.connect(
                host=str(settings["host"]),
                port=int(settings["port"]),
                user=str(settings["user"]),
                password=str(settings["password"]),
                dbname=str(settings["dbname"]),
                connect_timeout=20,
                cursor_factory=psycopg2.extras.DictCursor,
            )
            conn.set_session(readonly=not writable, autocommit=False)
            return conn
        except psycopg2.Error as exc:
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
def connect(*, writable: bool = False) -> Iterator[psycopg2.extensions.connection]:
    conn = _open_connection(writable=writable)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def session(*, writable: bool = False) -> Iterator[psycopg2.extensions.connection]:
    """整轮扫描复用一条连接，省掉每条 SQL 的握手。"""
    with connect(writable=writable) as conn:
        _tls.conn = conn
        try:
            yield conn
        finally:
            _tls.conn = None


def _active_conn() -> psycopg2.extensions.connection | None:
    return getattr(_tls, "conn", None)


def _execute(
    conn: psycopg2.extensions.connection,
    sql: str,
    params: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        if cur.description is None:
            return []
        return [dict(r) for r in cur.fetchall()]


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
        except psycopg2.Error as exc:
            last = exc
            if not is_retryable_db_error(exc) or attempt >= 2:
                raise
            logger.warning("连库第 %s 次失败，重试: %s", attempt + 1, exc)
            time.sleep(0.5 * (attempt + 1))
    assert last is not None
    raise last


def load_day_cars(day: date) -> list[dict[str, Any]]:
    """当天完成检判的车。order_car 一行一车次，order_code 是运单号。"""
    start, end = day_bounds(day)
    rows = fetch_all(
        """
        SELECT car_number, order_code, flow_code, check_start_time,
               net_weight, supplier_name, station_number
        FROM order_car
        WHERE check_start_time >= %s AND check_start_time < %s
          AND flow_code IS NOT NULL AND order_code IS NOT NULL
        ORDER BY check_start_time DESC
        """,
        (start, end),
    )
    return [row_plain(r) for r in rows]


def load_steel_type_stats(flows: list[str]) -> dict[str, list[dict[str, Any]]]:
    """AI 汇总占比。steel_volume 才是原始体积，steel_rate 只有 4 位小数会带偏 0.02pp。"""
    uniq = [f for f in dict.fromkeys(flows) if f]
    if not uniq:
        return {}
    rows = fetch_all(
        """
        SELECT flow_code, task_code, steel_type, steel_level, steel_volume, steel_rate, steel_weight
        FROM steel_type_stat
        WHERE flow_code = ANY(%s)
        """,
        (uniq,),
    )
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(str(row["flow_code"]), []).append(row_plain(row))
    return out


def load_manual_details(bills: list[str]) -> dict[str, list[dict[str, Any]]]:
    """人工评级明细。一车通常两条 grading_method，最终结果是它们的算术平均。

    manual_check 表已停更，不要用。
    """
    uniq = [b for b in dict.fromkeys(bills) if b]
    if not uniq:
        return {}
    rows = fetch_all(
        """
        SELECT waybill_no, grading_method, material_name, level, ratio, create_time
        FROM material_detail_info
        WHERE waybill_no = ANY(%s)
        ORDER BY waybill_no, grading_method, level
        """,
        (uniq,),
    )
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(str(row["waybill_no"]), []).append(row_plain(row))
    return out


def load_check_rows(
    flow_code: str,
    *,
    include_images: bool = False,
) -> list[dict[str, Any]]:
    """一车的逐抓斗记录。steel_type_stat.steel_volume 就是这里 total_volume 的分组求和。"""
    cols = ", ".join(CHECK_COLUMNS if include_images else SCAN_COLUMNS)
    rows = fetch_all(
        f"""
        SELECT {cols}
        FROM intelli_task_check
        WHERE flow_code = %s
        ORDER BY id
        """,
        (flow_code,),
    )
    return [row_plain(r) for r in rows]


def count_grabs(flows: list[str]) -> dict[str, int]:
    uniq = [f for f in dict.fromkeys(flows) if f]
    if not uniq:
        return {}
    rows = fetch_all(
        """
        SELECT flow_code, COUNT(*) AS cnt
        FROM intelli_task_check
        WHERE flow_code = ANY(%s)
        GROUP BY flow_code
        """,
        (uniq,),
    )
    return {str(r["flow_code"]): int(r["cnt"]) for r in rows}


def execute_guarded(statements: list[tuple[str, tuple[Any, ...]]]) -> None:
    """一个事务里跑完所有 UPDATE/INSERT。任一条失败就整体回滚。"""
    with connect(writable=True) as conn:
        try:
            with conn.cursor() as cur:
                for sql, params in statements:
                    assert_safe_sql(sql)
                    cur.execute(sql, params)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
