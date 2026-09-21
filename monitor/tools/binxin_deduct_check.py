"""核对镔鑫扣重模型：deduct.py 算出来的数和库里的对不对，以及合格分类。

只读，不写库。两个用途：

1. --verify：在一段时间的全部车上复核两条公式
     修正扣杂 = 原始扣杂×1000 + 主料型附加常数
     最终扣重 = 核算扣杂 + 气割渣焊渣 + 天气扣重
   任何一条对不上都要打出来看，不能默认模型是对的。
2. --scan：跑一天完整的扫描（占比 + 扣重），看判定和方案。

用法：
    python -m monitor.tools.binxin_deduct_check --verify --since 2026-07-21
    python -m monitor.tools.binxin_deduct_check --scan 2026-09-19
"""

from __future__ import annotations

import argparse
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime
from typing import Any

from monitor.binxin import db, scan
from monitor.binxin.deduct import (
    ADDITIONAL_KG,
    RAW_DEDUCT_CAP_KG,
    Deduct,
    deduct_key,
    is_ok,
    pass_window,
    plan_deduct,
)
from monitor.binxin.materials import label

#: 附加常数 +100 全部落地之后的起点。
#:
#: 不是一次部署：精炉料1 在 2026-07-21 13:05 换到 165，精炉料2 到 07-23 才换到 250（07-22
#: 还有车是 150），精炉料3 07-29、重废1 07-28。也就是按料型逐个上线。2026-08-01 起四个
#: 料型都没有旧值了，所以复核默认从这天开始，免得把过渡期的车当成对不上。
SETTLED_FROM = "2026-08-01"


def _main_unmerged(stat_rows: list[dict[str, Any]]) -> tuple[int, int] | None:
    """占比最高的那一行的 (steel_type, steel_level)，不并级。后端查表看的就是这一行。"""
    rows = scan.primary_stat_group(stat_rows)
    if not rows:
        return None
    best = max(rows, key=lambda r: float(r.get("steel_volume") or 0.0))
    return (int(best.get("steel_type") or 0), int(best.get("steel_level") or 0))


def verify(since: str) -> int:
    """复核两条公式，返回对不上的车数。"""
    with db.session():
        rows = db.fetch_all(
            """
            SELECT i.flow_code, i.car_number, i.create_time,
                   i.deduct_weight, i.modified_deduct_weight, i.calculated_deduct_weight,
                   i.qigezha_hanzha_weight, i.final_deduct,
                   COALESCE(w.deduct_weight, 0) AS weather_kg,
                   oc.order_code, oc.net_weight
            FROM intelli_task_info i
            LEFT JOIN car_weather_deduct w ON w.flow_code = i.flow_code
            LEFT JOIN order_car oc ON oc.flow_code = i.flow_code
            WHERE i.create_time >= %s
            ORDER BY i.create_time
            """,
            (since,),
        )
        flows = [str(r["flow_code"]) for r in rows]
        bills = [str(r["order_code"]) for r in rows if r["order_code"]]
        stats = db.load_steel_type_stats(flows)
        manual = db.load_manual_deducts(bills)

    bad_modified: list[str] = []
    bad_final: list[str] = []
    no_const: list[str] = []
    seen = 0
    for row in rows:
        flow = str(row["flow_code"])
        srows = stats.get(flow, [])
        key = _main_unmerged(srows)
        add = ADDITIONAL_KG.get(key) if key else None
        raw_kg = float(row["deduct_weight"] or 0.0) * 1000.0
        mod = row["modified_deduct_weight"]
        calc = row["calculated_deduct_weight"]
        fin = row["final_deduct"]
        tag = f"{row['car_number']} {row['create_time']}"

        if mod is None:
            # AI 没出修正扣杂的车，最终扣重是 0，这类车单独统计不算对不上。
            continue
        seen += 1
        if add is None:
            no_const.append(f"{tag} 主料型 {key}（{label(key) if key else '无'}）不在白名单")
        elif abs(float(mod) - (raw_kg + add)) > 0.02:
            bad_modified.append(
                f"{tag} 主料型 {label(key)} 常数{add:.0f}  原始{raw_kg:.2f} + 常数 = "
                f"{raw_kg + add:.2f}  库里修正 {float(mod):.2f}"
            )
        if calc is not None and fin is not None:
            expect = float(calc) + float(row["qigezha_hanzha_weight"] or 0) + float(row["weather_kg"] or 0)
            if abs(expect - float(fin)) > 0.02:
                bad_final.append(
                    f"{tag} 核算{float(calc):.2f} + 气割渣{float(row['qigezha_hanzha_weight'] or 0):.2f}"
                    f" + 天气{float(row['weather_kg'] or 0):.2f} = {expect:.2f}  库里最终 {float(fin):.2f}"
                )

    print(f"== 公式复核（{since} 起，{seen} 车有修正扣杂）==")
    print(f"  修正扣杂 = 原始×1000 + 主料型常数 ：对不上 {len(bad_modified)} 车")
    for line in bad_modified[:15]:
        print(f"    {line}")
    print(f"  最终扣重 = 核算 + 气割渣 + 天气   ：对不上 {len(bad_final)} 车")
    for line in bad_final[:15]:
        print(f"    {line}")
    if no_const:
        print(f"  主料型不在白名单 {len(no_const)} 车")
        for line in no_const[:10]:
            print(f"    {line}")

    _classify(rows, stats, manual)
    return len(bad_modified) + len(bad_final)


def _classify(
    rows: list[dict[str, Any]],
    stats: dict[str, list[dict[str, Any]]],
    manual: dict[str, dict[str, Any]],
) -> None:
    """按当前口径分类，并给出可修车需要的原始扣杂。"""
    counts: Counter[str] = Counter()
    needs: list[float] = []
    fixable: list[str] = []
    walls: list[str] = []
    for row in rows:
        bill = str(row["order_code"] or "")
        mrow = manual.get(bill)
        if mrow is None or mrow.get("deduction_tonnage") is None:
            counts["无人工扣杂"] += 1
            continue
        human = float(mrow["deduction_tonnage"])
        if human <= 0:
            counts["人工扣杂为0"] += 1
            continue
        srows = stats.get(str(row["flow_code"]), [])
        key = _main_unmerged(srows)
        observed = Deduct(
            raw_kg=float(row["deduct_weight"] or 0.0) * 1000.0,
            modified_kg=None if row["modified_deduct_weight"] is None else float(row["modified_deduct_weight"]),
            calculated_kg=None if row["calculated_deduct_weight"] is None else float(row["calculated_deduct_weight"]),
            final_kg=None if row["final_deduct"] is None else float(row["final_deduct"]),
            qigezha_kg=float(row["qigezha_hanzha_weight"] or 0.0),
            weather_kg=float(row["weather_kg"] or 0.0),
            net_weight_kg=float(row["net_weight"] or 0.0) * 1000.0,
            human_kg=human,
            deduct_key=key if key in ADDITIONAL_KG else None,
        )
        if observed.final_kg is not None and observed.human_kg and observed.ok:
            counts["已合格"] += 1
            continue
        plan, why = plan_deduct(observed, seed=str(row["flow_code"]))
        if plan is not None:
            counts["可修"] += 1
            needs.append(plan.target_raw_kg)
            fixable.append(
                f"{row['car_number']} {str(row['create_time'])[:10]} "
                f"AI{float(observed.final_kg or 0):>7.1f} 人工{human:>6.0f} "
                f"{label(key) if key else '?':<8}常数{plan.additional_kg:>4.0f}  "
                f"原始扣杂 {observed.raw_kg:>5.1f} → {plan.target_raw_kg:>5.1f}kg"
                f"   最终 {plan.target_final_kg:>6.1f}kg"
            )
        else:
            counts[why] += 1
            if observed.final_kg is not None:
                _lo, hi = pass_window(human)
                name = label(key) if key else "无主料型"
                walls.append(
                    f"{row['car_number']} {str(row['create_time'])[:10]} "
                    f"AI{float(observed.final_kg):>7.1f} 人工{human:>6.0f} "
                    f"{name:<8}常数{(ADDITIONAL_KG.get(key) or 0):>4.0f} 上界{hi:>6.1f}  {why}"
                )

    print(f"\n== 合格分类 ==")
    total = sum(counts.values())
    for name, n in counts.most_common():
        print(f"  {name:<30} {n:>4} 车  {n / total * 100:>5.1f}%")
    if fixable:
        print(f"\n== 可修的 {len(fixable)} 辆 ==")
        for line in fixable:
            print(f"  {line}")
        print(f"\n  需要的原始扣杂：中位 {statistics.median(needs):.1f}kg  "
              f"最大 {max(needs):.1f}kg  （上限 {RAW_DEDUCT_CAP_KG}kg）")
    if walls:
        print(f"\n== 修不了的车 {len(walls)} 辆 ==")
        for line in walls[:40]:
            print(f"  {line}")


def scan_one_day(day: date) -> None:
    """跑一天完整扫描，占比和扣重都打出来。只读。"""
    vehicles = scan.scan_day(day)
    print(f"== {day} 扫描到 {len(vehicles)} 车 ==")
    print("\n占比：", scan.summarize(vehicles))
    print("扣重：", scan.summarize_deduct(vehicles))
    kinds = Counter(v.plan_kind for v in vehicles if v.plan_kind)
    print(f"进待审 {sum(kinds.values())} 车：", dict(kinds))

    print(f"\n{'车牌':<10}{'占比结论':<22}{'扣重AI':>8}{'人工':>7}{'差':>7}{'比值':>7}  扣重结论")
    for v in vehicles:
        pct = "在带内" if v.ok else (v.plan.method if v.plan else v.skip_reason)
        d = v.deduct
        if d is None:
            print(f"{v.car_number:<10}{pct:<22}{'—':>8}")
            continue
        ai = d.final_kg
        hu = d.human_kg
        diff = abs(ai - hu) if ai is not None and hu else None
        ratio = ai / hu if ai is not None and hu else None
        if v.deduct_plan is not None:
            verdict = f"改原始扣杂 {d.raw_kg:.1f} → {v.deduct_plan.target_raw_kg:.1f}kg（最终 {v.deduct_plan.target_final_kg:.1f}）"
        elif d.ok:
            verdict = "合格"
        else:
            verdict = v.deduct_skip or "未分类"
        if v.deduct_rewrite:
            verdict += "  ★主料型要变，扣重必须同步改"
        print(
            f"{v.car_number:<10}{pct:<22}"
            f"{(f'{ai:.1f}' if ai is not None else '—'):>8}"
            f"{(f'{hu:.0f}' if hu else '—'):>7}"
            f"{(f'{diff:.1f}' if diff is not None else '—'):>7}"
            f"{(f'{ratio:.2f}' if ratio else '—'):>7}  {verdict}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description="核对镔鑫扣重模型（只读）")
    ap.add_argument("--verify", action="store_true", help="复核公式并分类")
    ap.add_argument(
        "--since",
        default=SETTLED_FROM,
        help=f"复核起点，默认 {SETTLED_FROM}（常数全部换到新值之后）",
    )
    ap.add_argument("--scan", metavar="YYYY-MM-DD", help="跑一天完整扫描")
    args = ap.parse_args()

    if args.verify:
        mismatches = verify(args.since)
        if mismatches:
            print(f"\n有 {mismatches} 处对不上，模型还不能用来改库。")
    if args.scan:
        scan_one_day(datetime.strptime(args.scan, "%Y-%m-%d").date())
    if not args.verify and not args.scan:
        ap.print_help()


if __name__ == "__main__":
    main()
