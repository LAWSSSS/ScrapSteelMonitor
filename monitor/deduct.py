"""Page 核算/原始扣杂：base → 预处理 → 区间补偿。目标 kg 由人工填写。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from monitor.config import DEDUCT_BAND_KG

DEDUCT_RATE_DECIMALS = 4
PREPROCESS_RANDOM_DEFAULT = 205.0
PREPROCESS_PLUS_50 = 50.0
PREPROCESS_MINUS_150 = 150.0


def _f(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    return float(value)


def _sum_deduct_volume(rows: list[dict[str, Any]]) -> tuple[float, float]:
    deduct = 0.0
    volume = 0.0
    for row in rows:
        deduct += _f(row.get("deduct_weight"))
        volume += _f(row.get("total_volume"))
    return deduct, volume


def deduct_rate(rows: list[dict[str, Any]]) -> float:
    """intelli_task_info.deduct_weight_rate：Σ杂质体积 / Σ废钢体积，四位小数。"""
    deduct, volume = _sum_deduct_volume(rows)
    if volume <= 0:
        return 0.0
    return round(deduct / volume, DEDUCT_RATE_DECIMALS)


def raw_deduct_kg(rows: list[dict[str, Any]], net_weight: float) -> float:
    """未四舍五入的体积占比公斤。"""
    deduct, volume = _sum_deduct_volume(rows)
    if volume <= 0 or net_weight <= 0:
        return 0.0
    return deduct / volume * float(net_weight)


def base_weight_kg(
    rows: list[dict[str, Any]],
    net_weight: float,
    deduct_weight_rate: Any = None,
) -> float:
    """baseWeight = 当日 Σ杂质/Σ体积（四位）× 净重。落库率只作对照，不用来算。"""
    del deduct_weight_rate
    net = float(net_weight)
    if net <= 0:
        return 0.0
    return deduct_rate(rows) * net


def _random_add(base_kg: float, stored: float) -> tuple[float, bool]:
    if not (0 < base_kg < 150):
        return 0.0, False
    if stored > 0:
        return stored, False
    return PREPROCESS_RANDOM_DEFAULT, True


def preprocess_base(base_kg: float, random_add: float) -> tuple[float, str]:
    """规则 A。random_add 只在 0<base<150 时有意义。"""
    if 0 < base_kg < 150:
        return base_kg + random_add, f"+{random_add:.2f}"
    if 150 <= base_kg < 200:
        return base_kg + PREPROCESS_PLUS_50, "+50"
    if base_kg > 450:
        return base_kg - PREPROCESS_MINUS_150, "-150"
    return base_kg, "keep"


def interval_rule(pre_kg: float) -> tuple[float, float, str]:
    """规则 B。1000/2500/5000 按左闭归入更高一档（规则页建议）。"""
    if pre_kg <= 200:
        return 0.0, 1.0, "≤200"
    if pre_kg < 1000:
        return 0.0, 2.0, "200<x<1000"
    if pre_kg < 2500:
        return 1500.0, 5.0, "1000≤x<2500"
    if pre_kg < 5000:
        return 0.0, 5.0, "2500≤x<5000"
    if pre_kg < 10000:
        return 5000.0, 10.0, "5000≤x<10000"
    return 0.0, 10.0, "≥10000"


@dataclass(frozen=True)
class DeductPageResult:
    base_kg: float
    pre_kg: float
    ai_kg: float
    original_kg: float
    preprocess_op: str
    interval: str
    offset: float
    divisor: float
    random_add: float
    random_missing: bool


def apply_page_rules(base_kg: float, preprocess_add_value: float = 0.0) -> DeductPageResult:
    """核算 = 预处理后 + 区间补偿；原始 = 核算 ÷ 区间倍数。"""
    rnd, missing = _random_add(base_kg, _f(preprocess_add_value))
    pre_kg, op = preprocess_base(base_kg, rnd)
    offset, divisor, label = interval_rule(pre_kg)
    accounted = pre_kg + offset
    return DeductPageResult(
        base_kg=base_kg,
        pre_kg=pre_kg,
        ai_kg=accounted,
        original_kg=accounted / divisor,
        preprocess_op=op,
        interval=label,
        offset=offset,
        divisor=divisor,
        random_add=rnd,
        random_missing=missing,
    )


def calculate_deduct(
    rows: list[dict[str, Any]],
    net_weight: float,
    preprocess_add_value: float = 0.0,
    deduct_weight_rate: Any = None,
) -> DeductPageResult:
    base = base_weight_kg(rows, net_weight, deduct_weight_rate)
    return apply_page_rules(base, preprocess_add_value)


def estimate_deduct_kg(
    rows: list[dict[str, Any]],
    net_weight: float,
    preprocess_add_value: float = 0.0,
    deduct_weight_rate: Any = None,
) -> float:
    """业务页「核算扣杂重量」(kg)。"""
    return calculate_deduct(
        rows, net_weight, preprocess_add_value, deduct_weight_rate
    ).ai_kg


def human_deduct_kg(ton_values: list[Any]) -> float | None:
    """人工扣重(T)算术平均（2 或 3 人），再换成 kg。"""
    vals = [_f(x) for x in ton_values if x is not None and x != ""]
    if not vals:
        return None
    return sum(vals) / len(vals) * 1000.0


def deduct_gap_kg(ai_kg: float, human_kg: float) -> float:
    return abs(float(ai_kg) - float(human_kg))


def is_deduct_unqualified(ai_kg: float, human_kg: float, band: float = DEDUCT_BAND_KG) -> bool:
    return deduct_gap_kg(ai_kg, human_kg) > band


def deduct_row_count(rows: list[dict[str, Any]]) -> int:
    return sum(1 for row in rows if _f(row.get("deduct_weight")) > 0)


def task_preprocess(task: dict[str, Any] | None) -> float:
    if not task:
        return 0.0
    return _f(task.get("preprocess_add_value"))


def _forward_matches(base_kg: float, stored: float, target: float) -> bool:
    return abs(apply_page_rules(base_kg, stored).ai_kg - target) < 0.06


def invert_base_to_accounted(
    target_kg: float,
    preprocess_add_value: float = 0.0,
    prefer_base: float | None = None,
) -> float | None:
    """反解 base，使页面核算尽量等于目标。有多解时取离当前 base 最近的。"""
    stored = _f(preprocess_add_value)
    rnd = stored if stored > 0 else PREPROCESS_RANDOM_DEFAULT
    candidates: list[float] = []

    def consider(pre_kg: float) -> None:
        offset, _divisor, _label = interval_rule(pre_kg)
        if abs(pre_kg + offset - target_kg) > 0.06:
            return
        options = [
            pre_kg - rnd,
            pre_kg - PREPROCESS_PLUS_50,
            pre_kg + PREPROCESS_MINUS_150,
            pre_kg,
        ]
        for base in options:
            if base <= 0:
                continue
            if _forward_matches(base, stored, target_kg):
                candidates.append(base)

    consider(target_kg)
    consider(target_kg - 1500.0)
    consider(target_kg - 5000.0)
    if not candidates:
        return None
    if prefer_base is None:
        return candidates[0]
    return min(candidates, key=lambda b: abs(b - prefer_base))


@dataclass
class DeductScalePlan:
    updates: dict[int, float] = field(default_factory=dict)
    factor: float = 1.0
    current_kg: float = 0.0
    target_kg: float = 0.0
    raw_kg: float = 0.0
    base_kg: float = 0.0
    preprocess_add_value: float = 0.0
    ok: bool = True
    reason: str = ""


def plan_deduct_scale(
    rows: list[dict[str, Any]],
    current_kg: float,
    target_kg: float,
    *,
    preprocess_add_value: float = 0.0,
    net_weight: float | None = None,
    deduct_weight_rate: Any = None,
) -> DeductScalePlan:
    """按目标核算反解 base，同比缩放 deduct_weight。体积不动。"""
    stored = _f(preprocess_add_value)
    plan = DeductScalePlan(
        current_kg=float(current_kg),
        target_kg=float(target_kg),
        preprocess_add_value=stored,
    )
    if target_kg <= 0:
        plan.ok = False
        plan.reason = "目标扣杂必须大于 0"
        return plan
    if net_weight is None or float(net_weight) <= 0:
        plan.ok = False
        plan.reason = "缺少净重，无法按核算规则缩放"
        return plan
    net = float(net_weight)
    page = calculate_deduct(rows, net, stored, deduct_weight_rate)
    plan.base_kg = page.base_kg
    plan.raw_kg = raw_deduct_kg(rows, net)
    if page.base_kg <= 0:
        plan.ok = False
        plan.reason = "当前 baseWeight 为 0，无法缩放"
        return plan
    new_base = invert_base_to_accounted(float(target_kg), stored, prefer_base=page.base_kg)
    if new_base is None or new_base <= 0:
        plan.ok = False
        plan.reason = f"目标 {target_kg:.2f}kg 按核算规则反解不出 base"
        return plan
    factor = new_base / page.base_kg
    plan.factor = factor
    for row in rows:
        dw = _f(row.get("deduct_weight"))
        if dw <= 0:
            continue
        plan.updates[int(row["id"])] = round(dw * factor, 4)
    if not plan.updates:
        plan.ok = False
        plan.reason = "没有可缩放的扣杂行"
    return plan


def reread_deduct(
    rows: list[dict[str, Any]],
    net_weight: float,
    human_kg: float,
    preprocess_add_value: float = 0.0,
    deduct_weight_rate: Any = None,
) -> dict[str, Any]:
    stored = _f(preprocess_add_value)
    page = calculate_deduct(rows, net_weight, stored, deduct_weight_rate)
    gap = deduct_gap_kg(page.ai_kg, human_kg)
    return {
        "ai_kg": round(page.ai_kg, 2),
        "original_kg": round(page.original_kg, 2),
        "base_kg": round(page.base_kg, 2),
        "pre_kg": round(page.pre_kg, 2),
        "raw_kg": round(raw_deduct_kg(rows, net_weight), 2),
        "preprocess_add_value": round(stored, 2),
        "preprocess_op": page.preprocess_op,
        "interval": page.interval,
        "random_missing": page.random_missing,
        "human_kg": round(human_kg, 2),
        "gap_kg": round(gap, 2),
        "in_band": gap <= DEDUCT_BAND_KG,
        "deduct_rows": deduct_row_count(rows),
        "net_weight": float(net_weight),
    }
