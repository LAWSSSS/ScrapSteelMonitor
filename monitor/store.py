"""Local JSON state, pending tickets, heartbeat, logs."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from monitor.config import KIND_DEDUCT, KIND_VOLUME, ROOT, TZ, ensure_dirs

logger = logging.getLogger(__name__)


def _json_dump(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _json_load(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def card_kind(card: dict[str, Any] | None) -> str:
    if not card:
        return KIND_VOLUME
    return str(card.get("kind") or KIND_VOLUME)


def ticket_key(weight_bill_no: str, day: str, kind: str = KIND_VOLUME) -> str:
    if kind == KIND_VOLUME:
        return f"{weight_bill_no}|{day}"
    return f"{weight_bill_no}|{day}|{kind}"


def processed_path() -> Path:
    return ROOT / "state" / "processed_tickets.json"


def load_processed() -> dict[str, Any]:
    ensure_dirs()
    data = _json_load(processed_path(), {})
    return data if isinstance(data, dict) else {}


def save_processed(data: dict[str, Any]) -> None:
    _json_dump(processed_path(), data)


def is_processed(weight_bill_no: str, day: str, kind: str = KIND_VOLUME) -> bool:
    return ticket_key(weight_bill_no, day, kind) in load_processed()


def mark_processed(
    weight_bill_no: str,
    day: str,
    payload: dict[str, Any],
    kind: str = KIND_VOLUME,
) -> None:
    data = load_processed()
    data[ticket_key(weight_bill_no, day, kind)] = payload
    save_processed(data)


def unmark_processed(weight_bill_no: str, day: str, kind: str = KIND_VOLUME) -> None:
    data = load_processed()
    data.pop(ticket_key(weight_bill_no, day, kind), None)
    save_processed(data)


def pending_dir() -> Path:
    ensure_dirs()
    return ROOT / "pending"


def pending_path(weight_bill_no: str, day: str, kind: str = KIND_VOLUME) -> Path:
    safe = weight_bill_no.replace("/", "_")
    if kind == KIND_VOLUME:
        return pending_dir() / f"{day}_{safe}.json"
    return pending_dir() / f"{day}_{safe}_{kind}.json"


def save_pending(card: dict[str, Any]) -> Path:
    kind = card_kind(card)
    path = pending_path(card["weight_bill_no"], card["biz_date"], kind)
    _json_dump(path, card)
    return path


def load_pending(
    weight_bill_no: str,
    day: str,
    kind: str = KIND_VOLUME,
) -> dict[str, Any] | None:
    path = pending_path(weight_bill_no, day, kind)
    if not path.is_file():
        return None
    data = _json_load(path, None)
    return data if isinstance(data, dict) else None


def remove_pending(weight_bill_no: str, day: str, kind: str = KIND_VOLUME) -> None:
    path = pending_path(weight_bill_no, day, kind)
    if path.is_file():
        path.unlink()


def list_pending(
    day: str | None = None,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for path in sorted(pending_dir().glob("*.json")):
        data = _json_load(path, None)
        if not isinstance(data, dict):
            continue
        if day and data.get("biz_date") != day:
            continue
        if kind and card_kind(data) != kind:
            continue
        cards.append(data)
    return cards


MODE_CHOOSE = "choose"
MODE_WATCH = "watch-today"
MODE_WATCH_AUTO = "watch-today-auto"
MODE_REVIEW = "review-history"
VALID_UI_MODES = (MODE_CHOOSE, MODE_WATCH, MODE_WATCH_AUTO, MODE_REVIEW)


def is_watch_mode(mode: str | None) -> bool:
    return mode in {MODE_WATCH, MODE_WATCH_AUTO}


def ui_mode_path() -> Path:
    return ROOT / "state" / "ui_mode.json"


def load_ui_mode() -> dict[str, Any]:
    ensure_dirs()
    data = _json_load(ui_mode_path(), {})
    if not isinstance(data, dict):
        data = {}
    mode = data.get("mode")
    if mode not in VALID_UI_MODES:
        mode = "choose"
    return {
        "mode": mode,
        "date": str(data.get("date") or ""),
        "updated_at": str(data.get("updated_at") or ""),
    }


def save_ui_mode(mode: str, day: str = "") -> dict[str, Any]:
    if mode not in VALID_UI_MODES:
        raise ValueError(f"未知工作方式: {mode}")
    payload = {
        "mode": mode,
        "date": day,
        "updated_at": datetime.now(TZ).isoformat(timespec="seconds"),
    }
    _json_dump(ui_mode_path(), payload)
    return payload


def write_heartbeat(extra: dict[str, Any] | None = None) -> None:
    ensure_dirs()
    payload = {
        "ts": datetime.now(TZ).isoformat(timespec="seconds"),
        **(extra or {}),
    }
    _json_dump(ROOT / "state" / "heartbeat.json", payload)


def read_heartbeat() -> dict[str, Any]:
    data = _json_load(ROOT / "state" / "heartbeat.json", {})
    return data if isinstance(data, dict) else {}


def write_scan_summary(day: str, summary: dict[str, Any]) -> None:
    ensure_dirs()
    _json_dump(ROOT / "state" / f"scan_{day}.json", summary)


def load_scan_summary(day: str) -> dict[str, Any]:
    data = _json_load(ROOT / "state" / f"scan_{day}.json", {})
    return data if isinstance(data, dict) else {}


def append_log(event: str, payload: dict[str, Any]) -> None:
    ensure_dirs()
    day = datetime.now(TZ).date().isoformat()
    path = ROOT / "logs" / f"{day}.jsonl"
    rec = {"ts": datetime.now(TZ).isoformat(timespec="seconds"), "event": event, **payload}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    logger.info("%s %s", event, payload.get("reason") or payload.get("weight_bill_no") or "")


def recent_logs(limit: int = 40) -> list[dict[str, Any]]:
    ensure_dirs()
    rows: list[dict[str, Any]] = []
    files = sorted((ROOT / "logs").glob("*.jsonl"))[-3:]
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows[-limit:]
