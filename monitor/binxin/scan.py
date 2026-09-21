"""镔鑫扫描：一天一轮，判断每车的主料型差异率，分类出要修正的。

判定链路（每一步都可能让一辆车提前出局）：

1. 人工最终占比 = material_detail_info 两次评级的算术平均。
2. AI 原始占比 = steel_type_stat.steel_volume 的占比，重废并级。
3. AI 展示占比 = 原始占比过一遍镔鑫后端的规范化映射，这才是页面上的数。
4. 差异率 = |人工主料型占比 − AI主料型展示占比| / 人工主料型占比，主料型不一致记 100%。
5. 要修正的车再反演出目标原始占比，并检查改完该料型是否仍是最高。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

from monitor.binxin import db, store
from monitor.binxin.config import MIN_GRABS_TO_CORRECT
from monitor.binxin.materials import Key, canonical, from_manual, is_graded, label, priority_rank
from monitor.binxin.rate import (
    band_window,
    diff_rate,
    display_main_pct,
    human_final_pcts,
    in_band,
    is_reachable,
    lead_floor,
    main_key,
    normalize,
    pick_raw_target,
)
from monitor.config import TZ

logger = logging.getLogger(__name__)

#: 出局原因。看板上按这些分组计数。
SKIP_NO_AI = "无AI检判结果"
SKIP_NO_HUMAN = "无人工检判结果"
SKIP_HUMAN_OUT_OF_SCOPE = "人工主料型不在AI输出范围"
SKIP_AI_OUT_OF_SCOPE = "AI主料型不在判定范围"
SKIP_UNREACHABLE = "人工占比落在展示空洞"
SKIP_LEAD_CONFLICT = "进带与保持主料型冲突"
SKIP_TOO_FEW_GRABS = "抓斗太少不便修正"

#: 修正方式。
PLAN_SCALE = "其余料型等比缩放"
PLAN_RESHAPE = "需摊平其余料型"


def _round(value: float | None, digits: int = 2) -> float | None:
    """卡片上的数都收到小数位。落盘和看板都读这一份，不能带一长串浮点尾数。"""
    return None if value is None else round(float(value), digits)


@dataclass
class Vehicle:
    """一车的判定结果。in_band 为真或 skip_reason 非空时 plan 为 None。"""

    car_number: str
    order_code: str
    flow_code: str
    checked_at: str
    grabs: int
    task_code: str = ""
    net_weight: float = 0.0
    station_number: Any = None
    human_pcts: dict[Key, float] = field(default_factory=dict)
    ai_raw_pcts: dict[Key, float] = field(default_factory=dict)
    ai_display_pcts: dict[Key, float] = field(default_factory=dict)
    human_main: Key | None = None
    ai_main: Key | None = None
    diff_rate_pct: float | None = None
    ok: bool = False
    skip_reason: str = ""
    plan: "Plan | None" = None

    def as_card(self) -> dict[str, Any]:
        """看板用的扁平结构。"""
        return {
            "car_number": self.car_number,
            "order_code": self.order_code,
            "flow_code": self.flow_code,
            "task_code": self.task_code,
            "checked_at": self.checked_at,
            "grabs": self.grabs,
            "net_weight": self.net_weight,
            "station_number": self.station_number,
            "human_main": label(self.human_main) if self.human_main else "",
            "human_main_pct": _round(
                self.human_pcts.get(self.human_main) if self.human_main else None
            ),
            "ai_main": label(self.ai_main) if self.ai_main else "",
            "ai_main_pct": _round(
                self.ai_display_pcts.get(self.ai_main) if self.ai_main else None
            ),
            "diff_rate_pct": _round(self.diff_rate_pct, 1),
            "ok": self.ok,
            "skip_reason": self.skip_reason,
            "human_pcts": {label(k): round(v, 2) for k, v in self.human_pcts.items()},
            "ai_raw_pcts": {label(k): round(v, 2) for k, v in self.ai_raw_pcts.items()},
            "ai_display_pcts": {label(k): round(v, 2) for k, v in self.ai_display_pcts.items()},
            "plan": self.plan.as_card() if self.plan else None,
        }


@dataclass
class Plan:
    """把某个料型的原始占比推到 target_raw_pct，使其展示占比落进人工的 ±10%。"""

    target_key: Key
    human_pct: float
    current_raw_pct: float
    target_raw_pct: float
    target_display_pct: float
    lead_floor_pct: float
    method: str

    def as_card(self) -> dict[str, Any]:
        lo, hi = band_window(self.human_pct)
        # band_window 是差异率定义的纯公式，人工 95% 时上界会算到 104.5%。展示占比不可能
        # 超过 100%，所以报给人看的时候截到 100，免得看板上出现不存在的区间。
        hi = min(hi, 100.0)
        return {
            "target": label(self.target_key),
            # JSON 存不了元组键，落盘成两个整数，批准时再还原。
            "target_key": [self.target_key[0], self.target_key[1]],
            "current_raw_pct": round(self.current_raw_pct, 2),
            "target_raw_pct": round(self.target_raw_pct, 2),
            "target_display_pct": round(self.target_display_pct, 2),
            "lead_floor_pct": round(self.lead_floor_pct, 2),
            "method": self.method,
            "band_lo_pct": round(lo, 2),
            "band_hi_pct": round(hi, 2),
        }


def human_pcts_of(rows: Iterable[dict[str, Any]]) -> dict[Key, float]:
    """material_detail_info 行 → 人工最终占比。白名单外的料型归 OTHER 但保留占比。"""
    triples = [
        (
            int(row["grading_method"] or 0),
            from_manual(str(row["material_name"] or ""), row["level"]),
            float(row["ratio"] or 0.0) * 100.0,
        )
        for row in rows
    ]
    return human_final_pcts(triples)


def primary_stat_group(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """只取一组 data_type。

    近 30 天每个 flow 都只有 data_type=1 一组，但表结构允许多组，真出现多组时把它们一起
    求和会让占比翻倍。这里固定挑行数最多的那组（并列取 data_type 最小），并记一条日志。
    """
    groups: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(int(row.get("data_type") or 0), []).append(row)
    if len(groups) <= 1:
        return next(iter(groups.values()), [])
    picked = min(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    logger.warning(
        "flow 有 %s 组 data_type（%s），只用 data_type=%s",
        len(groups),
        sorted(groups),
        picked[0],
    )
    return picked[1]


def ai_raw_pcts_of(rows: Iterable[dict[str, Any]]) -> dict[Key, float]:
    """steel_type_stat 行 → AI 原始占比。按 steel_volume 算，重废并级。"""
    volumes: dict[Key, float] = {}
    for row in primary_stat_group(rows):
        key = canonical(row.get("steel_type"), row.get("steel_level"))
        volumes[key] = volumes.get(key, 0.0) + float(row.get("steel_volume") or 0.0)
    total = sum(volumes.values())
    if total <= 0.0:
        return {}
    return {k: v / total * 100.0 for k, v in volumes.items()}


def judge(
    car: dict[str, Any],
    stat_rows: list[dict[str, Any]],
    manual_rows: list[dict[str, Any]],
    grabs: int,
) -> Vehicle:
    """一车的完整判定。不连库，方便单测。"""
    vehicle = Vehicle(
        car_number=str(car.get("car_number") or ""),
        order_code=str(car.get("order_code") or ""),
        flow_code=str(car.get("flow_code") or ""),
        checked_at=str(car.get("check_start_time") or ""),
        grabs=grabs,
    )
    vehicle.ai_raw_pcts = ai_raw_pcts_of(stat_rows)
    vehicle.human_pcts = human_pcts_of(manual_rows)
    if not vehicle.ai_raw_pcts:
        vehicle.skip_reason = SKIP_NO_AI
        return vehicle
    if not vehicle.human_pcts:
        vehicle.skip_reason = SKIP_NO_HUMAN
        return vehicle

    vehicle.ai_display_pcts = normalize(vehicle.ai_raw_pcts, quantize=False)
    vehicle.human_main = main_key(vehicle.human_pcts, rank=priority_rank)
    vehicle.ai_main = main_key(vehicle.ai_raw_pcts, rank=priority_rank)
    if vehicle.human_main is None or not is_graded(vehicle.human_main):
        vehicle.skip_reason = SKIP_HUMAN_OUT_OF_SCOPE
        return vehicle
    if vehicle.ai_main is None or not is_graded(vehicle.ai_main):
        vehicle.skip_reason = SKIP_AI_OUT_OF_SCOPE
        return vehicle

    human_pct = vehicle.human_pcts[vehicle.human_main]
    ai_display = vehicle.ai_display_pcts.get(vehicle.ai_main, 0.0)
    same_main = vehicle.ai_main == vehicle.human_main
    vehicle.diff_rate_pct = diff_rate(human_pct, ai_display, same_main_type=same_main)
    if in_band(human_pct, ai_display, same_main_type=same_main):
        vehicle.ok = True
        return vehicle

    if not is_reachable(human_pct):
        vehicle.skip_reason = SKIP_UNREACHABLE
        return vehicle
    if grabs < MIN_GRABS_TO_CORRECT:
        vehicle.skip_reason = SKIP_TOO_FEW_GRABS
        return vehicle

    vehicle.plan = plan_correction(vehicle.ai_raw_pcts, vehicle.human_main, human_pct)
    if vehicle.plan is None:
        vehicle.skip_reason = SKIP_LEAD_CONFLICT
    return vehicle


def plan_correction(
    raw_pcts: dict[Key, float],
    target_key: Key,
    human_pct: float,
) -> Plan | None:
    """反演出目标原始占比。先试改动最小的等比缩放，不行再试摊平其余料型。"""
    current = raw_pcts.get(target_key, 0.0)
    for method, reshape in ((PLAN_SCALE, False), (PLAN_RESHAPE, True)):
        floor = lead_floor(raw_pcts, target_key, reshape=reshape)
        target = pick_raw_target(human_pct, current, floor_pct=floor)
        if target is None:
            continue
        return Plan(
            target_key=target_key,
            human_pct=human_pct,
            current_raw_pct=current,
            target_raw_pct=target,
            target_display_pct=display_main_pct(target),
            lead_floor_pct=floor,
            method=method,
        )
    return None


def scan_day(day: date) -> list[Vehicle]:
    """扫一天。一条连接拉完所有表，再在内存里按车分组判定。只读。"""
    with db.session():
        cars = db.load_day_cars(day)
        flows = [str(c["flow_code"]) for c in cars]
        bills = [str(c["order_code"]) for c in cars]
        stats = db.load_steel_type_stats(flows)
        manuals = db.load_manual_details(bills)
        grabs = db.count_grabs(flows)
    out: list[Vehicle] = []
    for car in cars:
        flow = str(car["flow_code"])
        vehicle = judge(
            car,
            stats.get(flow, []),
            manuals.get(str(car["order_code"]), []),
            grabs.get(flow, 0),
        )
        vehicle.task_code = str((stats.get(flow) or [{}])[0].get("task_code") or "")
        vehicle.net_weight = float(car.get("net_weight") or 0.0)
        vehicle.station_number = car.get("station_number")
        out.append(vehicle)
    return out


def pending_card(vehicle: Vehicle, day: date) -> dict[str, Any]:
    """待审单。批准时要用的字段都在里面，不必再回头查扫描结果。"""
    if vehicle.plan is None:
        raise ValueError(f"{vehicle.car_number} 没有方案，不能进待审")
    return {
        **vehicle.as_card(),
        "biz_date": day.isoformat(),
        "task_code": vehicle.task_code,
        "net_weight": vehicle.net_weight,
        "station_number": vehicle.station_number,
        "target_key": [vehicle.plan.target_key[0], vehicle.plan.target_key[1]],
        "target_raw_pct": round(vehicle.plan.target_raw_pct, 4),
        "human_main_pct": round(vehicle.plan.human_pct, 4),
        "status": "pending",
    }


def queue_day(day: date) -> dict[str, Any]:
    """扫一天并把要修正的车写进待审队列。本地写文件，不写库。

    已经批准过的车不再重新入队；已在待审里的车用新方案覆盖，避免拿着旧目标占比去批准。
    """
    vehicles = scan_day(day)
    day_text = day.isoformat()
    queued: list[str] = []
    for vehicle in vehicles:
        if vehicle.plan is None:
            continue
        if store.is_processed(vehicle.flow_code, day_text):
            continue
        store.save_pending(pending_card(vehicle, day))
        queued.append(vehicle.car_number)
    summary = {
        "date": day_text,
        "scanned_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "total": len(vehicles),
        "counts": summarize(vehicles),
        "queued": queued,
        "vehicles": [v.as_card() for v in vehicles],
    }
    store.save_scan_summary(day_text, summary)
    store.append_log(
        "scanned",
        {"biz_date": day_text, "total": len(vehicles), "queued": len(queued)},
    )
    return summary


def summarize(vehicles: list[Vehicle]) -> dict[str, int]:
    """按结论计数，顺序固定，方便看板和日志对比。"""
    counts: dict[str, int] = {"在带内": 0, "待修正": 0}
    for vehicle in vehicles:
        if vehicle.ok:
            counts["在带内"] += 1
        elif vehicle.plan is not None:
            counts["待修正"] += 1
            counts[vehicle.plan.method] = counts.get(vehicle.plan.method, 0) + 1
        else:
            reason = vehicle.skip_reason or "未分类"
            counts[reason] = counts.get(reason, 0) + 1
    return counts
