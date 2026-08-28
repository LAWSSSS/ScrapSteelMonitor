from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pymysql.err import OperationalError

from monitor.db import (
    SCAN_COLUMNS,
    _open_connection,
    format_db_error,
    group_check_rows,
    is_retryable_db_error,
)


class RetryableTests(unittest.TestCase):
    def test_2013_is_retryable(self) -> None:
        self.assertTrue(is_retryable_db_error(OperationalError(2013, "Lost connection")))
        self.assertTrue(is_retryable_db_error(OperationalError(2006, "MySQL server has gone away")))
        self.assertFalse(is_retryable_db_error(OperationalError(1045, "Access denied")))

    def test_friendly_message(self) -> None:
        msg = format_db_error(OperationalError(2013, "Lost connection to MySQL server during query"))
        self.assertIn("连库中断", msg)
        self.assertIn("握手", msg)
        self.assertNotIn("Lost connection", msg)

    def test_open_retries_2013_then_ok(self) -> None:
        fake = object()
        calls = {"n": 0}

        def boom(**_kwargs: object) -> object:
            calls["n"] += 1
            if calls["n"] < 3:
                raise OperationalError(2013, "Lost connection")
            return fake

        settings = {
            "host": "127.0.0.1",
            "port": 3306,
            "user": "u",
            "password": "p",
            "database": "d",
            "charset": "utf8mb4",
        }
        with patch("monitor.db.mysql_settings", return_value=settings), patch(
            "monitor.db.pymysql.connect", side_effect=boom
        ), patch("monitor.db.time.sleep"):
            conn = _open_connection(writable=True)
        self.assertIs(conn, fake)
        self.assertEqual(calls["n"], 3)

    def test_open_gives_up_after_three_2013(self) -> None:
        def boom(**_kwargs: object) -> object:
            raise OperationalError(2013, "Lost connection")

        settings = {
            "host": "127.0.0.1",
            "port": 3306,
            "user": "u",
            "password": "p",
            "database": "d",
            "charset": "utf8mb4",
        }
        with patch("monitor.db.mysql_settings", return_value=settings), patch(
            "monitor.db.pymysql.connect", side_effect=boom
        ), patch("monitor.db.time.sleep"):
            with self.assertRaises(OperationalError):
                _open_connection(writable=True)

    def test_scan_skips_images(self) -> None:
        self.assertNotIn("images", SCAN_COLUMNS)
        self.assertIn("total_volume", SCAN_COLUMNS)
        self.assertIn("is_hunliao", SCAN_COLUMNS)


class GroupRowsTests(unittest.TestCase):
    def test_group_by_task_and_car(self) -> None:
        rows = [
            {"id": 1, "task_code": "a", "car_number": "桂A", "total_volume": 1},
            {"id": 2, "task_code": "a", "car_number": "桂A", "total_volume": 2},
            {"id": 3, "task_code": "b", "car_number": "桂B", "total_volume": 3},
        ]
        grouped = group_check_rows(rows)
        self.assertEqual(len(grouped[("a", "桂A")]), 2)
        self.assertEqual(len(grouped[("b", "桂B")]), 1)


if __name__ == "__main__":
    unittest.main()
