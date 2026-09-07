"""Local review UI on 127.0.0.1 only."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monitor.apply import approve, approve_deduct, refresh_deduct, reject
from monitor.config import (
    HEARTBEAT_STALE_SEC,
    KIND_DEDUCT,
    KIND_VOLUME,
    STEEL_NAMES,
    TZ,
    dashboard_bind,
    parse_day,
    relabel_vehicle_steels,
    today_cn,
)
from monitor.db import format_db_error
from monitor.restore import restore, restore_deduct
from monitor.scan import merge_pending_ai, scan_day
from monitor.store import (
    MODE_WATCH,
    MODE_WATCH_AUTO,
    is_processed,
    is_watch_mode,
    list_pending,
    load_processed,
    load_scan_summary,
    load_ui_mode,
    read_heartbeat,
    recent_logs,
    save_ui_mode,
    write_heartbeat,
)
from monitor.vpn_keepalive import read_mouse_status, start_in_background
from monitor.watch import CONTROLLER

logger = logging.getLogger(__name__)
WEB = Path(__file__).resolve().parent / "web" / "index.html"


def _alive_text() -> str:
    hb = read_heartbeat()
    ts = hb.get("ts")
    watch = CONTROLLER.snapshot()
    watching = bool(watch.get("watching"))
    if watching:
        mode = "watch-today-auto" if watch.get("auto") else "watch-today"
    else:
        mode = hb.get("mode") or "dashboard"
    if not ts:
        base = "还没有心跳"
    else:
        try:
            t = datetime.fromisoformat(str(ts))
            if t.tzinfo is None:
                t = t.replace(tzinfo=TZ)
            age = (datetime.now(TZ) - t).total_seconds()
        except ValueError:
            age = 99999
        stale = age > HEARTBEAT_STALE_SEC and watching
        flag = "心跳超时" if stale else "正常"
        base = f"{flag} · 上次 {ts}"
    watch_txt = "当天监控开" if watching else "当天监控关"
    if watch.get("scanning"):
        watch_txt += " · 正在扫描"
    elif watch.get("next_scan_at") and watching:
        watch_txt += f" · 下次 {watch['next_scan_at']}"
    mouse = read_mouse_status()
    if mouse.get("running"):
        last = mouse.get("last_action") or mouse.get("last_shape") or mouse.get("note") or "等待停住"
        vpn = f"VPN鼠标保活开（上次 {last}）"
    elif mouse.get("error"):
        vpn = f"VPN鼠标保活失败：{mouse.get('error')}"
    else:
        vpn = "VPN鼠标保活关"
    return f"{base} · {watch_txt} · {vpn} · {mode}"


def _ui_payload() -> dict:
    saved = load_ui_mode()
    watch = CONTROLLER.snapshot()
    today = today_cn().isoformat()
    mode = saved.get("mode") or "choose"
    if watch.get("watching"):
        mode = MODE_WATCH_AUTO if watch.get("auto") else MODE_WATCH
    day = today if is_watch_mode(mode) else (saved.get("date") or "")
    return {
        "mode": mode,
        "date": day,
        "today": today,
        **watch,
    }


def _notice(day: str, summary: dict, pending_n: int) -> str:
    ui = _ui_payload()
    if ui.get("watching") and ui.get("last_error"):
        return f"扫描出错：{ui['last_error']}。监控仍开着，5 分钟后会再试。"
    if ui["mode"] == "choose":
        return "先选一种工作方式：监控当天（先审）、监控当天-自动，或审核历史日。"
    if is_watch_mode(ui["mode"]):
        extra = ""
        if ui.get("next_scan_at"):
            extra = f"下次自动扫 {ui['next_scan_at']}。"
        if ui.get("auto"):
            applied = int(ui.get("last_auto_applied") or 0)
            if ui.get("scanning"):
                return f"正在扫今天 {ui['today']}，扫到待审会自动写库。"
            return (
                f"自动模式：正在监控今天 {ui['today']}，每 {int(ui['interval_sec']) // 60} 分钟扫一次。"
                f"{extra} 上次自动写库 {applied} 辆。点「监控当天」可改回先审。"
            )
        if ui.get("scanning"):
            return f"正在扫今天 {ui['today']}，仍需你点批准才写库。"
        return (
            f"正在监控今天 {ui['today']}，每 {int(ui['interval_sec']) // 60} 分钟扫一次。"
            f"{extra} 待审 {pending_n}。点「监控当天-自动」可改为扫完就写库。"
        )
    if summary:
        return f"{day} 已扫完，待审 {pending_n}。不会循环监控今天。"
    return f"已切到审核历史日。选好日期后点「扫描该日」。当前看 {day or '未选日期'}。"


def overview(day: str) -> dict:
    ui = _ui_payload()
    if is_watch_mode(ui["mode"]):
        day = ui["today"]
    summary = load_scan_summary(day)
    pending = list_pending(day, kind=KIND_VOLUME if is_watch_mode(ui["mode"]) else None)
    processed = load_processed()
    done = sum(
        1
        for k in processed
        if k.endswith("|" + day) or k.endswith("|" + day + "|deduct")
    )
    pending_volume = {
        str(p.get("weight_bill_no") or ""): p
        for p in pending
        if (p.get("kind") or KIND_VOLUME) == KIND_VOLUME
    }
    vehicles = []
    for raw in summary.get("vehicles") or []:
        kind = raw.get("kind") or KIND_VOLUME
        if is_watch_mode(ui["mode"]) and kind == KIND_DEDUCT:
            continue
        card = pending_volume.get(str(raw.get("weight_bill_no") or "")) if kind == KIND_VOLUME else None
        v = merge_pending_ai(raw, card) if kind == KIND_VOLUME else dict(raw)
        if is_processed(v.get("weight_bill_no", ""), day, kind):
            v["status"] = "done"
        vehicles.append(relabel_vehicle_steels(v))
    return {
        "summary": {
            **summary,
            "processed": done,
            "skipped": summary.get("skipped", 0),
            "manual_bills": summary.get("manual_bills", 0),
        },
        "pending": [relabel_vehicle_steels(p) for p in pending],
        "vehicles": vehicles,
        "steel_names": {str(k): v for k, v in STEEL_NAMES.items()},
        "logs": recent_logs(30),
        "aliveText": _alive_text(),
        "notice": _notice(day, summary, len(pending)),
        "ui": ui,
    }


def _history_default_day() -> date:
    """Yesterday, or the last historical date the user already picked."""
    today = today_cn()
    saved = load_ui_mode().get("date") or ""
    if saved:
        try:
            day = parse_day(saved)
            if day < today:
                return day
        except ValueError:
            pass
    return today - timedelta(days=1)


def set_mode(mode: str, day_text: str = "") -> dict:
    today = today_cn()
    if is_watch_mode(mode):
        auto = mode == MODE_WATCH_AUTO
        save_ui_mode(mode, today.isoformat())
        CONTROLLER.set_auto(auto)
        CONTROLLER.start()
        if auto:
            CONTROLLER.flush_auto()
        return {"ok": True, "ui": _ui_payload(), "overview": overview(today.isoformat())}
    if mode == "review-history":
        CONTROLLER.stop()
        if day_text:
            day = parse_day(day_text)
            if day > today:
                raise ValueError("不能选未来日期")
        else:
            day = _history_default_day()
        save_ui_mode("review-history", day.isoformat())
        write_heartbeat({"mode": "dashboard", "last_scan_date": day.isoformat()})
        return {"ok": True, "ui": _ui_payload(), "overview": overview(day.isoformat())}
    if mode == "choose":
        CONTROLLER.stop()
        save_ui_mode("choose", "")
        write_heartbeat({"mode": "dashboard"})
        return {"ok": True, "ui": _ui_payload(), "overview": overview(today.isoformat())}
    raise ValueError(f"未知工作方式: {mode}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        logger.info("%s " + fmt, self.address_string(), *args)

    def _json(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            body = WEB.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/overview":
            qs = parse_qs(parsed.query)
            day = (qs.get("date") or [today_cn().isoformat()])[0]
            try:
                parse_day(day)
                self._json(200, overview(day))
            except Exception as exc:
                self._json(400, {"error": str(exc)})
            return
        if parsed.path == "/api/mode":
            self._json(200, {"ui": _ui_payload()})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            body = self._read_json()
            if parsed.path == "/api/mode":
                self._json(200, set_mode(str(body.get("mode") or ""), str(body.get("date") or "")))
                return
            if parsed.path == "/api/scan":
                if CONTROLLER.is_running():
                    snap = CONTROLLER.run_once_now()
                    if snap.get("error"):
                        raise RuntimeError(snap["error"])
                    day_s = today_cn().isoformat()
                    write_heartbeat({
                        "last_scan_date": day_s,
                        "mode": "watch-today-auto" if CONTROLLER.is_auto() else "watch-today",
                    })
                    self._json(200, {"ok": True, "ui": _ui_payload(), "overview": overview(day_s)})
                    return
                day = parse_day(str(body.get("date")))
                if day > today_cn():
                    raise ValueError("不能选未来日期")
                summary = scan_day(
                    day,
                    auto=False,
                    heartbeat_mode="once",
                    include_deduct=True,
                    max_manuals=3,
                )
                save_ui_mode("review-history", day.isoformat())
                write_heartbeat({"last_scan_date": day.isoformat(), "mode": "once"})
                self._json(200, {"ok": True, "summary": summary, "ui": _ui_payload()})
                return
            if parsed.path == "/api/approve":
                self._json(200, approve(str(body["ticket"]), str(body["date"])))
                return
            if parsed.path == "/api/approve-deduct":
                self._json(
                    200,
                    approve_deduct(
                        str(body["ticket"]),
                        str(body["date"]),
                        float(body["target_kg"]),
                    ),
                )
                return
            if parsed.path == "/api/refresh-deduct":
                self._json(200, refresh_deduct(str(body["ticket"]), str(body["date"])))
                return
            if parsed.path == "/api/reject":
                kind = str(body.get("kind") or KIND_VOLUME)
                self._json(200, reject(str(body["ticket"]), str(body["date"]), kind))
                return
            if parsed.path == "/api/restore":
                kind = str(body.get("kind") or KIND_VOLUME)
                if kind == KIND_DEDUCT:
                    self._json(200, restore_deduct(ticket=str(body["ticket"]), day=str(body["date"])))
                else:
                    self._json(200, restore(ticket=str(body["ticket"]), day=str(body["date"])))
                return
            self._json(404, {"error": "not found"})
        except Exception as exc:
            logger.exception("api error")
            self._json(400, {"error": format_db_error(exc)})


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(description="本机审核看板")
    parser.add_argument("--no-mouse", action="store_true", help="不启动 VPN 鼠标保活")
    parser.add_argument("--no-browser", action="store_true", help="启动时不打开浏览器")
    args = parser.parse_args()
    host, port = dashboard_bind()
    if host not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("看板只允许绑定 127.0.0.1")
    if not args.no_mouse:
        start_in_background()
    # 进程重启后不自动开当天监控，必须在面板里再点一次
    saved = load_ui_mode()
    if is_watch_mode(str(saved.get("mode") or "")):
        save_ui_mode("choose", saved.get("date") or "")
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    logger.info("看板 %s", url)
    write_heartbeat({"mode": "dashboard"})
    if not args.no_browser:
        webbrowser.open(url)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
