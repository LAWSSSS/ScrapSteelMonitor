"""镔鑫修正方案试跑：连真库、出方案、逐条自检，一个字都不写库。

只读连接，没有 execute_guarded 的调用路径。跑法：

    BINXIN_PG_PASSWORD=<密码> python3 -m monitor.tools.binxin_dry_run --days 30
    BINXIN_PG_PASSWORD=<密码> python3 -m monitor.tools.binxin_dry_run --car 苏GC7733

自检的是「方案落到行上以后，重新按库里的算法复算一遍，结果对不对」，而不是信方案自己的报数。
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from monitor.binxin import db, scan
from monitor.binxin.correct import (
    GrabPlan,
    car_key_pcts,
    plan_grabs,
    preview,
    rebuild_stat_rows,
)
from monitor.binxin.materials import label
from monitor.binxin.rate import in_band, main_key, normalize
from monitor.binxin.materials import priority_rank

logger = logging.getLogger(__name__)


@dataclass
class Checked:
    """一车的试跑结果。problems 非空就说明方案不能用。"""

    vehicle: scan.Vehicle
    plan: GrabPlan | None
    stat_rows: list[dict[str, Any]]
    problems: list[str]


def _verify(
    vehicle: scan.Vehicle,
    rows: list[dict[str, Any]],
    plan: GrabPlan,
) -> list[str]:
    """独立复算。不看 plan 自己报的数，重新从行上算。"""
    problems: list[str] = []
    after_rows = []
    for row in rows:
        clone = dict(row)
        if int(row["id"]) in plan.volume_updates:
            clone["total_volume"] = plan.volume_updates[int(row["id"])]
        after_rows.append(clone)
    after_rows.extend(dict(ins) for ins in plan.inserts)

    before_vol = sum(float(r.get("total_volume") or 0) for r in rows)
    after_vol = sum(float(r.get("total_volume") or 0) for r in after_rows)
    if abs(after_vol - before_vol) > max(1.0, before_vol * 1e-6):
        problems.append(f"总体积变了：{before_vol:.0f} → {after_vol:.0f}")

    recomputed = car_key_pcts(after_rows)
    target = plan.target_key
    got = recomputed.get(target, 0.0)
    if abs(got - plan.target_raw_pct) > 0.35:
        problems.append(f"复算原始占比 {got:.2f}% 偏离目标 {plan.target_raw_pct:.2f}%")
    if main_key(recomputed, rank=priority_rank) != target:
        problems.append(f"复算后 {label(target)} 不是最高料型")

    display = normalize(recomputed, quantize=False).get(target, 0.0)
    human_pct = vehicle.human_pcts.get(target, 0.0)
    if not in_band(human_pct, display, same_main_type=True):
        problems.append(f"复算展示占比 {display:.2f}% 不在人工 {human_pct:.1f}% 的带内")

    for ins in plan.inserts:
        if not ins.get("images"):
            problems.append(f"新增行没有 images（uuid {ins.get('uuid')}）")
        if ins.get("deduct_weight"):
            problems.append(f"新增行带了 deduct_weight（uuid {ins.get('uuid')}）")

    before_deduct = sum(float(r.get("deduct_weight") or 0) for r in rows)
    after_deduct = sum(float(r.get("deduct_weight") or 0) for r in after_rows)
    if abs(after_deduct - before_deduct) > 1e-6:
        problems.append(f"整车扣重变了：{before_deduct:.2f} → {after_deduct:.2f}")

    negative = [rid for rid, v in plan.volume_updates.items() if v < 0]
    if negative:
        problems.append(f"{len(negative)} 行体积被改成负数")
    return problems


def run_day(day: date) -> list[Checked]:
    """扫一天，给每辆要修正的车出方案并自检。只读。"""
    out: list[Checked] = []
    with db.session():
        cars = db.load_day_cars(day)
        flows = [str(c["flow_code"]) for c in cars]
        stats = db.load_steel_type_stats(flows)
        manuals = db.load_manual_details([str(c["order_code"]) for c in cars])
        grabs = db.count_grabs(flows)
        for car in cars:
            flow = str(car["flow_code"])
            vehicle = scan.judge(
                car,
                stats.get(flow, []),
                manuals.get(str(car["order_code"]), []),
                grabs.get(flow, 0),
            )
            if vehicle.plan is None:
                out.append(Checked(vehicle, None, [], []))
                continue
            rows = db.load_check_rows(flow, include_images=True)
            plan = plan_grabs(
                rows,
                vehicle.plan.target_key,
                vehicle.plan.target_raw_pct,
                seed=f"{vehicle.car_number}|{flow}",
                human_pct=vehicle.plan.human_pct,
            )
            problems = _verify(vehicle, rows, plan) if plan.ok else [plan.reason]
            stat_rows = (
                rebuild_stat_rows(
                    plan.after_raw_pcts,
                    car_vol=sum(float(r.get("total_volume") or 0) for r in rows),
                    net_weight=float(car.get("net_weight") or 0.0),
                    template={
                        "flow_code": flow,
                        "task_code": rows[0].get("task_code") if rows else None,
                        "car_number": vehicle.car_number,
                        "station_number": car.get("station_number"),
                    },
                )
                if plan.ok
                else []
            )
            out.append(Checked(vehicle, plan, stat_rows, problems))
    return out


def _print_car(item: Checked) -> None:
    v = item.vehicle
    print(f"\n{'=' * 72}\n{v.car_number}  {v.checked_at}  {v.grabs} 抓斗  flow={v.flow_code}")
    print(
        f"  人工主料型 {label(v.human_main) if v.human_main else '—'} "
        f"{v.human_pcts.get(v.human_main, 0.0):.2f}%"
        if v.human_main
        else "  人工主料型 —"
    )
    print(
        f"  AI 主料型 {label(v.ai_main) if v.ai_main else '—'} "
        f"原始 {v.ai_raw_pcts.get(v.ai_main, 0.0):.2f}% / "
        f"展示 {v.ai_display_pcts.get(v.ai_main, 0.0):.2f}%"
        if v.ai_main
        else "  AI 主料型 —"
    )
    print(f"  差异率 {v.diff_rate_pct:.1f}%" if v.diff_rate_pct is not None else "  差异率 —")
    if item.plan is None:
        print(f"  结论：{'在带内' if v.ok else v.skip_reason}")
        return
    print(f"  修正方式：{v.plan.method if v.plan else ''}")
    for line in preview(item.plan, human_pct=v.plan.human_pct if v.plan else None).splitlines():
        print(f"  {line}")
    if item.stat_rows:
        print("  重建 steel_type_stat：")
        for row in item.stat_rows:
            print(
                f"    {label((row['steel_type'], row['steel_level']))}: "
                f"rate={row['steel_rate']} weight={row['steel_weight']} "
                f"volume={row['steel_volume']}"
            )
    print(f"  自检：{'通过' if not item.problems else '；'.join(item.problems)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="镔鑫修正方案试跑，只读不写库")
    parser.add_argument("--days", type=int, default=7, help="往前扫几天")
    parser.add_argument("--car", default="", help="只看某个车牌")
    parser.add_argument("--detail", action="store_true", help="连在带内和出局的车也打印")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    today = date.today()
    checked: list[Checked] = []
    for offset in range(args.days):
        day = today - timedelta(days=offset)
        try:
            checked.extend(run_day(day))
        except Exception as exc:  # 单天失败不该毁掉整轮
            print(f"{day} 扫描失败：{db.format_db_error(exc)}")
    if args.car:
        checked = [c for c in checked if c.vehicle.car_number == args.car]

    planned = [c for c in checked if c.plan is not None]
    for item in checked if args.detail else planned:
        _print_car(item)

    counts = scan.summarize([c.vehicle for c in checked])
    bad = [c for c in planned if c.problems]
    print(f"\n{'=' * 72}")
    print(f"共 {len(checked)} 车：" + "、".join(f"{k} {v}" for k, v in counts.items() if v))
    print(f"出了方案 {len(planned)} 车，自检通过 {len(planned) - len(bad)}，不通过 {len(bad)}")
    for item in bad:
        print(f"  {item.vehicle.car_number}: {'；'.join(item.problems)}")
    print("没有写库：本次只用了只读连接。")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
