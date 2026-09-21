"""写库封锁闸的单测。不连库。

最关键的一条是 test_writable_connection_blocked_when_locked：只要闸没开，任何想开可写连接的
调用都在建连接之前就抛，所以不存在"改了一半"的可能。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from monitor.binxin import guard
from monitor.config import TZ


class GuardTestCase(unittest.TestCase):
    """每个用例换一个临时 STATE_ROOT，互不干扰，也不会写到真的状态目录。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        patcher = mock.patch.object(guard, "STATE_ROOT", root)
        patcher.start()
        self.addCleanup(patcher.stop)
        ensure = mock.patch.object(guard, "ensure_dirs", lambda: None)
        ensure.start()
        self.addCleanup(ensure.stop)
        self.addCleanup(self._tmp.cleanup)
        self.root = root

    def _write_arm(self, until: datetime) -> None:
        path = guard.arm_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"armed_until": until.isoformat(timespec="seconds")}), encoding="utf-8"
        )

    def _env(self, ok: bool):
        value = guard.WRITE_TOKEN if ok else ""
        return mock.patch.dict("os.environ", {guard.WRITE_ENV: value}, clear=False)


class DefaultStateTests(GuardTestCase):
    def test_locked_by_default(self) -> None:
        with self._env(False):
            state = guard.status()
        self.assertFalse(state["allowed"])
        self.assertEqual(len(state["blockers"]), 2)

    def test_assert_raises_when_locked(self) -> None:
        with self._env(False):
            with self.assertRaises(guard.WriteLocked) as ctx:
                guard.assert_write_allowed()
        self.assertIn("没有改动任何数据", str(ctx.exception))

    def test_error_names_both_blockers(self) -> None:
        with self._env(False):
            with self.assertRaises(guard.WriteLocked) as ctx:
                guard.assert_write_allowed()
        text = str(ctx.exception)
        self.assertIn(guard.WRITE_ENV, text)
        self.assertIn("解锁文件", text)


class EnvTests(GuardTestCase):
    def test_exact_token_required(self) -> None:
        self.assertFalse(guard.env_ok({guard.WRITE_ENV: "1"}))
        self.assertFalse(guard.env_ok({guard.WRITE_ENV: "true"}))
        self.assertFalse(guard.env_ok({guard.WRITE_ENV: "YES-I-APPROVE"}))
        self.assertTrue(guard.env_ok({guard.WRITE_ENV: guard.WRITE_TOKEN}))

    def test_env_alone_is_not_enough(self) -> None:
        with self._env(True):
            self.assertFalse(guard.status()["allowed"])
            with self.assertRaises(guard.WriteLocked):
                guard.assert_write_allowed()

    def test_file_alone_is_not_enough(self) -> None:
        self._write_arm(datetime.now(TZ) + timedelta(minutes=10))
        with self._env(False):
            self.assertFalse(guard.status()["allowed"])
            with self.assertRaises(guard.WriteLocked):
                guard.assert_write_allowed()


class ArmTests(GuardTestCase):
    def test_both_conditions_open_the_gate(self) -> None:
        guard.arm(10)
        with self._env(True):
            self.assertTrue(guard.status()["allowed"])
            guard.assert_write_allowed()

    def test_expired_arm_locks_again(self) -> None:
        self._write_arm(datetime.now(TZ) - timedelta(seconds=1))
        with self._env(True):
            state = guard.status()
        self.assertFalse(state["allowed"])
        self.assertTrue(any("过期" in b for b in state["blockers"]))

    def test_arm_minutes_are_capped(self) -> None:
        payload = guard.arm(9999)
        self.assertEqual(payload["minutes"], guard.MAX_ARM_MINUTES)

    def test_arm_minutes_floor_is_one(self) -> None:
        self.assertEqual(guard.arm(0)["minutes"], 1)

    def test_disarm_locks_immediately(self) -> None:
        guard.arm(60)
        guard.disarm()
        with self._env(True):
            self.assertFalse(guard.status()["allowed"])

    def test_corrupt_arm_file_is_treated_as_locked(self) -> None:
        path = guard.arm_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")
        with self._env(True):
            self.assertFalse(guard.status()["allowed"])

    def test_seconds_left_counts_down(self) -> None:
        guard.arm(30)
        with self._env(True):
            left = guard.status()["seconds_left"]
        self.assertGreater(left, 29 * 60)
        self.assertLessEqual(left, 30 * 60)


class ConnectionChokepointTests(GuardTestCase):
    """闸挂在 _open_connection 上，所以写路径不用各自记得调 assert。"""

    def test_writable_connection_blocked_when_locked(self) -> None:
        from monitor.binxin import db

        with self._env(False):
            with mock.patch("psycopg2.connect") as connect:
                with self.assertRaises(guard.WriteLocked):
                    db._open_connection(writable=True)
                connect.assert_not_called()

    def test_readonly_connection_needs_no_arm(self) -> None:
        from monitor.binxin import db

        with self._env(False):
            with mock.patch("psycopg2.connect") as connect:
                with mock.patch.object(db, "pg_settings", return_value={
                    "host": "h", "port": 1, "user": "u", "password": "p", "dbname": "d",
                }):
                    db._open_connection(writable=False)
                connect.assert_called_once()

    def test_execute_guarded_blocked_when_locked(self) -> None:
        from monitor.binxin import db

        with self._env(False):
            with mock.patch("psycopg2.connect") as connect:
                with self.assertRaises(guard.WriteLocked):
                    db.execute_guarded([("UPDATE intelli_task_check SET total_volume=1", ())])
                connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
