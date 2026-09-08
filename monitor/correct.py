"""Volume move + hunliao flip. Per-grab volumes stay uneven; deduct_weight untouched."""

from __future__ import annotations

import hashlib
import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from monitor.config import (
    DISPLAY_RULE_SWITCH,
    HUNLIAO_AFTER_CAP,
    HUNLIAO_MIX_THRESHOLD,
)

SINK_TYPES = (2, 3, 13)


def _rng(seed: str) -> random.Random:
    digest = hashlib.md5(seed.encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def _f(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    return float(value)


def _i(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def display_boost_pp(raw_pct: float, day: date) -> float:
    """Non-mix page boost in percentage points. Mix cars must not call this."""
    x = float(raw_pct)
    if day >= DISPLAY_RULE_SWITCH:
        if 40.0 <= x < 50.0:
            return 50.0
        if 50.0 <= x < 60.0:
            return 40.0
        if 60.0 <= x < 70.0:
            return 30.0
        if 70.0 <= x < 80.0:
            return 20.0
        if 80.0 <= x < 90.0:
            return 10.0
        return 0.0
    if 50.0 <= x < 60.0:
        return 30.0
    if 60.0 <= x < 80.0:
        return 20.0
    if 80.0 <= x < 90.0:
        return 10.0
    return 0.0


def display_main_pct(raw_pct: float, day: date, *, is_mix: bool) -> tuple[float, float]:
    """Page-visible main-type %. Mix = raw. Non-mix = raw + interval boost, capped at 100."""
    if is_mix:
        return float(raw_pct), 0.0
    boost = display_boost_pp(raw_pct, day)
    return min(100.0, float(raw_pct) + boost), boost


def car_type_pcts(rows: list[dict[str, Any]]) -> dict[int, float]:
    vols: dict[int, float] = defaultdict(float)
    for row in rows:
        vols[int(row["steel_type"])] += _f(row.get("total_volume"))
    total = sum(vols.values())
    if total <= 0:
        return {}
    return {k: v / total * 100.0 for k, v in vols.items()}


def hunliao_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_uuid: dict[str, int] = {}
    for row in rows:
        uid = str(row["uuid"])
        flag = row.get("is_hunliao")
        prev = by_uuid.get(uid, 0)
        if flag is None:
            by_uuid[uid] = max(prev, 0)
        else:
            by_uuid[uid] = max(prev, 1 if int(flag) == 1 else 0)
    total = len(by_uuid)
    hun = sum(1 for v in by_uuid.values() if v == 1)
    ratio = hun / total if total else 0.0
    return {
        "uuid_total": total,
        "hun_uuids": hun,
        "ratio": ratio,
        "is_mix": ratio >= HUNLIAO_MIX_THRESHOLD,
        "by_uuid": by_uuid,
    }


def min_mix_uuid_count(total: int) -> int:
    """Smallest k such that k/total >= 55%."""
    if total <= 0:
        return 0
    return min(total, math.ceil(HUNLIAO_MIX_THRESHOLD * total - 1e-12))


@dataclass
class HunliaoFlipPlan:
    uuids: list[str]
    mix_after: float
    ok: bool = True
    reason: str = ""


def pick_hunliao_flip(
    rows: list[dict[str, Any]],
    seed: str,
) -> list[str]:
    """Compat: only the uuid list. Prefer plan_hunliao_flip."""
    return plan_hunliao_flip(rows, seed).uuids


def plan_hunliao_flip(rows: list[dict[str, Any]], seed: str) -> HunliaoFlipPlan:
    """Flip the fewest non-mix grabs so mix ratio just crosses 55%. Never chase 80%/100%."""
    stats = hunliao_stats(rows)
    total = stats["uuid_total"]
    hun = stats["hun_uuids"]
    if total <= 0:
        return HunliaoFlipPlan([], 0.0, ok=False, reason="没有抓斗")
    if stats["is_mix"]:
        return HunliaoFlipPlan([], stats["ratio"])
    need = min_mix_uuid_count(total)
    mix_after = need / total
    if mix_after >= HUNLIAO_AFTER_CAP:
        return HunliaoFlipPlan(
            [],
            stats["ratio"],
            ok=False,
            reason=f"抓斗太少，刚过55%也会到 {mix_after:.0%}，不改成混料",
        )
    take = need - hun
    if take <= 0:
        return HunliaoFlipPlan([], stats["ratio"])
    candidates = [uid for uid, flag in stats["by_uuid"].items() if flag != 1]
    rng = _rng(f"hunliao|{seed}")
    rng.shuffle(candidates)
    chosen = candidates[:take]
    after = (hun + len(chosen)) / total
    # 最多刚过线一档，禁止一次翻到 80%+
    if after >= HUNLIAO_AFTER_CAP:
        return HunliaoFlipPlan(
            [],
            stats["ratio"],
            ok=False,
            reason=f"改后混料抓斗会到 {after:.0%}，超过上限",
        )
    return HunliaoFlipPlan(chosen, after)


@dataclass
class VolumePlan:
    volume_updates: dict[int, float] = field(default_factory=dict)
    hunliao_ids: list[int] = field(default_factory=list)
    inserts: list[dict[str, Any]] = field(default_factory=list)
    after_pcts: dict[int, float] = field(default_factory=dict)
    grab_samples: list[float] = field(default_factory=list)
    ok: bool = True
    reason: str = ""


# 附属料型行只记体积，不能再挂主行的原图 / 混料开关 / 扣杂。
_SATELLITE_CLEAR = (
    "deduct_weight",
    "deduct_weight_url",
    "origin_image_url",
    "is_hunliao",
    "steel_count",
    "description",
    "chao_biao",
    "total_volume_old",
)


def pick_clone_source(siblings: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Prefer a sibling that already has the shared render image."""
    if not siblings:
        return None
    with_images = [r for r in siblings if r.get("images")]
    return with_images[0] if with_images else siblings[0]


def _clone_insert(src: dict[str, Any], steel_type: int, volume: float) -> dict[str, Any]:
    """New steel_type row for an existing uuid: copy images, never copy origin/hunliao."""
    row = dict(src)
    row.pop("id", None)
    row["steel_type"] = steel_type
    row["total_volume"] = round(volume, 4)
    for key in _SATELLITE_CLEAR:
        row[key] = None
    return row


def rebuild_inserts(
    planned: list[dict[str, Any]],
    live_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rebuild INSERT rows from live siblings so images come from the same uuid."""
    by_uuid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in live_rows:
        by_uuid[str(row["uuid"])].append(row)
    out: list[dict[str, Any]] = []
    for ins in planned:
        uid = str(ins["uuid"])
        src = pick_clone_source(by_uuid.get(uid) or [])
        if src is None:
            raise RuntimeError(f"找不到 uuid {uid} 的已有行，无法新增料型")
        if not src.get("images"):
            raise RuntimeError(f"uuid {uid} 已有行没有 images，拒绝插入空图行")
        out.append(_clone_insert(src, int(ins["steel_type"]), float(ins["total_volume"])))
    return out


def plan_volume(
    rows: list[dict[str, Any]],
    target_type: int,
    target_pct: float,
    seed: str,
) -> VolumePlan:
    """Move volume so car-level target_type ≈ target_pct, grabs stay uneven."""
    plan = VolumePlan()
    if not rows:
        plan.ok = False
        plan.reason = "没有 AI 明细行"
        return plan

    by_uuid: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        uid = str(row["uuid"])
        st = int(row["steel_type"])
        by_uuid[uid][st] = row

    car_vol = sum(_f(r.get("total_volume")) for r in rows)
    if car_vol <= 0:
        plan.ok = False
        plan.reason = "总体积为 0"
        return plan

    target_frac = target_pct / 100.0
    want = car_vol * target_frac
    have = sum(_f(r.get("total_volume")) for r in rows if int(r["steel_type"]) == target_type)
    delta = want - have
    other_cap_frac = max(0.0, target_frac - 0.02)
    sinks = [t for t in SINK_TYPES if t != target_type]
    rng = _rng(f"vol|{seed}|{target_type}|{target_pct:.4f}")

    new_vol: dict[tuple[str, int], float] = {}
    for uid, types in by_uuid.items():
        for st, row in types.items():
            new_vol[(uid, st)] = _f(row.get("total_volume"))

    def uuid_total(uid: str) -> float:
        return sum(v for (u, _st), v in new_vol.items() if u == uid)

    if abs(delta) < car_vol * 1e-6:
        plan.after_pcts = car_type_pcts(rows)
        return plan

    if delta < 0:
        need = -delta
        eligible: list[tuple[str, float, float]] = []
        for uid, types in by_uuid.items():
            tot = uuid_total(uid)
            if tot <= 0 or target_type not in types:
                continue
            tvol = new_vol[(uid, target_type)]
            ratio = tvol / tot
            if ratio < max(target_frac, 0.40):
                continue
            floor = max(0.08, target_frac * (0.55 + 0.5 * rng.random()))
            can = tvol - floor * tot
            if can > tot * 0.01:
                eligible.append((uid, ratio, can))
        eligible.sort(key=lambda x: x[1], reverse=True)
        if not eligible:
            plan.ok = False
            plan.reason = "没有可下调的偏高抓斗"
            return plan
        remain = need
        for uid, _ratio, can in eligible:
            if remain <= 1:
                break
            take = min(can, remain) * (0.72 + 0.28 * rng.random())
            take = min(take, can, remain)
            if take <= 0:
                continue
            new_vol[(uid, target_type)] -= take
            remain -= take
            parts = _split_to_sinks(uid, by_uuid[uid], sinks, take, rng)
            for st, add in parts.items():
                key = (uid, st)
                if key in new_vol:
                    new_vol[key] += add
                else:
                    new_vol[key] = add
        if remain > need * 0.08:
            plan.ok = False
            plan.reason = f"下调体积不够，还差 {remain:.1f}"
            return plan
    else:
        need = delta
        remain = need
        for _pass in range(3):
            if remain <= 1:
                break
            donors: list[tuple[str, int, float]] = []
            for uid, types in by_uuid.items():
                for st in types:
                    if st == target_type:
                        continue
                    vol = new_vol.get((uid, st), 0.0)
                    if vol > 1:
                        donors.append((uid, st, vol))
            donors.sort(key=lambda x: x[2], reverse=True)
            if not donors:
                break
            for uid, st, vol in donors:
                if remain <= 1:
                    break
                frac = 0.55 + 0.4 * rng.random() if _pass == 0 else 0.92
                take = min(vol * frac, remain, vol * 0.98)
                if take <= 0:
                    continue
                new_vol[(uid, st)] -= take
                remain -= take
                tkey = (uid, target_type)
                if tkey in new_vol:
                    new_vol[tkey] += take
                elif target_type in by_uuid[uid]:
                    new_vol[tkey] = _f(by_uuid[uid][target_type].get("total_volume")) + take
                else:
                    src = pick_clone_source(list(by_uuid[uid].values()))
                    if src is None:
                        plan.ok = False
                        plan.reason = f"uuid {uid} 没有可克隆的行"
                        return plan
                    plan.inserts.append(_clone_insert(src, target_type, take))
                    new_vol[tkey] = take
        if remain > need * 0.12:
            plan.ok = False
            plan.reason = f"上调体积不够，还差 {remain:.1f}"
            return plan

    _enforce_other_cap(new_vol, by_uuid, target_type, other_cap_frac, sinks, rng, plan)

    type_vol: dict[int, float] = defaultdict(float)
    for (uid, st), vol in new_vol.items():
        type_vol[st] += vol
        orig = by_uuid.get(uid, {}).get(st)
        if orig is None:
            if not any(
                ins.get("uuid") == uid and int(ins["steel_type"]) == st for ins in plan.inserts
            ):
                src = pick_clone_source(list(by_uuid[uid].values()))
                if src is None:
                    plan.ok = False
                    plan.reason = f"uuid {uid} 没有可克隆的行"
                    return plan
                plan.inserts.append(_clone_insert(src, st, vol))
            continue
        old = _f(orig.get("total_volume"))
        if abs(vol - old) >= 0.01:
            plan.volume_updates[int(orig["id"])] = round(vol, 4)

    total = sum(type_vol.values())
    plan.after_pcts = {k: v / total * 100.0 for k, v in type_vol.items()} if total else {}
    main_after = plan.after_pcts.get(target_type, 0.0)
    others = [v for k, v in plan.after_pcts.items() if k != target_type]
    if others and main_after <= max(others) + 1e-6:
        plan.ok = False
        plan.reason = f"改后主料型 {main_after:.2f}% 不再最高"
        return plan

    samples: list[float] = []
    for uid, types in by_uuid.items():
        tot = uuid_total(uid)
        if tot <= 0:
            continue
        samples.append(new_vol.get((uid, target_type), 0.0) / tot * 100.0)
    samples.sort()
    if samples:
        plan.grab_samples = [
            round(samples[0], 2),
            round(samples[len(samples) // 2], 2),
            round(samples[-1], 2),
        ]
    return plan


def _split_to_sinks(
    uid: str,
    types: dict[int, dict[str, Any]],
    sinks: list[int],
    amount: float,
    rng: random.Random,
) -> dict[int, float]:
    existing = [t for t in sinks if t in types]
    pool = existing or list(sinks)
    weights = [0.35 + rng.random() for _ in pool]
    wsum = sum(weights)
    out: dict[int, float] = {}
    remain = amount
    for i, st in enumerate(pool):
        if i == len(pool) - 1:
            part = remain
        else:
            part = amount * (weights[i] / wsum)
            remain -= part
        if part > 0:
            out[st] = part
    return out


def _enforce_other_cap(
    new_vol: dict[tuple[str, int], float],
    by_uuid: dict[str, dict[int, dict[str, Any]]],
    target_type: int,
    other_cap_frac: float,
    sinks: list[int],
    rng: random.Random,
    plan: VolumePlan,
) -> None:
    car = sum(new_vol.values())
    if car <= 0:
        return
    type_vol: dict[int, float] = defaultdict(float)
    for (_uid, st), vol in new_vol.items():
        type_vol[st] += vol
    for st, vol in list(type_vol.items()):
        if st == target_type:
            continue
        cap = car * other_cap_frac
        extra = vol - cap
        if extra <= car * 0.002:
            continue
        keys = [(uid, s) for (uid, s), v in new_vol.items() if s == st and v > 0]
        keys.sort(key=lambda k: new_vol[k], reverse=True)
        remain = extra
        dests = [t for t in sinks if t != st]
        for key in keys:
            if remain <= 1:
                break
            take = min(new_vol[key] * 0.5, remain)
            if take <= 0:
                continue
            new_vol[key] -= take
            remain -= take
            uid = key[0]
            parts = _split_to_sinks(uid, by_uuid[uid], dests or [target_type], take, rng)
            for dst, add in parts.items():
                dkey = (uid, dst)
                new_vol[dkey] = new_vol.get(dkey, 0.0) + add


def apply_hunliao_ids(rows: list[dict[str, Any]], flip_uuids: list[str]) -> list[int]:
    """One UPDATE per uuid: only the row that already has is_hunliao set."""
    wanted = set(flip_uuids)
    picked: dict[str, dict[str, Any]] = {}

    def _score(row: dict[str, Any]) -> tuple[int, int, int]:
        has_origin = 0 if row.get("origin_image_url") else 1
        type_pref = 0 if int(row["steel_type"]) == 1 else 1
        return (has_origin, type_pref, int(row["id"]))

    for row in rows:
        uid = str(row["uuid"])
        if uid not in wanted or row.get("is_hunliao") is None:
            continue
        prev = picked.get(uid)
        if prev is None or _score(row) < _score(prev):
            picked[uid] = row
    return [int(row["id"]) for row in picked.values()]


def pick_target_with_jitter(avg_pct: float, seed: str) -> tuple[float, float]:
    rng = _rng(f"delta|{seed}|{avg_pct:.4f}")
    delta = 0.0
    for _ in range(12):
        delta = rng.uniform(-9.7, 9.7)
        if abs(delta) >= 0.3:
            break
    low = max(0.5, avg_pct - 9.7)
    high = min(99.5, avg_pct + 9.7)
    if low >= high:
        return max(0.5, min(99.5, avg_pct)), 0.0
    target = min(high, max(low, avg_pct + delta))
    return target, target - avg_pct


def clamp_still_highest(target: float, avg: float, other_max: float) -> float | None:
    """Keep target in the ±10 band. other_max only lifts it when that still fits.

    Do not abort when the *current* runner-up sits above the band: plan_volume
    can steal from today's leader, so after-edit other_max is often lower.
    """
    low = max(0.5, avg - 9.7)
    high = min(99.5, avg + 9.7)
    if low >= high:
        return None
    need = other_max + 0.15
    preferred = max(target, need) if need < high else target
    return min(high, max(low, preferred))
