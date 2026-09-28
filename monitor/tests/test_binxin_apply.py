"""镔鑫落库链路单测。全部用假连接和临时目录，一条 SQL 都不会真的发给镔鑫库。

写库的真实执行只在 FakeDb 里发生，断言的是"生成了什么语句"和"语句是否安全"，而不是
"库里变成了什么"。真库验证留到人工批准之后。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from monitor.binxin import apply as bx_apply
from monitor.binxin import backup, deduct, restore, scan, store
from monitor.binxin.correct import plan_grabs
from monitor.binxin.materials import JINGLU, ZHONGFEI_KEY
from monitor.sql_guard import assert_safe_sql

IMG = "http://172.31.1.102:19000/scrape-steel/2026-09-19/abc_render.jpg"
ORIG = "http://172.31.1.102:19000/scrape-steel/2026-09-19/abc_origin.jpg"
FLOW = "flow-abc-123"
DAY = "2026-09-19"


def _check_row(row_id: int, uid: str, st: int, lv: int, vol: float, deduct=None) -> dict:
    return {
        "id": row_id,
        "uuid": uid,
        "task_code": "task-1",
        "flow_code": FLOW,
        "car_number": "苏GC7733",
        "steel_type": st,
        "steel_level": lv,
        "status": 2,
        "create_time": "2026-09-19 10:13:06",
        "duration_time": 3,
        "check_type": 1,
        "check_method": None,
        "station_number": 4,
        "algorithm_id": 7,
        "camera_id": 2,
        "acc_timestamp": 1789783379,
        "acc_time": 1789783379625,
        "total_volume": vol,
        "old_total_volume": vol,
        "images": IMG,
        "origin_image_url": ORIG,
        "deduct_weight": deduct,
    }


def _check_rows() -> list[dict]:
    rows, next_id = [], 1
    layout = (
        ("u1", ((1, 1, 500.0), (1, 2, 300.0), (3, 1, 40.0))),
        ("u2", ((1, 1, 420.0), (1, 2, 380.0), (3, 1, 30.0), (1, 3, 10.0))),
        ("u3", ((1, 1, 300.0), (1, 2, 260.0))),
        ("u4", ((1, 1, 280.0), (1, 2, 240.0), (3, 1, 20.0))),
    )
    for uid, entries in layout:
        first = True
        for st, lv, vol in entries:
            rows.append(_check_row(next_id, uid, st, lv, vol, 100.0 if first else None))
            next_id += 1
            first = False
    return rows


def _stat_rows() -> list[dict]:
    """与 _check_rows 的体积一致的汇总行。"""
    return [
        {"id": 900, "flow_code": FLOW, "task_code": "task-1", "car_number": "苏GC7733",
         "station_number": 4, "data_type": 1, "steel_type": 1, "steel_level": 1,
         "steel_rate": 0.4636, "steel_volume": 1500.0, "steel_weight": 15.6,
         "create_time": "2026-09-19 11:29:03"},
        {"id": 901, "flow_code": FLOW, "task_code": "task-1", "car_number": "苏GC7733",
         "station_number": 4, "data_type": 1, "steel_type": 1, "steel_level": 2,
         "steel_rate": 0.3648, "steel_volume": 1180.0, "steel_weight": 12.3,
         "create_time": "2026-09-19 11:29:03"},
        {"id": 902, "flow_code": FLOW, "task_code": "task-1", "car_number": "苏GC7733",
         "station_number": 4, "data_type": 1, "steel_type": 3, "steel_level": 1,
         "steel_rate": 0.0278, "steel_volume": 90.0, "steel_weight": 0.9,
         "create_time": "2026-09-19 11:29:03"},
        {"id": 903, "flow_code": FLOW, "task_code": "task-1", "car_number": "苏GC7733",
         "station_number": 4, "data_type": 1, "steel_type": 1, "steel_level": 3,
         "steel_rate": 0.0031, "steel_volume": 10.0, "steel_weight": 0.1,
         "create_time": "2026-09-19 11:29:03"},
    ]


def _manual_rows(main_pct: float = 0.95) -> list[dict]:
    """人工两次评级，主料型都是精炉料2。"""
    rest = round(1.0 - main_pct, 4)
    out = []
    for method in (1, 2):
        out.append({"waybill_no": "wb-1", "grading_method": method,
                    "material_name": "精炉料", "level": 2, "ratio": main_pct})
        out.append({"waybill_no": "wb-1", "grading_method": method,
                    "material_name": "精炉料", "level": 1, "ratio": rest})
    return out


def _set_clause(sql: str) -> str:
    return sql.split(" SET ", 1)[1].split(" WHERE ", 1)[0]


def _deduct_row() -> dict:
    """AI 扣重现状：原始 3kg、主料型精炉料2（常数 250），人工 300kg，差 47kg 已合格。

    这辆车在占比上要从精炉料1改成精炉料2，常数会从 165 变 250，所以扣重必须跟着重写。
    """
    return {
        "flow_code": FLOW,
        "car_number": "苏GC7733",
        "deduct_weight": 0.003,
        "deduct_weight_rate": 0.0001,
        "modified_deduct_weight": 168.0,
        "modified_deduct_rate": 0.005,
        "calculated_deduct_weight": 168.0,
        "final_deduct": 168.0,
        "qigezha_hanzha_weight": 0.0,
        "weather_deduct_weight": 0.0,
        "net_weight": 33.66,
        "create_time": "2026-09-19 11:29:03",
    }


def _card() -> dict:
    return {
        "flow_code": FLOW,
        "task_code": "task-1",
        "order_code": "wb-1",
        "car_number": "苏GC7733",
        "biz_date": DAY,
        "net_weight": 33.66,
        "station_number": 4,
        "target": "精炉料2",
        "target_key": [1, 2],
        "target_raw_pct": 64.5,
        "human_main_pct": 95.0,
        "status": "pending",
    }


class FakeDb:
    """假库。会真的把语句作用到自己的状态上，所以能验证"语句产出的库状态对不对"。

    只认 apply.py 实际会发出的四种语句。收到没见过的形状就直接失败，这样以后改了 SQL
    形状而忘了改这里时，测试会报错而不是悄悄放过。
    """

    def __init__(self) -> None:
        self.check_rows = _check_rows()
        self.stat_rows = _stat_rows()
        self.deduct_row = _deduct_row()
        self.human_deduct_kg: float | None = 300.0
        self.executed: list[tuple[str, tuple]] = []
        self.next_check_id = 5000
        self.next_stat_id = 9000
        self.frozen_stat: list[dict] | None = None

    def load_check_rows(self, flow, *, include_images=False):
        return [dict(r) for r in self.check_rows]

    def reread_stat_rows(self, flow):
        if self.frozen_stat is not None and self.executed:
            return [dict(r) for r in self.frozen_stat]
        return [dict(r) for r in self.stat_rows]

    def load_manual_details(self, bills):
        return {"wb-1": _manual_rows()}

    def load_deduct_rows(self, flows):
        return {FLOW: dict(self.deduct_row)} if self.deduct_row else {}

    def load_manual_deducts(self, bills):
        if self.human_deduct_kg is None:
            return {}
        return {"wb-1": {"waybill_no": "wb-1", "deduction_tonnage": self.human_deduct_kg}}

    def execute_guarded(self, statements):
        new_ids: list[int] = []
        for sql, params in statements:
            assert_safe_sql(sql)
            self.executed.append((sql, params))
            new_ids.extend(self._run(sql, params))
        return new_ids

    def _run(self, sql: str, params: tuple) -> list[int]:
        if sql.startswith("UPDATE intelli_task_check SET total_volume = CASE id"):
            self._apply_volume_case(params)
            return []
        if sql.startswith("INSERT INTO intelli_task_check"):
            row = dict(zip(bx_apply.INSERT_COLUMNS, params))
            row["id"] = self.next_check_id
            self.next_check_id += 1
            self.check_rows.append(row)
            return [row["id"]]
        if sql.startswith("UPDATE steel_type_stat SET"):
            rate, volume, weight, row_id, _flow = params
            for row in self.stat_rows:
                if int(row["id"]) == int(row_id):
                    row.update(
                        steel_rate=rate, steel_volume=float(volume), steel_weight=float(weight)
                    )
            return []
        if sql.startswith("INSERT INTO steel_type_stat"):
            cols = (
                "flow_code", "task_code", "car_number", "station_number", "data_type",
                "create_time", "steel_type", "steel_level",
                *bx_apply.STAT_VALUE_COLUMNS,
            )
            row = dict(zip(cols, params))
            row["id"] = self.next_stat_id
            self.next_stat_id += 1
            self.stat_rows.append(row)
            return []
        if sql.startswith("UPDATE intelli_task_info SET"):
            cols = [seg.split(" = ")[0].strip() for seg in _set_clause(sql).split(", ")]
            self.deduct_row.update(dict(zip(cols, params[: len(cols)])))
            return []
        raise AssertionError(f"假库不认识这条语句：{sql[:80]}")

    def _apply_volume_case(self, params: tuple) -> None:
        """params = [id, vol] * n + [flow] + ids，取前 2n 段。"""
        tail = len(params) - 1
        while tail > 0 and not isinstance(params[tail], str):
            tail -= 1
        pairs = params[:tail]
        by_id = {int(pairs[i]): float(pairs[i + 1]) for i in range(0, len(pairs), 2)}
        for row in self.check_rows:
            if int(row["id"]) in by_id:
                row["total_volume"] = by_id[int(row["id"])]


class ApplyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        for module in (store, backup):
            patcher = mock.patch.object(module, "STATE_ROOT", root)
            patcher.start()
            self.addCleanup(patcher.stop)
            noop = mock.patch.object(module, "ensure_dirs", lambda: None)
            noop.start()
            self.addCleanup(noop.stop)
        self.fake = FakeDb()
        db_patch = mock.patch.object(bx_apply, "db", self.fake)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        store.save_pending(_card())

    def root_backups(self) -> Path:
        return Path(self._tmp.name) / "backups"


class StatementTests(ApplyTestCase):
    def test_volume_update_is_scoped_to_flow(self) -> None:
        stmts = bx_apply._volume_statements(FLOW, {1: 10.0, 2: 20.0})
        sql, params = stmts[0]
        self.assertIn("WHERE flow_code = %s", sql)
        self.assertIn(FLOW, params)
        assert_safe_sql(sql)

    def test_volume_update_is_one_statement(self) -> None:
        self.assertEqual(len(bx_apply._volume_statements(FLOW, {i: 1.0 for i in range(20)})), 1)

    def test_no_volume_no_statement(self) -> None:
        self.assertEqual(bx_apply._volume_statements(FLOW, {}), [])

    def test_insert_returns_id(self) -> None:
        plan = plan_grabs(_check_rows(), (JINGLU, 3), 55.0, seed="s")
        self.assertTrue(plan.ok, plan.reason)
        stmts = bx_apply._insert_statements(plan.inserts)
        self.assertTrue(stmts)
        for sql, _params in stmts:
            self.assertIn("RETURNING id", sql)
            assert_safe_sql(sql)

    def test_insert_refuses_row_carrying_deduct(self) -> None:
        bad = {"images": IMG, "deduct_weight": 100.0, "uuid": "u1"}
        with self.assertRaises(bx_apply.ApproveRefused) as ctx:
            bx_apply._insert_statements([bad])
        self.assertIn("deduct_weight", str(ctx.exception))

    def test_insert_refuses_row_without_image(self) -> None:
        with self.assertRaises(bx_apply.ApproveRefused):
            bx_apply._insert_statements([{"uuid": "u1", "images": None}])

    def test_stat_update_matches_by_type_and_level(self) -> None:
        wanted = [{"steel_type": 1, "steel_level": 2, "steel_rate": 0.645,
                   "steel_volume": 1000.0, "steel_weight": 21.7}]
        stmts = bx_apply._stat_statements(FLOW, _stat_rows(), wanted)
        updates = [s for s in stmts if s[0].startswith("UPDATE")]
        self.assertTrue(any(901 in s[1] for s in updates))

    def test_stat_inserts_missing_key(self) -> None:
        wanted = [{"steel_type": 4, "steel_level": 1, "steel_rate": 0.01,
                   "steel_volume": 10.0, "steel_weight": 0.3}]
        stmts = bx_apply._stat_statements(FLOW, _stat_rows(), wanted)
        self.assertTrue(any(s[0].startswith("INSERT") for s in stmts))

    def test_stat_zeroes_vanished_key(self) -> None:
        """料型从结果里消失时把汇总行置零，不能留着旧占比。"""
        wanted = [{"steel_type": 1, "steel_level": 1, "steel_rate": 1.0,
                   "steel_volume": 2780.0, "steel_weight": 33.66}]
        stmts = bx_apply._stat_statements(FLOW, _stat_rows(), wanted)
        zeroed = [s for s in stmts if s[1][:3] == (0, 0, 0)]
        self.assertEqual(len(zeroed), 3)

    def test_stat_refuses_when_no_existing_rows(self) -> None:
        with self.assertRaises(bx_apply.ApproveRefused):
            bx_apply._stat_statements(FLOW, [], [{"steel_type": 1, "steel_level": 1,
                                                  "steel_rate": 1.0, "steel_volume": 1.0,
                                                  "steel_weight": 1.0}])

    def test_new_stat_row_reuses_data_type_and_create_time(self) -> None:
        wanted = [{"steel_type": 4, "steel_level": 1, "steel_rate": 0.01,
                   "steel_volume": 10.0, "steel_weight": 0.3}]
        stmts = bx_apply._stat_statements(FLOW, _stat_rows(), wanted)
        insert = next(s for s in stmts if s[0].startswith("INSERT"))
        self.assertIn(1, insert[1])
        self.assertIn("2026-09-19 11:29:03", insert[1])

    def test_all_statements_pass_sql_guard(self) -> None:
        plan = plan_grabs(_check_rows(), (JINGLU, 2), 64.5, seed="s", human_pct=95.0)
        self.assertTrue(plan.ok, plan.reason)
        wanted = [{"steel_type": 1, "steel_level": 2, "steel_rate": 0.645,
                   "steel_volume": 1000.0, "steel_weight": 21.7}]
        for sql, _params in bx_apply.build_statements(FLOW, plan, _stat_rows(), wanted):
            assert_safe_sql(sql)


class ReplanTests(ApplyTestCase):
    def test_replan_uses_live_rows(self) -> None:
        live = bx_apply.replan_live(_card())
        self.assertTrue(live["plan"].ok)
        self.assertEqual(len(live["check_rows"]), len(_check_rows()))

    def test_replan_refuses_when_already_in_band(self) -> None:
        """人工降到与 AI 展示一致时不该再改。"""
        self.fake.load_manual_details = lambda bills: {
            "wb-1": [
                {"waybill_no": "wb-1", "grading_method": m, "material_name": "精炉料",
                 "level": 1, "ratio": 0.90}
                for m in (1, 2)
            ]
        }
        card = {**_card(), "target_key": [1, 1], "human_main_pct": 90.0}
        with self.assertRaises(bx_apply.ApproveRefused) as ctx:
            bx_apply.replan_live(card)
        self.assertIn("已经在带内", str(ctx.exception))

    def test_replan_refuses_when_human_main_changed(self) -> None:
        self.fake.load_manual_details = lambda bills: {
            "wb-1": [
                {"waybill_no": "wb-1", "grading_method": m, "material_name": "重废",
                 "level": 1, "ratio": 1.0}
                for m in (1, 2)
            ]
        }
        with self.assertRaises(bx_apply.ApproveRefused) as ctx:
            bx_apply.replan_live(_card())
        self.assertIn("人工主料型已变成", str(ctx.exception))

    def test_replan_refuses_empty_check_rows(self) -> None:
        self.fake.load_check_rows = lambda flow, include_images=False: []
        with self.assertRaises(bx_apply.ApproveRefused):
            bx_apply.replan_live(_card())

    def test_replan_refuses_empty_stat_rows(self) -> None:
        self.fake.reread_stat_rows = lambda flow: []
        with self.assertRaises(bx_apply.ApproveRefused):
            bx_apply.replan_live(_card())


class ApproveTests(ApplyTestCase):
    def test_write_actually_lands_in_band(self) -> None:
        """端到端：语句作用到假库后，回读重算必须真的进带。"""
        result = bx_apply.approve(FLOW, DAY)
        self.assertTrue(result["in_band"], result["after"])
        self.assertTrue(result["after"]["main_is_target"])
        self.assertAlmostEqual(result["after"]["raw_pct"], 64.5, delta=0.4)

    def test_check_rows_and_stat_stay_consistent(self) -> None:
        """写完汇总表必须还等于逐抓斗体积的分组求和，否则两张表会互相打架。"""
        bx_apply.approve(FLOW, DAY)
        from monitor.binxin.correct import car_key_pcts
        from monitor.binxin.scan import ai_raw_pcts_of

        from_checks = car_key_pcts(self.fake.check_rows)
        from_stat = ai_raw_pcts_of(self.fake.stat_rows)
        for key, pct in from_checks.items():
            self.assertAlmostEqual(from_stat.get(key, 0.0), pct, delta=0.02)

    def test_total_volume_conserved_in_db(self) -> None:
        before = sum(float(r["total_volume"]) for r in _check_rows())
        bx_apply.approve(FLOW, DAY)
        after = sum(float(r["total_volume"] or 0) for r in self.fake.check_rows)
        self.assertAlmostEqual(before, after, delta=before * 1e-6)

    def test_inserted_rows_carry_image_and_no_deduct(self) -> None:
        bx_apply.approve(FLOW, DAY)
        for row in self.fake.check_rows:
            if int(row["id"]) >= 5000:
                self.assertEqual(row["images"], IMG)
                self.assertIsNone(row.get("deduct_weight"))

    def test_deduct_total_unchanged_in_db(self) -> None:
        before = sum(float(r["deduct_weight"] or 0) for r in _check_rows())
        bx_apply.approve(FLOW, DAY)
        after = sum(float(r.get("deduct_weight") or 0) for r in self.fake.check_rows)
        self.assertAlmostEqual(before, after)

    def test_stat_weight_matches_rate_times_net_weight(self) -> None:
        bx_apply.approve(FLOW, DAY)
        for row in self.fake.stat_rows:
            if float(row["steel_rate"]) > 0:
                self.assertAlmostEqual(
                    float(row["steel_weight"]), float(row["steel_rate"]) * 33.66, places=4
                )

    def test_approve_writes_snapshot_before_statements(self) -> None:
        result = bx_apply.approve(FLOW, DAY)
        snap = Path(result["backup"]["snapshot"])
        self.assertTrue(snap.is_file())
        self.assertTrue(Path(result["backup"]["rollback"]).is_file())

    def test_approve_clears_pending_and_marks_processed(self) -> None:
        bx_apply.approve(FLOW, DAY)
        self.assertIsNone(store.load_pending(FLOW, DAY))
        self.assertTrue(store.is_processed(FLOW, DAY))

    def test_approve_records_insert_ids_for_rollback(self) -> None:
        result = bx_apply.approve(FLOW, DAY)
        rec = store.load_processed()[store.ticket_key(FLOW, DAY)]
        self.assertEqual(rec["insert_ids"], result["insert_ids"])

    def test_approve_missing_card(self) -> None:
        with self.assertRaises(FileNotFoundError):
            bx_apply.approve("no-such-flow", DAY)

    def test_rollback_script_covers_inserted_ids(self) -> None:
        result = bx_apply.approve(FLOW, DAY)
        text = Path(result["backup"]["rollback"]).read_text(encoding="utf-8")
        for new_id in result["insert_ids"]:
            self.assertIn(str(new_id), text)

    def test_rollback_script_has_no_delete(self) -> None:
        result = bx_apply.approve(FLOW, DAY)
        text = Path(result["backup"]["rollback"]).read_text(encoding="utf-8")
        statements = [
            line for line in text.splitlines() if line.strip() and not line.startswith("--")
        ]
        self.assertTrue(statements)
        for line in statements:
            self.assertNotIn("DELETE", line.upper())
        self.assertIn("BEGIN;", text)
        self.assertIn("COMMIT;", text)

    def test_approve_reports_off_band_result(self) -> None:
        """写完回读发现没进带时要如实报出来，而不是假装成功。"""
        self.fake.frozen_stat = [
            dict(r, steel_volume=v) for r, v in zip(_stat_rows(), (2600.0, 100.0, 50.0, 30.0))
        ]
        result = bx_apply.approve(FLOW, DAY)
        self.assertFalse(result["in_band"])
        self.assertTrue(Path(result["backup"]["rollback"]).is_file())

    def test_reject_touches_no_sql(self) -> None:
        bx_apply.reject(FLOW, DAY)
        self.assertEqual(self.fake.executed, [])
        self.assertIsNone(store.load_pending(FLOW, DAY))


class DeductWriteTests(ApplyTestCase):
    """扣重跟着占比一起改。主料型从精炉料1变精炉料2，常数从 165 变 250。"""

    def test_deduct_row_is_rewritten(self) -> None:
        result = bx_apply.approve(FLOW, DAY)
        self.assertTrue(result["deduct_written"])
        self.assertEqual(
            sum(1 for sql, _p in self.fake.executed if "intelli_task_info" in sql), 1
        )

    def test_modified_equals_raw_plus_new_constant(self) -> None:
        """页面上 原始扣杂 + 常数 = 修正扣杂。常数必须用改完之后的主料型那一档。"""
        bx_apply.approve(FLOW, DAY)
        row = self.fake.deduct_row
        raw_kg = float(row["deduct_weight"]) * 1000.0
        self.assertAlmostEqual(
            float(row["modified_deduct_weight"]), raw_kg + 250.0, places=2
        )

    def test_final_lands_in_the_pass_window(self) -> None:
        bx_apply.approve(FLOW, DAY)
        self.assertTrue(
            deduct.is_ok(float(self.fake.deduct_row["final_deduct"]), 300.0),
            self.fake.deduct_row,
        )

    def test_verify_reports_rule_and_pass(self) -> None:
        result = bx_apply.approve(FLOW, DAY)
        after = result["after"]["deduct"]
        self.assertTrue(after["follows_rule"], after)
        self.assertTrue(after["ok"], after)
        self.assertEqual(after["additional_kg"], 250.0)

    def test_raw_stays_under_the_cap(self) -> None:
        bx_apply.approve(FLOW, DAY)
        raw_kg = float(self.fake.deduct_row["deduct_weight"]) * 1000.0
        self.assertLessEqual(raw_kg, deduct.RAW_DEDUCT_CAP_KG)

    def test_rates_move_with_the_weights(self) -> None:
        """重量改了占比没动，页面上「修正扣杂占比」会和重量对不上。"""
        before = dict(self.fake.deduct_row)
        bx_apply.approve(FLOW, DAY)
        after = self.fake.deduct_row
        self.assertNotEqual(after["modified_deduct_rate"], before["modified_deduct_rate"])
        self.assertNotEqual(after["deduct_weight_rate"], before["deduct_weight_rate"])

    def test_main_type_change_writes_constant_when_human_band_is_unreachable(self) -> None:
        """上界 200 < 新常数 250：扣重进不了合格区间，占比仍改，扣重按新常数重写。"""
        self.fake.human_deduct_kg = 100.0
        result = bx_apply.approve(FLOW, DAY)
        self.assertTrue(result["deduct_written"])
        row = self.fake.deduct_row
        raw_kg = float(row["deduct_weight"]) * 1000.0
        self.assertAlmostEqual(float(row["modified_deduct_weight"]), raw_kg + 250.0, places=2)
        after = result["after"]["deduct"]
        self.assertTrue(after["follows_rule"], after)
        self.assertFalse(after["ok"], after)

    def test_snapshot_keeps_the_original_deduct_row(self) -> None:
        before = dict(self.fake.deduct_row)
        result = bx_apply.approve(FLOW, DAY)
        snap = backup.load_snapshot(result["backup"]["snapshot"])
        self.assertAlmostEqual(
            float(snap["deduct_row"]["modified_deduct_weight"]),
            float(before["modified_deduct_weight"]),
            places=2,
        )

    def test_rollback_script_restores_the_deduct_row(self) -> None:
        result = bx_apply.approve(FLOW, DAY)
        text = Path(result["backup"]["rollback"]).read_text(encoding="utf-8")
        self.assertIn("intelli_task_info", text)
        self.assertIn("168", text)

    def test_restore_undoes_the_deduct_write(self) -> None:
        before = dict(self.fake.deduct_row)
        result = bx_apply.approve(FLOW, DAY)
        snapshot = backup.load_snapshot(result["backup"]["snapshot"])
        self.fake.execute_guarded(
            restore.build_restore_statements(snapshot, result["insert_ids"])
        )
        for col in bx_apply.DEDUCT_COLUMNS:
            self.assertAlmostEqual(
                float(self.fake.deduct_row[col]), float(before[col]), places=4, msg=col
            )

    def test_no_human_deduct_leaves_the_row_alone(self) -> None:
        """人工没填扣杂时不能瞎改。但主料型要变，所以整车也不该批。"""
        self.fake.human_deduct_kg = None
        with self.assertRaises(bx_apply.ApproveRefused):
            bx_apply.approve(FLOW, DAY)
        self.assertEqual(self.fake.executed, [])

    def test_deduct_statement_rejects_unknown_column(self) -> None:
        with self.assertRaises(bx_apply.ApproveRefused):
            bx_apply._deduct_statements(FLOW, {"deduct_weight": 0.02, "surprise": 1})


def _deduct_only_card() -> dict:
    """占比已经合格、只改扣重的单。人工主料型 = AI 主料型 = 精炉料1，占比都在 90% 附近。"""
    return {
        **_card(),
        "kind": scan.KIND_DEDUCT,
        "target": "精炉料1",
        "target_key": [1, 1],
        "target_raw_pct": None,
        "human_main_pct": 90.0,
    }


class DeductOnlyTests(ApplyTestCase):
    """占比在带内、只有扣重要改的单。占比相关的三张表一条语句都不该发。"""

    def setUp(self) -> None:
        super().setUp()
        # 人工主料型改成精炉料1、占比 90%，和 AI 的展示占比一致 → 占比已在带内。
        self.fake.load_manual_details = lambda bills: {
            "wb-1": [
                {"waybill_no": "wb-1", "grading_method": m, "material_name": "精炉料",
                 "level": lv, "ratio": r}
                for m in (1, 2)
                for lv, r in ((1, 0.90), (2, 0.10))
            ]
        }
        # 人工 300kg、常数 165（精炉料1）时原始扣杂要抬到 40kg 以上，超过 25kg 上限，
        # 所以换一个能修的人工值。
        self.fake.human_deduct_kg = 280.0
        store.save_pending(_deduct_only_card())

    def _live(self) -> dict:
        return bx_apply.replan_live(_deduct_only_card())

    def test_replan_makes_no_pct_plan(self) -> None:
        live = self._live()
        self.assertIsNone(live["plan"])
        self.assertTrue(live["deduct_only"])
        self.assertIsNotNone(live["deduct_fields"])

    def test_only_the_info_table_is_touched(self) -> None:
        bx_apply.approve(FLOW, DAY)
        tables = {
            "intelli_task_check": 0, "steel_type_stat": 0, "intelli_task_info": 0,
        }
        for sql, _p in self.fake.executed:
            for name in tables:
                if name in sql:
                    tables[name] += 1
        self.assertEqual(tables["intelli_task_check"], 0)
        self.assertEqual(tables["steel_type_stat"], 0)
        self.assertEqual(tables["intelli_task_info"], 1)

    def test_volumes_are_untouched(self) -> None:
        before = {int(r["id"]): float(r["total_volume"]) for r in _check_rows()}
        bx_apply.approve(FLOW, DAY)
        for row in self.fake.check_rows:
            self.assertAlmostEqual(float(row["total_volume"]), before[int(row["id"])])

    def test_deduct_lands_in_the_window(self) -> None:
        bx_apply.approve(FLOW, DAY)
        row = self.fake.deduct_row
        self.assertTrue(deduct.is_ok(float(row["final_deduct"]), 280.0), row)
        self.assertAlmostEqual(
            float(row["modified_deduct_weight"]),
            float(row["deduct_weight"]) * 1000.0 + 165.0,
            places=2,
        )

    def test_result_reports_no_pct_rows_changed(self) -> None:
        result = bx_apply.approve(FLOW, DAY)
        self.assertEqual(result["updated_rows"], 0)
        self.assertEqual(result["inserted_rows"], 0)
        self.assertEqual(result["insert_ids"], [])
        self.assertTrue(result["deduct_written"])

    def test_refuses_when_pct_fell_out_of_band(self) -> None:
        """占比在批准前被改坏了就不能按「只改扣重」处理，要退回重扫。"""
        self.fake.stat_rows = [
            dict(r, steel_volume=v)
            for r, v in zip(_stat_rows(), (100.0, 2600.0, 50.0, 30.0))
        ]
        with self.assertRaises(bx_apply.ApproveRefused) as ctx:
            bx_apply.approve(FLOW, DAY)
        self.assertIn("不在带内", str(ctx.exception))
        self.assertEqual(self.fake.executed, [])

    def test_refuses_when_deduct_plan_vanishes(self) -> None:
        self.fake.human_deduct_kg = None
        with self.assertRaises(bx_apply.ApproveRefused) as ctx:
            bx_apply.approve(FLOW, DAY)
        self.assertIn("只改扣重", str(ctx.exception))
        self.assertEqual(self.fake.executed, [])

    def test_restore_undoes_the_deduct_only_write(self) -> None:
        before = dict(self.fake.deduct_row)
        result = bx_apply.approve(FLOW, DAY)
        snapshot = backup.load_snapshot(result["backup"]["snapshot"])
        self.fake.execute_guarded(
            restore.build_restore_statements(snapshot, result["insert_ids"])
        )
        for col in bx_apply.DEDUCT_COLUMNS:
            self.assertAlmostEqual(
                float(self.fake.deduct_row[col]), float(before[col]), places=4, msg=col
            )


class RestoreTests(ApplyTestCase):
    def test_restore_statements_cover_all_three_parts(self) -> None:
        snapshot = {
            "meta": {"flow_code": FLOW, "car_number": "苏GC7733"},
            "check_rows": [{"id": 1, "total_volume": 500.0}, {"id": 2, "total_volume": 300.0}],
            "stat_rows": [{"id": 900, "steel_rate": 0.46, "steel_volume": 1500.0,
                           "steel_weight": 15.6}],
        }
        stmts = restore.build_restore_statements(snapshot, [5000])
        text = " ".join(s[0] for s in stmts)
        self.assertIn("intelli_task_check", text)
        self.assertIn("steel_type_stat", text)
        self.assertEqual(sum(1 for s in stmts if "total_volume = 0" in s[0]), 1)
        for sql, _p in stmts:
            assert_safe_sql(sql)

    def test_restore_never_deletes(self) -> None:
        snapshot = {
            "meta": {"flow_code": FLOW},
            "check_rows": [{"id": 1, "total_volume": 1.0}],
            "stat_rows": [],
        }
        for sql, _p in restore.build_restore_statements(snapshot, [77]):
            self.assertNotIn("DELETE", sql.upper())

    def test_restore_refuses_empty_snapshot(self) -> None:
        with self.assertRaises(RuntimeError):
            restore.build_restore_statements({"meta": {"flow_code": FLOW}}, [])

    def test_round_trip_restores_original_volumes(self) -> None:
        """批准后回滚，语句里的体积必须等于原始值。"""
        original = {int(r["id"]): float(r["total_volume"]) for r in _check_rows()}
        result = bx_apply.approve(FLOW, DAY)
        snapshot = backup.load_snapshot(result["backup"]["snapshot"])
        stmts = restore.build_restore_statements(snapshot, result["insert_ids"])
        sql, params = stmts[0]
        # params = [id, vol] * n + [flow] + ids，只取前面的成对段
        tail = len(params) - 1
        while tail > 0 and not isinstance(params[tail], str):
            tail -= 1
        pairs = {int(params[i]): float(params[i + 1]) for i in range(0, tail, 2)}
        self.assertEqual(len(pairs), len(original))
        for row_id, volume in original.items():
            self.assertAlmostEqual(pairs[row_id], volume, places=4)

    def test_restore_undoes_the_write_in_fake_db(self) -> None:
        """批准 → 回滚，假库里的体积要回到原样。"""
        original = {int(r["id"]): float(r["total_volume"]) for r in _check_rows()}
        result = bx_apply.approve(FLOW, DAY)
        snapshot = backup.load_snapshot(result["backup"]["snapshot"])
        self.fake.execute_guarded(
            restore.build_restore_statements(snapshot, result["insert_ids"])
        )
        for row in self.fake.check_rows:
            row_id = int(row["id"])
            if row_id in original:
                self.assertAlmostEqual(float(row["total_volume"]), original[row_id], places=3)
            else:
                self.assertEqual(float(row["total_volume"]), 0.0)


if __name__ == "__main__":
    unittest.main()
