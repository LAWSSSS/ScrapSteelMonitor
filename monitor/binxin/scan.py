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
from monitor.binxin.config import CHECKS_DEDUCT, MIN_GRABS_TO_CORRECT
from monitor.binxin.deduct import Deduct, DeductPlan, decide, deduct_key, plan_when_main_type_changes
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
    seeded_rng,
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
SKIP_DEDUCT_BLOCKS_PCT = "改主料型会让扣重常数对不上"

#: 修正方式。
PLAN_SCALE = "其余料型等比缩放"
PLAN_RESHAPE = "需摊平其余料型"

#: 待审单要改哪几条线。占比和扣重各有独立的合格标准，可以单独成单。
KIND_BOTH = "占比+扣重"
KIND_PCT = "占比"
KIND_DEDUCT = "扣重"


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
    deduct: Deduct | None = None
    deduct_plan: DeductPlan | None = None
    deduct_skip: str = ""
    deduct_rewrite: bool = False

    @property
    def effective_main(self) -> Key | None:
        """占比改完之后这车的主料型。扣重常数要按这个取，不是按当前的 ai_main。"""
        return self.plan.target_key if self.plan is not None else self.ai_main

    @property
    def plan_kind(self) -> str:
        """这车要改哪几条线。两条都没有方案时是空串。"""
        if self.plan is not None and self.deduct_plan is not None:
            return KIND_BOTH
        if self.plan is not None:
            return KIND_PCT
        if self.deduct_plan is not None:
            return KIND_DEDUCT
        return ""

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
            "kind": self.plan_kind,
            "skip_reason": self.skip_reason,
            "human_pcts": {label(k): round(v, 2) for k, v in self.human_pcts.items()},
            "ai_raw_pcts": {label(k): round(v, 2) for k, v in self.ai_raw_pcts.items()},
            "ai_display_pcts": {label(k): round(v, 2) for k, v in self.ai_display_pcts.items()},
            "plan": self.plan.as_card() if self.plan else None,
            "deduct": self.deduct.as_card() if self.deduct else None,
            "deduct_plan": self.deduct_plan.as_card() if self.deduct_plan else None,
            "deduct_skip": self.deduct_skip,
            "deduct_rewrite": self.deduct_rewrite,
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

    vehicle.plan = plan_correction(
        vehicle.ai_raw_pcts, vehicle.human_main, human_pct, seed=vehicle.flow_code
    )
    if vehicle.plan is None:
        vehicle.skip_reason = SKIP_LEAD_CONFLICT
    return vehicle


def _observe_deduct(
    vehicle: Vehicle,
    deduct_row: dict[str, Any] | None,
    manual_row: dict[str, Any] | None,
    stat_rows: list[dict[str, Any]],
    main: Key | None,
) -> Deduct:
    """把库里的扣重字段装成 Deduct。附加常数按传进来的 main 取。"""
    return Deduct(
        raw_kg=float((deduct_row or {}).get("deduct_weight") or 0.0) * 1000.0,
        modified_kg=_opt_float((deduct_row or {}).get("modified_deduct_weight")),
        calculated_kg=_opt_float((deduct_row or {}).get("calculated_deduct_weight")),
        final_kg=_opt_float((deduct_row or {}).get("final_deduct")),
        qigezha_kg=float((deduct_row or {}).get("qigezha_hanzha_weight") or 0.0),
        weather_kg=float((deduct_row or {}).get("weather_deduct_weight") or 0.0),
        net_weight_kg=vehicle.net_weight * 1000.0,
        human_kg=_opt_float((manual_row or {}).get("deduction_tonnage")),
        deduct_key=deduct_key(stat_rows, main) if main is not None else None,
        modified_rate=float((deduct_row or {}).get("modified_deduct_rate") or 0.0),
        # 附加常数 2026-07 中旬 +100 过一次，改历史车必须按当时那一版算。
        checked_at=vehicle.checked_at,
    )


def judge_deduct(
    vehicle: Vehicle,
    deduct_row: dict[str, Any] | None,
    manual_row: dict[str, Any] | None,
    stat_rows: list[dict[str, Any]],
) -> None:
    """填上这车的扣重判定，就地改 vehicle。要在占比判定之后调用。

    常数按 vehicle.effective_main 取，也就是占比改完之后的主料型。占比修正会换主料型的车，
    常数跟着变，但 modified_deduct_weight 是存量字段不会自己重算——页面上原始扣杂和修正
    扣杂并排显示，两者之差就是常数，不同步改就对不上。deduct_rewrite 标记的就是这种车。

    主料型正确优先于扣重合格。换主料型后，扣重能落进人工 ±100kg 就按合格方案改；落不进、
    但新常数是已知的，就保持原始扣杂，只把修正扣杂改成 原始 + 新常数，最终扣重偏离人工也
    照样改占比。只有常数本身未知（过渡期、重废2、没有人工）才把占比方案一起撤掉。
    """
    changed = vehicle.plan is not None and vehicle.plan.target_key != vehicle.ai_main
    observed = _observe_deduct(
        vehicle, deduct_row, manual_row, stat_rows, vehicle.effective_main
    )
    if changed:
        plan, skip = plan_when_main_type_changes(observed, seed=vehicle.flow_code)
    else:
        plan, skip = decide(observed, main_type_changed=False, seed=vehicle.flow_code)
    if changed and plan is None:
        logger.info(
            "%s 占比可改但扣重常数未知（%s），占比方案撤掉",
            vehicle.car_number,
            skip or "原因未知",
        )
        vehicle.plan = None
        vehicle.skip_reason = SKIP_DEDUCT_BLOCKS_PCT
        changed = False
        observed = _observe_deduct(
            vehicle, deduct_row, manual_row, stat_rows, vehicle.ai_main
        )
        plan, skip = decide(observed, main_type_changed=False, seed=vehicle.flow_code)
    vehicle.deduct = observed
    vehicle.deduct_plan = plan
    # 合格方案不带跳过原因。按常数放行的方案保留原因，看板上要写明这次不追扣重合格。
    vehicle.deduct_skip = skip if plan is None or plan.accept_miss else ""
    vehicle.deduct_rewrite = changed


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def plan_correction(
    raw_pcts: dict[Key, float],
    target_key: Key,
    human_pct: float,
    *,
    seed: str = "",
) -> Plan | None:
    """反演出目标原始占比。先试改动最小的等比缩放，不行再试摊平其余料型。

    seed 传 flow_code：目标占比要带按车抖动，否则人工占比相同的车会拿到同一个目标值。
    种子只能跟车有关、不能带时间，这样重扫同一天给出的目标不会变。
    """
    current = raw_pcts.get(target_key, 0.0)
    for method, reshape in ((PLAN_SCALE, False), (PLAN_RESHAPE, True)):
        floor = lead_floor(raw_pcts, target_key, reshape=reshape)
        rng = seeded_rng(f"binxin|target|{seed}|{target_key}|{method}") if seed else None
        target = pick_raw_target(human_pct, current, floor_pct=floor, rng=rng)
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
        deducts = db.load_deduct_rows(flows) if CHECKS_DEDUCT else {}
        manual_deducts = db.load_manual_deducts(bills) if CHECKS_DEDUCT else {}
    out: list[Vehicle] = []
    for car in cars:
        flow = str(car["flow_code"])
        bill = str(car["order_code"])
        vehicle = judge(
            car,
            stats.get(flow, []),
            manuals.get(bill, []),
            grabs.get(flow, 0),
        )
        vehicle.task_code = str((stats.get(flow) or [{}])[0].get("task_code") or "")
        vehicle.net_weight = float(car.get("net_weight") or 0.0)
        vehicle.station_number = car.get("station_number")
        if CHECKS_DEDUCT:
            judge_deduct(
                vehicle,
                deducts.get(flow),
                manual_deducts.get(bill),
                stats.get(flow, []),
            )
        out.append(vehicle)
    return out


def pending_card(vehicle: Vehicle, day: date) -> dict[str, Any]:
    """待审单。批准时要用的字段都在里面，不必再回头查扫描结果。

    两条线可以单独成单：占比要改的、扣重要改的、以及两个都要改的。kind 记的就是这个，批准
    时按它决定改哪些表。
    """
    if vehicle.plan is None and vehicle.deduct_plan is None:
        raise ValueError(f"{vehicle.car_number} 没有方案，不能进入待确认")
    # 扣重常数按改完之后的主料型取，所以 target_key 一律记 effective_main：占比要改时是人工
    # 的主料型，只改扣重时就是当前 AI 的主料型（这时它和人工一致，否则占比也会要改）。
    main = vehicle.effective_main
    if main is None:
        raise ValueError(f"{vehicle.car_number} 没有主料型，不能进入待确认")
    return {
        **vehicle.as_card(),
        "biz_date": day.isoformat(),
        "task_code": vehicle.task_code,
        "net_weight": vehicle.net_weight,
        "station_number": vehicle.station_number,
        "kind": vehicle.plan_kind,
        "target_key": [main[0], main[1]],
        "target_raw_pct": (
            round(vehicle.plan.target_raw_pct, 4) if vehicle.plan is not None else None
        ),
        "human_main_pct": round(
            vehicle.plan.human_pct
            if vehicle.plan is not None
            else (vehicle.human_pcts.get(vehicle.human_main, 0.0) if vehicle.human_main else 0.0),
            4,
        ),
        # 扣重的目标只作展示用。批准时会按实时数据重算一遍，不拿这里的旧值写库。
        "deduct_target_raw_kg": (
            round(vehicle.deduct_plan.target_raw_kg, 1) if vehicle.deduct_plan else None
        ),
        "deduct_human_kg": vehicle.deduct.human_kg if vehicle.deduct else None,
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
        if vehicle.plan is None and vehicle.deduct_plan is None:
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
        "deduct_counts": summarize_deduct(vehicles) if CHECKS_DEDUCT else {},
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
    counts: dict[str, int] = {"在带内": 0, "待确认": 0}
    for vehicle in vehicles:
        if vehicle.ok:
            counts["在带内"] += 1
        elif vehicle.plan is not None:
            counts["待确认"] += 1
            counts[vehicle.plan.method] = counts.get(vehicle.plan.method, 0) + 1
        else:
            reason = vehicle.skip_reason or "未分类"
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def summarize_deduct(vehicles: list[Vehicle]) -> dict[str, int]:
    """扣重单独计数。一辆车占比和扣重可以一个合格一个不合格，两条线互不替代。"""
    counts: dict[str, int] = {"扣重合格": 0, "扣重待确认": 0, "扣重需随主料型重算": 0}
    for vehicle in vehicles:
        if vehicle.deduct is None:
            continue
        if vehicle.deduct_rewrite:
            counts["扣重需随主料型重算"] += 1
        if vehicle.deduct_plan is not None:
            counts["扣重待确认"] += 1
        elif vehicle.deduct.ok:
            counts["扣重合格"] += 1
        else:
            reason = vehicle.deduct_skip or "扣重未分类"
            counts[reason] = counts.get(reason, 0) + 1
    return counts
