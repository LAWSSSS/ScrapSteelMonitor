"""镔鑫审核看板。只绑 127.0.0.1。

只有 /api/approve 一条路径会写库，而且还要先过写库封锁闸。扫描、刷新、退回都不碰库里的
检判结果（退回只删本机待审文件）。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monitor.binxin import guard
from monitor.binxin.apply import approve, reject
from monitor.binxin.config import DIFF_RATE_LIMIT_PCT
from monitor.binxin.db import format_db_error
from monitor.binxin.scan import queue_day
from monitor.binxin.store import list_pending, load_processed, load_scan_summary, recent_logs
from monitor.config import parse_day, today_cn

logger = logging.getLogger(__name__)
WEB = Path(__file__).resolve().parent / "web" / "index.html"

#: 看板固定本机。绑到别的地址等于把写库入口暴露到网上。
HOST = "127.0.0.1"
PORT = 8788


def overview(day: str) -> dict:
    """看板一次要的全部数据。纯读本地文件，不连库。"""
    summary = load_scan_summary(day)
    pending = {str(c["flow_code"]): c for c in list_pending(day)}
    processed = load_processed()
    done = {
        str(k).split("|", 1)[0]: v
        for k, v in processed.items()
        if str(k).endswith("|" + day)
    }
    lock = guard.status()
    vehicles = summary.get("vehicles") or []
    if summary:
        notice = (
            f"{day} 扫过 {summary.get('total', 0)} 车，待审 {len(pending)}，"
            f"已落库 {len(done)}。差异率超 {DIFF_RATE_LIMIT_PCT:.0f}% 的才进待审。"
        )
    else:
        notice = f"{day} 还没扫过。点「扫描该日」。扫描只读库。"
    return {
        "date": day,
        "total": summary.get("total", 0),
        "counts": summary.get("counts") or {},
        "vehicles": vehicles,
        "pending": pending,
        "processed": done,
        "lock": lock,
        "logs": recent_logs(25),
        "notice": notice,
    }


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

    def _body(self) -> dict:
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
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            body = self._body()
            if parsed.path == "/api/scan":
                day = parse_day(str(body.get("date") or ""))
                if day > today_cn():
                    raise ValueError("不能选未来日期")
                summary = queue_day(day)
                self._json(200, {"ok": True, "summary": summary})
                return
            if parsed.path == "/api/lock":
                action = str(body.get("action") or "")
                if action == "arm":
                    if not guard.env_ok():
                        raise RuntimeError(
                            f"没解锁：启动看板前要先 export {guard.WRITE_ENV}={guard.WRITE_TOKEN}"
                        )
                    guard.arm(int(body.get("minutes") or guard.DEFAULT_ARM_MINUTES), who="看板")
                elif action == "disarm":
                    guard.disarm()
                else:
                    raise ValueError(f"未知操作 {action}")
                self._json(200, {"ok": True, "lock": guard.status()})
                return
            if parsed.path == "/api/approve":
                self._json(200, approve(str(body["flow"]), str(body["date"])))
                return
            if parsed.path == "/api/reject":
                self._json(200, reject(str(body["flow"]), str(body["date"])))
                return
            self._json(404, {"error": "not found"})
        except Exception as exc:
            logger.exception("接口出错")
            self._json(400, {"error": format_db_error(exc)})


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(description="镔鑫审核看板")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    # 进程起来时先锁死，避免上次残留的解锁文件让看板一开就能写库。
    guard.disarm()
    httpd = ThreadingHTTPServer((HOST, args.port), Handler)
    url = f"http://{HOST}:{args.port}/"
    logger.info("镔鑫看板 %s（写库当前锁死）", url)
    if not args.no_browser:
        webbrowser.open(url)
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
