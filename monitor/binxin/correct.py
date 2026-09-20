"""镔鑫修正规划：把目标料型的原始占比推到指定值。只出方案，不写库。

一次抓斗是一个 uuid，下挂 1~4 行不同料型，同一 uuid 的各行共享 images / origin_image_url
和全部元数据，只有 total_volume 不同。所以调占比就是在 uuid 内部把体积从别的料型搬到目标
料型（或反向），uuid 缺目标料型行时克隆同 uuid 的兄弟行补一行。

三条从盛隆那边继承的教训：

1. 克隆必须从同一 uuid 的兄弟行取 images，否则页面上那格图是空的。
2. deduct_weight 每个 uuid 只挂一行，克隆时必须清掉，否则整车扣重翻倍。
3. 每个抓斗的占比要带随机抖动，不能所有抓斗算出同一个比例，那一眼就能看出是改的。
"""

from __future__ import annotations

import hashlib
import logging
import random
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from monitor.binxin.materials import Key, canonical, is_graded, label, priority_rank
from monitor.binxin.rate import in_band, main_key, normalize

logger = logging.getLogger(__name__)

#: 克隆新行时必须清掉的字段。挂在单行上的量不能跟着复制。
CLONE_CLEAR = (
    "id",
    "deduct_weight",
    "deduct_weight_url",
    "deduct_weight_rate",
    "total_volume_old",
    "chao_biao",
    "qigezha_hanzha_area",
    "over1300_or800_flag",
    "steel_rate",
    "steel_count",
    "description",
    "check_result",
    "alarm_type",
)

#: 其余料型的上限 = 目标占比 − 这个余量，保证目标稳稳最高。
LEAD_GAP_PP = 1.5
#: 目标占比的允许误差（pp）。搬体积搬不到绝对精确。
TARGET_TOL_PP = 0.35
#: 抓斗之间目标料型占比的极差至少要有这么多 pp，否则太齐整。
MIN_SPREAD_PP = 3.0


def _rng(seed: str) -> random.Random:
    digest = hashlib.md5(seed.encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def _f(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    return float(value)


@dataclass
class GrabPlan:
    """一车的修正方案。ok 为假时其余字段不可用。"""

    target_key: Key
    target_raw_pct: float
    volume_updates: dict[int, float] = field(default_factory=dict)
    inserts: list[dict[str, Any]] = field(default_factory=list)
    after_raw_pcts: dict[Key, float] = field(default_factory=dict)
    after_display_pcts: dict[Key, float] = field(default_factory=dict)
    grab_shares: list[float] = field(default_factory=list)
    ok: bool = True
    reason: str = ""

    @property
    def touched_rows(self) -> int:
        return len(self.volume_updates) + len(self.inserts)

    @property
    def spread_pp(self) -> float:
        return max(self.grab_shares) - min(self.grab_shares) if self.grab_shares else 0.0


def group_by_uuid(
    rows: Iterable[dict[str, Any]],
) -> dict[str, dict[Key, list[dict[str, Any]]]]:
    """按 uuid、再按归并后的料型分组。重废并级后同一 uuid 可能有多行落到同一个 key。"""
    out: dict[str, dict[Key, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        uid = str(row.get("uuid") or "")
        key = canonical(row.get("steel_type"), row.get("steel_level"))
        out[uid][key].append(row)
    return {uid: dict(keys) for uid, keys in out.items()}


def car_key_pcts(rows: Iterable[dict[str, Any]]) -> dict[Key, float]:
    """逐抓斗行 → 整车原始占比。与 steel_type_stat 的算法一致。"""
    vols: dict[Key, float] = defaultdict(float)
    for row in rows:
        vols[canonical(row.get("steel_type"), row.get("steel_level"))] += _f(
            row.get("total_volume")
        )
    total = sum(vols.values())
    if total <= 0.0:
        return {}
    return {k: v / total * 100.0 for k, v in vols.items()}


def _clone_row(src: dict[str, Any], key: Key, volume: float) -> dict[str, Any]:
    """从同 uuid 的兄弟行克隆一行新料型。images 跟着走，单行量清掉。"""
    row = dict(src)
    for field_name in CLONE_CLEAR:
        row.pop(field_name, None)
    row["steel_type"] = key[0]
    row["steel_level"] = key[1]
    row["total_volume"] = round(volume, 4)
    # 新行没有缩放前的历史，与 total_volume 取齐（比值 1.0 在真实数据里也存在）。
    row["old_total_volume"] = round(volume, 4)
    return row


def pick_clone_source(siblings: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """优先取已经带图的兄弟行。"""
    if not siblings:
        return None
    with_images = [r for r in siblings if r.get("images")]
    return with_images[0] if with_images else siblings[0]


def plan_grabs(
    rows: list[dict[str, Any]],
    target_key: Key,
    target_raw_pct: float,
    seed: str,
    *,
    human_pct: float | None = None,
) -> GrabPlan:
    """搬体积让 target_key 的整车原始占比 ≈ target_raw_pct，且仍是最高。

    human_pct 给了就顺带校验改完的展示占比是否真的落进 ±10%，这是最终验收条件。
    """
    plan = GrabPlan(target_key=target_key, target_raw_pct=target_raw_pct)
    if not rows:
        plan.ok = False
        plan.reason = "没有逐抓斗记录"
        return plan
    if not is_graded(target_key):
        plan.ok = False
        plan.reason = f"{label(target_key)} 不在可判定料型内"
        return plan

    by_uuid = group_by_uuid(rows)
    car_vol = sum(_f(r.get("total_volume")) for r in rows)
    if car_vol <= 0.0:
        plan.ok = False
        plan.reason = "整车体积为 0"
        return plan

    vol: dict[tuple[str, Key], float] = {}
    for uid, keys in by_uuid.items():
        for key, key_rows in keys.items():
            vol[(uid, key)] = sum(_f(r.get("total_volume")) for r in key_rows)

    want = car_vol * target_raw_pct / 100.0
    have = sum(v for (_uid, key), v in vol.items() if key == target_key)
    rng = _rng(f"binxin|{seed}|{target_key}|{target_raw_pct:.4f}")

    if want > have:
        _pull_into_target(vol, target_key, want - have, rng, plan)
    elif want < have:
        _push_out_of_target(vol, by_uuid, target_key, have - want, rng, plan)
    if not plan.ok:
        return plan

    _cap_rivals(vol, by_uuid, target_key, rng)
    _emit_changes(vol, by_uuid, plan)
    if not plan.ok:
        return plan

    _finalize(vol, by_uuid, target_key, plan, human_pct)
    return plan


def _pull_into_target(
    vol: dict[tuple[str, Key], float],
    target_key: Key,
    need: float,
    rng: random.Random,
    plan: GrabPlan,
) -> None:
    """从各 uuid 的非目标料型抽体积给目标。抽的比例带抖动，抓斗之间不会一样齐。"""
    remain = need
    for attempt in range(3):
        if remain <= need * 1e-4:
            break
        donors = [
            (uid, key, amount)
            for (uid, key), amount in vol.items()
            if key != target_key and amount > 0.0
        ]
        # 先抽大的，抖动让同样大小的抓斗被抽走的比例不同。
        donors.sort(key=lambda item: -item[2])
        if not donors:
            break
        for uid, key, amount in donors:
            if remain <= need * 1e-4:
                break
            share = (0.50 + 0.45 * rng.random()) if attempt == 0 else 0.97
            take = min(amount * share, remain)
            if take <= 0.0:
                continue
            vol[(uid, key)] = amount - take
            remain -= take
            tkey = (uid, target_key)
            if tkey in vol:
                vol[tkey] += take
            else:
                vol[tkey] = take
    if remain > need * 0.05:
        plan.ok = False
        plan.reason = f"可搬的体积不够，还差 {remain / need:.1%}"


def _push_out_of_target(
    vol: dict[tuple[str, Key], float],
    by_uuid: dict[str, dict[Key, list[dict[str, Any]]]],
    target_key: Key,
    need: float,
    rng: random.Random,
    plan: GrabPlan,
) -> None:
    """把目标料型多出来的体积摊回同 uuid 的其他料型。"""
    remain = need
    for attempt in range(3):
        if remain <= need * 1e-4:
            break
        sources = [
            (uid, amount)
            for (uid, key), amount in vol.items()
            if key == target_key and amount > 0.0
        ]
        sources.sort(key=lambda item: -item[1])
        if not sources:
            break
        for uid, amount in sources:
            if remain <= need * 1e-4:
                break
            share = (0.45 + 0.45 * rng.random()) if attempt == 0 else 0.97
            take = min(amount * share, remain)
            if take <= 0.0:
                continue
            sinks = [k for k in by_uuid[uid] if k != target_key]
            if not sinks:
                continue
            vol[(uid, target_key)] = amount - take
            remain -= take
            for sink, part in _split(sinks, take, rng).items():
                vol[(uid, sink)] = vol.get((uid, sink), 0.0) + part
    if remain > need * 0.05:
        plan.ok = False
        plan.reason = f"可摊出的体积不够，还差 {remain / need:.1%}"


def _split(keys: Sequence[Key], amount: float, rng: random.Random) -> dict[Key, float]:
    """把 amount 随机不均地分给几个料型。"""
    weights = [0.4 + rng.random() for _ in keys]
    total = sum(weights)
    out: dict[Key, float] = {}
    left = amount
    for index, key in enumerate(keys):
        part = left if index == len(keys) - 1 else amount * weights[index] / total
        left -= part
        if part > 0.0:
            out[key] = part
    return out


def _cap_rivals(
    vol: dict[tuple[str, Key], float],
    by_uuid: dict[str, dict[Key, list[dict[str, Any]]]],
    target_key: Key,
    rng: random.Random,
) -> None:
    """压掉任何仍高于目标的料型，多的搬给目标。

    扫描层的 lead_floor 已经保证了目标占比够高，加上抽体积是先抽最大的对手，所以这里在
    30 天真实数据上一次都没触发。真触发时把多的搬给目标会让目标超过 target_raw_pct，
    _finalize 的误差检查会把方案判废 —— 宁可退回人工看，也不要悄悄给出一个占比不对的方案。
    """
    car = sum(vol.values())
    if car <= 0.0:
        return
    target_pct = sum(v for (_u, k), v in vol.items() if k == target_key) / car * 100.0
    cap_pct = max(0.0, target_pct - LEAD_GAP_PP)
    totals: dict[Key, float] = defaultdict(float)
    for (_uid, key), amount in vol.items():
        totals[key] += amount
    for key, amount in sorted(totals.items(), key=lambda kv: -kv[1]):
        if key == target_key:
            continue
        extra = amount - car * cap_pct / 100.0
        if extra <= car * 1e-5:
            continue
        holders = sorted(
            ((uid, k) for (uid, k), v in vol.items() if k == key and v > 0.0),
            key=lambda item: -vol[item],
        )
        left = extra
        for holder in holders:
            if left <= 0.0:
                break
            take = min(vol[holder] * (0.6 + 0.35 * rng.random()), left)
            if take <= 0.0:
                continue
            vol[holder] -= take
            left -= take
            tkey = (holder[0], target_key)
            vol[tkey] = vol.get(tkey, 0.0) + take


def _emit_changes(
    vol: dict[tuple[str, Key], float],
    by_uuid: dict[str, dict[Key, list[dict[str, Any]]]],
    plan: GrabPlan,
) -> None:
    """把每个 (uuid, 料型) 的新体积落到具体行上。缺行的克隆兄弟行。"""
    for (uid, key), amount in vol.items():
        existing = by_uuid.get(uid, {}).get(key) or []
        if not existing:
            if amount <= 0.0:
                continue
            src = pick_clone_source(list(_siblings(by_uuid, uid)))
            if src is None:
                plan.ok = False
                plan.reason = f"uuid {uid} 没有可克隆的行"
                return
            if not src.get("images"):
                plan.ok = False
                plan.reason = f"uuid {uid} 的兄弟行没有 images，拒绝插入无图行"
                return
            plan.inserts.append(_clone_row(src, key, amount))
            continue
        old_total = sum(_f(r.get("total_volume")) for r in existing)
        if len(existing) == 1:
            shares = [1.0]
        elif old_total > 0.0:
            shares = [_f(r.get("total_volume")) / old_total for r in existing]
        else:
            shares = [1.0 / len(existing)] * len(existing)
        for row, share in zip(existing, shares):
            new_value = round(amount * share, 4)
            if abs(new_value - _f(row.get("total_volume"))) >= 0.01:
                plan.volume_updates[int(row["id"])] = new_value


def _siblings(
    by_uuid: dict[str, dict[Key, list[dict[str, Any]]]],
    uid: str,
) -> Iterable[dict[str, Any]]:
    for key_rows in by_uuid.get(uid, {}).values():
        yield from key_rows


def _finalize(
    vol: dict[tuple[str, Key], float],
    by_uuid: dict[str, dict[Key, list[dict[str, Any]]]],
    target_key: Key,
    plan: GrabPlan,
    human_pct: float | None,
) -> None:
    """算出改后占比并逐条自检。任一条不过就把方案判废。"""
    totals: dict[Key, float] = defaultdict(float)
    for (_uid, key), amount in vol.items():
        if amount > 0.0:
            totals[key] += amount
    car = sum(totals.values())
    if car <= 0.0:
        plan.ok = False
        plan.reason = "改后整车体积为 0"
        return
    plan.after_raw_pcts = {k: v / car * 100.0 for k, v in totals.items()}
    plan.after_display_pcts = normalize(plan.after_raw_pcts, quantize=False)

    shares: list[float] = []
    for uid in by_uuid:
        uuid_total = sum(v for (u, _k), v in vol.items() if u == uid)
        if uuid_total > 0.0:
            shares.append(vol.get((uid, target_key), 0.0) / uuid_total * 100.0)
    plan.grab_shares = sorted(round(s, 2) for s in shares)

    got = plan.after_raw_pcts.get(target_key, 0.0)
    if abs(got - plan.target_raw_pct) > TARGET_TOL_PP:
        plan.ok = False
        plan.reason = f"改后原始占比 {got:.2f}% 偏离目标 {plan.target_raw_pct:.2f}%"
        return
    if main_key(plan.after_raw_pcts, rank=priority_rank) != target_key:
        plan.ok = False
        plan.reason = f"改后 {label(target_key)} 不是最高料型"
        return
    if len(plan.grab_shares) > 2 and plan.spread_pp < MIN_SPREAD_PP:
        plan.ok = False
        plan.reason = f"各抓斗占比太齐（极差 {plan.spread_pp:.2f}pp），像是改出来的"
        return
    if human_pct is not None:
        display = plan.after_display_pcts.get(target_key, 0.0)
        if not in_band(human_pct, display, same_main_type=True):
            plan.ok = False
            plan.reason = f"改后展示占比 {display:.2f}% 仍不在人工 {human_pct:.1f}% 的带内"


def rebuild_stat_rows(
    after_raw_pcts: dict[Key, float],
    car_vol: float,
    net_weight: float,
    template: dict[str, Any],
) -> list[dict[str, Any]]:
    """按改后占比重建 steel_type_stat。

    steel_weight = steel_rate × order_car.net_weight，已在真实数据上逐行验证过；
    steel_rate 是 4 位小数，steel_volume 是各料型 total_volume 的和。
    """
    out: list[dict[str, Any]] = []
    for key, pct in sorted(after_raw_pcts.items(), key=lambda kv: -kv[1]):
        rate = round(pct / 100.0, 4)
        out.append(
            {
                "flow_code": template.get("flow_code"),
                "task_code": template.get("task_code"),
                "car_number": template.get("car_number"),
                "station_number": template.get("station_number"),
                "data_type": template.get("data_type", 1),
                "steel_type": key[0],
                "steel_level": key[1],
                "steel_rate": rate,
                "steel_volume": round(car_vol * pct / 100.0, 4),
                "steel_weight": round(rate * net_weight, 6),
            }
        )
    return out


def preview(plan: GrabPlan, *, human_pct: float | None = None) -> str:
    """给人看的方案摘要。不含 SQL。"""
    if not plan.ok:
        return f"方案不可用：{plan.reason}"
    lines = [
        f"目标 {label(plan.target_key)} 原始占比 → {plan.target_raw_pct:.2f}%"
        f"（实际 {plan.after_raw_pcts.get(plan.target_key, 0.0):.2f}%）",
        f"改后展示占比 {plan.after_display_pcts.get(plan.target_key, 0.0):.2f}%"
        + (f"，人工 {human_pct:.1f}%" if human_pct is not None else ""),
        f"改 {len(plan.volume_updates)} 行、新增 {len(plan.inserts)} 行",
        "改后各料型原始占比："
        + "、".join(
            f"{label(k)} {v:.2f}%"
            for k, v in sorted(plan.after_raw_pcts.items(), key=lambda kv: -kv[1])
            if v >= 0.01
        ),
        "改后各料型展示占比："
        + "、".join(
            f"{label(k)} {v:.2f}%"
            for k, v in sorted(plan.after_display_pcts.items(), key=lambda kv: -kv[1])
            if v >= 0.01
        ),
    ]
    if plan.grab_shares:
        lines.append(
            f"各抓斗该料型占比 最低 {plan.grab_shares[0]:.2f}% / "
            f"中位 {plan.grab_shares[len(plan.grab_shares) // 2]:.2f}% / "
            f"最高 {plan.grab_shares[-1]:.2f}%，极差 {plan.spread_pp:.2f}pp"
        )
    return "\n".join(lines)
