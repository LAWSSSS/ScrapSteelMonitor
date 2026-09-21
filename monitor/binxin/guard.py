"""写库封锁闸。默认锁死，必须人工解锁才能写镔鑫库。

为什么要有这一层：扫描、出方案、看板、单测都不需要写库，只有"批准某辆车"这一个动作需要。
把开关做成默认关、且需要两个互相独立的条件同时成立，就不会因为某次误调用或者 .env 里
残留的一行配置把真车的检判结果改掉。

两个条件都只能由人来满足：

1. 环境变量 BINXIN_ALLOW_WRITE 等于 "yes-i-approve"。手滑打不出这个值。
2. 解锁文件存在且没过期。由看板上的解锁按钮或 `python3 -m monitor.binxin.guard arm` 写出，
   默认只管 30 分钟，过期自动回到锁死状态。

闸口挂在 db.execute_guarded 上——那是唯一一处会开可写连接的地方，所以以后新增的写路径
也自动被挡住，不需要每处都记得调一次。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from monitor.binxin.config import STATE_ROOT, ensure_dirs
from monitor.config import TZ

logger = logging.getLogger(__name__)

#: 必须等于这个值。写成一句话是为了让"不小心设成 1"不生效。
WRITE_ENV = "BINXIN_ALLOW_WRITE"
WRITE_TOKEN = "yes-i-approve"

#: 解锁默认时长。到点自动锁回去，不依赖谁记得点关。
DEFAULT_ARM_MINUTES = 30
MAX_ARM_MINUTES = 240


class WriteLocked(RuntimeError):
    """写库被封锁。消息里写清楚差哪一步。"""


def arm_path() -> Path:
    return STATE_ROOT / "state" / "write_armed.json"


def _read_arm() -> dict[str, Any]:
    path = arm_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _expiry(data: dict[str, Any]) -> datetime | None:
    text = str(data.get("armed_until") or "")
    if not text:
        return None
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when.replace(tzinfo=TZ) if when.tzinfo is None else when


def env_ok(env: dict[str, str] | None = None) -> bool:
    source = env if env is not None else dict(os.environ)
    return source.get(WRITE_ENV, "") == WRITE_TOKEN


def arm(minutes: int = DEFAULT_ARM_MINUTES, *, who: str = "") -> dict[str, Any]:
    """解锁。只写本地文件，不碰库。"""
    span = max(1, min(int(minutes), MAX_ARM_MINUTES))
    ensure_dirs()
    now = datetime.now(TZ)
    payload = {
        "armed_at": now.isoformat(timespec="seconds"),
        "armed_until": (now + timedelta(minutes=span)).isoformat(timespec="seconds"),
        "minutes": span,
        "who": who,
    }
    path = arm_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.warning("镔鑫写库已解锁 %s 分钟，到 %s 自动锁回", span, payload["armed_until"])
    return payload


def disarm() -> None:
    path = arm_path()
    if path.is_file():
        path.unlink()
    logger.warning("镔鑫写库已锁死")


def status() -> dict[str, Any]:
    """给看板显示的状态。allowed 为真才可能写库。"""
    data = _read_arm()
    until = _expiry(data)
    now = datetime.now(TZ)
    file_ok = until is not None and until > now
    has_env = env_ok()
    blockers: list[str] = []
    if not has_env:
        blockers.append(f"环境变量 {WRITE_ENV} 没设成 {WRITE_TOKEN}")
    if until is None:
        blockers.append("没有解锁文件")
    elif not file_ok:
        blockers.append(f"解锁已于 {until.isoformat(timespec='seconds')} 过期")
    return {
        "allowed": has_env and file_ok,
        "env_ok": has_env,
        "file_ok": file_ok,
        "armed_until": until.isoformat(timespec="seconds") if until else "",
        "seconds_left": int((until - now).total_seconds()) if file_ok and until else 0,
        "blockers": blockers,
    }


def assert_write_allowed() -> None:
    """不满足条件就抛。所有写路径的唯一入口。"""
    state = status()
    if state["allowed"]:
        return
    raise WriteLocked(
        "镔鑫写库处于封锁状态，本次没有改动任何数据。还差："
        + "；".join(state["blockers"])
        + "。解锁方式：看板上点「解锁写库」，或先 export "
        f"{WRITE_ENV}={WRITE_TOKEN} 再跑 python3 -m monitor.binxin.guard arm"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="镔鑫写库封锁闸")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_arm = sub.add_parser("arm", help="解锁写库")
    p_arm.add_argument("--minutes", type=int, default=DEFAULT_ARM_MINUTES)
    p_arm.add_argument("--who", default="")
    sub.add_parser("disarm", help="立刻锁死")
    sub.add_parser("status", help="看当前状态")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.cmd == "arm":
        if not env_ok():
            print(f"没解锁：环境变量 {WRITE_ENV} 必须等于 {WRITE_TOKEN}")
            return 1
        payload = arm(args.minutes, who=args.who)
        print(f"已解锁到 {payload['armed_until']}（{payload['minutes']} 分钟）")
        return 0
    if args.cmd == "disarm":
        disarm()
        print("已锁死")
        return 0
    state = status()
    print("可以写库" if state["allowed"] else "封锁中：" + "；".join(state["blockers"]))
    return 0 if state["allowed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
