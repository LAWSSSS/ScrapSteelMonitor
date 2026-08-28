"""Scan CLI: --date --once (default path) or --watch-today."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monitor.apply import approve
from monitor.config import WATCH_INTERVAL_SEC, parse_day, today_cn
from monitor.scan import scan_day
from monitor.store import list_pending, write_heartbeat
from monitor.vpn_keepalive import start_in_background

logger = logging.getLogger(__name__)


def _run_scan(
    day,
    auto: bool,
    *,
    include_deduct: bool = False,
    max_manuals: int = 2,
) -> dict:
    summary = scan_day(
        day,
        auto=auto,
        include_deduct=include_deduct,
        max_manuals=max_manuals,
    )
    if auto:
        applied = []
        for card in list_pending(day.isoformat(), kind="volume"):
            result = approve(card["weight_bill_no"], card["biz_date"])
            applied.append(
                {
                    "weight_bill_no": card["weight_bill_no"],
                    "plate": card.get("plate"),
                    "insert_ids": result.get("insert_ids"),
                }
            )
        summary["auto_applied"] = applied
    return summary


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(description="废钢检判扫描（默认不写库）")
    parser.add_argument("--date", help="业务日 YYYY-MM-DD")
    parser.add_argument("--once", action="store_true", help="只扫一次")
    parser.add_argument("--watch-today", action="store_true", help="每5分钟扫今天")
    parser.add_argument("--auto", action="store_true", help="跳过审核直接写库")
    parser.add_argument("--interval", type=int, default=WATCH_INTERVAL_SEC)
    parser.add_argument("--no-mouse", action="store_true", help="不启动 VPN 鼠标保活")
    args = parser.parse_args()

    if not args.no_mouse:
        start_in_background()

    if args.watch_today:
        logger.info("当天监控已开 interval=%s auto=%s", args.interval, args.auto)
        while True:
            day = today_cn()
            try:
                summary = _run_scan(day, auto=args.auto, include_deduct=False)
                write_heartbeat(
                    {
                        "last_scan_date": day.isoformat(),
                        "mode": "watch-today",
                        "pending": summary.get("pending"),
                    }
                )
                logger.info(
                    "扫完 %s pending=%s skipped=%s",
                    day,
                    summary.get("pending"),
                    summary.get("skipped"),
                )
            except Exception:
                logger.exception("扫描失败")
            time.sleep(max(30, args.interval))
        return

    if not args.date:
        parser.error(
            "请指定 --date YYYY-MM-DD，或使用 --watch-today。"
            "更直观：打开看板 http://127.0.0.1:8765 选工作方式。"
        )
    day = parse_day(args.date)
    summary = _run_scan(day, auto=args.auto, include_deduct=True, max_manuals=3)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
