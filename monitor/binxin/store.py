"""镔鑫本地状态：待审队列、已处理标记、扫描摘要、操作日志。全在本机文件里，不进库。

键用 flow_code，因为镔鑫一次进厂检判对应一个 flow_code，车牌会重复、运单号也可能补录。
盛隆那边用的是磅单号，形状一样但键不同，所以没法直接共用一份 store。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from monitor.binxin.config import STATE_ROOT, ensure_dirs
from monitor.config import TZ

logger = logging.getLogger(__name__)


def _dump(path: Path, data: object) -> None:
    """先写临时文件再改名，避免写一半被读到。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _load(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("读不动 %s，当作空", path)
        return default


def ticket_key(flow_code: str, day: str) -> str:
    return f"{flow_code}|{day}"


def processed_path() -> Path:
    return STATE_ROOT / "state" / "processed.json"


def load_processed() -> dict[str, Any]:
    ensure_dirs()
    data = _load(processed_path(), {})
    return data if isinstance(data, dict) else {}


def is_processed(flow_code: str, day: str) -> bool:
    return ticket_key(flow_code, day) in load_processed()


def mark_processed(flow_code: str, day: str, payload: dict[str, Any]) -> None:
    data = load_processed()
    data[ticket_key(flow_code, day)] = {
        "ts": datetime.now(TZ).isoformat(timespec="seconds"),
        **payload,
    }
    _dump(processed_path(), data)


def unmark_processed(flow_code: str, day: str) -> None:
    data = load_processed()
    data.pop(ticket_key(flow_code, day), None)
    _dump(processed_path(), data)


def pending_dir() -> Path:
    ensure_dirs()
    return STATE_ROOT / "pending"


def pending_path(flow_code: str, day: str) -> Path:
    safe = flow_code.replace("/", "_")
    return pending_dir() / f"{day}_{safe}.json"


def save_pending(card: dict[str, Any]) -> Path:
    path = pending_path(str(card["flow_code"]), str(card["biz_date"]))
    _dump(path, card)
    return path


def load_pending(flow_code: str, day: str) -> dict[str, Any] | None:
    data = _load(pending_path(flow_code, day), None)
    return data if isinstance(data, dict) else None


def remove_pending(flow_code: str, day: str) -> None:
    path = pending_path(flow_code, day)
    if path.is_file():
        path.unlink()


def list_pending(day: str | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(pending_dir().glob("*.json")):
        data = _load(path, None)
        if not isinstance(data, dict):
            continue
        if day and str(data.get("biz_date")) != day:
            continue
        out.append(data)
    return out


def summary_path(day: str) -> Path:
    return STATE_ROOT / "state" / f"scan_{day}.json"


def save_scan_summary(day: str, payload: dict[str, Any]) -> None:
    ensure_dirs()
    _dump(summary_path(day), payload)


def load_scan_summary(day: str) -> dict[str, Any]:
    data = _load(summary_path(day), {})
    return data if isinstance(data, dict) else {}


def append_log(event: str, payload: dict[str, Any]) -> None:
    ensure_dirs()
    now = datetime.now(TZ)
    path = STATE_ROOT / "logs" / f"{now.date().isoformat()}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": now.isoformat(timespec="seconds"), "event": event, **payload}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    logger.info("%s %s", event, payload.get("car_number") or payload.get("reason") or "")


def recent_logs(limit: int = 40) -> list[dict[str, Any]]:
    ensure_dirs()
    out: list[dict[str, Any]] = []
    for path in sorted((STATE_ROOT / "logs").glob("*.jsonl"))[-3:]:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out[-limit:]
