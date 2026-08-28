"""Move the mouse only after it has been idle, so the office VPN does not drop.

Does not click or type. macOS may ask for Accessibility permission once.
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import math
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monitor.config import (
    MOUSE_IDLE_SEC,
    MOUSE_RADIUS_PX,
    ROOT as MONITOR_ROOT,
    TZ,
    ensure_dirs,
)
from monitor.store import append_log

logger = logging.getLogger(__name__)

_thread: threading.Thread | None = None
_stop = threading.Event()


def _status_path() -> Path:
    ensure_dirs()
    return MONITOR_ROOT / "state" / "vpn_keepalive.json"


def write_mouse_status(payload: dict) -> None:
    import json

    path = _status_path()
    data = {"ts": datetime.now(TZ).isoformat(timespec="seconds"), **payload}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_mouse_status() -> dict:
    import json

    path = _status_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def shape_points(name: str, cx: float, cy: float, radius: float) -> list[tuple[float, float]]:
    """Closed-ish path, about `radius` pixels out from the current point."""
    n = 48
    if name == "circle":
        return [
            (cx + radius * math.cos(2 * math.pi * i / n), cy + radius * math.sin(2 * math.pi * i / n))
            for i in range(n + 1)
        ]
    if name == "square":
        corners = [
            (cx - radius, cy - radius),
            (cx + radius, cy - radius),
            (cx + radius, cy + radius),
            (cx - radius, cy + radius),
            (cx - radius, cy - radius),
        ]
        return _interp_polyline(corners, 12)
    if name == "triangle":
        corners = [
            (cx, cy - radius),
            (cx + radius * 0.95, cy + radius * 0.75),
            (cx - radius * 0.95, cy + radius * 0.75),
            (cx, cy - radius),
        ]
        return _interp_polyline(corners, 14)
    if name == "diamond":
        corners = [
            (cx, cy - radius),
            (cx + radius, cy),
            (cx, cy + radius),
            (cx - radius, cy),
            (cx, cy - radius),
        ]
        return _interp_polyline(corners, 12)
    if name == "figure8":
        pts: list[tuple[float, float]] = []
        for i in range(n + 1):
            t = 2 * math.pi * i / n
            pts.append((cx + radius * math.sin(t), cy + radius * math.sin(t) * math.cos(t)))
        return pts
    # zigzag
    pts = []
    steps = 10
    for i in range(steps + 1):
        x = cx - radius + 2 * radius * i / steps
        y = cy + (radius * 0.7 if i % 2 else -radius * 0.7)
        pts.append((x, y))
    return pts


def _interp_polyline(corners: list[tuple[float, float]], per_edge: int) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for i in range(len(corners) - 1):
        x0, y0 = corners[i]
        x1, y1 = corners[i + 1]
        for j in range(per_edge):
            t = j / per_edge
            out.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    out.append(corners[-1])
    return out


SHAPE_CYCLE = ("circle", "square", "triangle", "diamond", "figure8", "zigzag")


class _WinMouse:
    def position(self) -> tuple[float, float]:
        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        pt = POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            raise RuntimeError("无法读取鼠标位置")
        return float(pt.x), float(pt.y)

    def move(self, x: float, y: float) -> None:
        user32 = ctypes.windll.user32
        wide = max(int(user32.GetSystemMetrics(0)), 200)
        high = max(int(user32.GetSystemMetrics(1)), 200)
        x = min(wide - 8, max(8, x))
        y = min(high - 8, max(8, y))
        if not user32.SetCursorPos(int(x), int(y)):
            raise RuntimeError("无法移动鼠标")


class _MacMouse:
    def __init__(self) -> None:
        self._cg = ctypes.CDLL(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        )

        class CGPoint(ctypes.Structure):
            _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

        self._CGPoint = CGPoint
        cg = self._cg
        cg.CGEventCreate.restype = ctypes.c_void_p
        cg.CGEventCreate.argtypes = [ctypes.c_void_p]
        cg.CGEventGetLocation.restype = CGPoint
        cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
        cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        cg.CGEventCreateMouseEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            CGPoint,
            ctypes.c_uint32,
        ]
        cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        cg.CFRelease.argtypes = [ctypes.c_void_p]
        cg.CGMainDisplayID.restype = ctypes.c_uint32
        cg.CGDisplayPixelsWide.argtypes = [ctypes.c_uint32]
        cg.CGDisplayPixelsWide.restype = ctypes.c_size_t
        cg.CGDisplayPixelsHigh.argtypes = [ctypes.c_uint32]
        cg.CGDisplayPixelsHigh.restype = ctypes.c_size_t

    def position(self) -> tuple[float, float]:
        ev = self._cg.CGEventCreate(None)
        if not ev:
            raise RuntimeError("无法读取鼠标位置")
        try:
            pt = self._cg.CGEventGetLocation(ev)
            return float(pt.x), float(pt.y)
        finally:
            self._cg.CFRelease(ev)

    def move(self, x: float, y: float) -> None:
        display = self._cg.CGMainDisplayID()
        wide = max(int(self._cg.CGDisplayPixelsWide(display)), 200)
        high = max(int(self._cg.CGDisplayPixelsHigh(display)), 200)
        x = min(wide - 8, max(8, x))
        y = min(high - 8, max(8, y))
        pt = self._CGPoint(x, y)
        ev = self._cg.CGEventCreateMouseEvent(None, 5, pt, 0)
        if not ev:
            raise RuntimeError("无法移动鼠标：系统设置 → 隐私与安全性 → 辅助功能，勾选终端/Python")
        try:
            self._cg.CGEventPost(0, ev)
        finally:
            self._cg.CFRelease(ev)


def default_mouse() -> Any:
    if sys.platform == "win32":
        return _WinMouse()
    if sys.platform == "darwin":
        return _MacMouse()
    raise RuntimeError("当前系统不支持 VPN 鼠标保活，请用 --no-mouse 启动")


def _draw_shape(mouse: Any, name: str, radius: float) -> str:
    x0, y0 = mouse.position()
    pts = shape_points(name, x0, y0, radius)
    for x, y in pts:
        mouse.move(x, y)
        time.sleep(0.012)
    mouse.move(x0, y0)
    return name


def run_loop(
    *,
    idle_sec: float = MOUSE_IDLE_SEC,
    radius: float = MOUSE_RADIUS_PX,
    stop: threading.Event | None = None,
    mouse_factory: Callable[[], Any] | None = None,
) -> None:
    stop = stop or _stop
    mouse = (mouse_factory or default_mouse)()
    last_pos = mouse.position()
    idle_from = time.monotonic()
    shape_i = 0
    write_mouse_status({"running": True, "last_shape": None, "note": "等待鼠标停住"})
    logger.info("VPN 鼠标保活已开：停住 %.0f 秒后画一次大轨迹", idle_sec)
    while not stop.is_set():
        time.sleep(0.4)
        try:
            pos = mouse.position()
        except Exception as exc:
            write_mouse_status({"running": False, "error": str(exc)})
            logger.warning("读鼠标失败: %s", exc)
            stop.wait(5)
            continue
        moved = abs(pos[0] - last_pos[0]) + abs(pos[1] - last_pos[1])
        if moved > 3:
            last_pos = pos
            idle_from = time.monotonic()
            continue
        if time.monotonic() - idle_from < idle_sec:
            continue
        name = SHAPE_CYCLE[shape_i % len(SHAPE_CYCLE)]
        shape_i += 1
        try:
            _draw_shape(mouse, name, radius)
            last_pos = mouse.position()
            idle_from = time.monotonic()
            write_mouse_status({"running": True, "last_shape": name})
            logger.info("画了 %s，防止 VPN 空闲掉线", name)
        except Exception as exc:
            write_mouse_status({"running": False, "error": str(exc)})
            logger.warning("移动鼠标失败: %s", exc)
            append_log("mouse_keepalive_error", {"error": str(exc)})
            stop.wait(10)


def start_in_background() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()

    def _safe_run() -> None:
        try:
            run_loop()
        except Exception as exc:
            write_mouse_status({"running": False, "error": str(exc)})
            logger.warning("VPN 鼠标保活未启动: %s", exc)

    _thread = threading.Thread(target=_safe_run, name="vpn-mouse-keepalive", daemon=True)
    _thread.start()


def stop_background() -> None:
    _stop.set()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(description="鼠标停住后每隔约30秒画轨迹，防止 VPN 掉线")
    parser.add_argument("--idle", type=float, default=MOUSE_IDLE_SEC)
    parser.add_argument("--radius", type=float, default=MOUSE_RADIUS_PX)
    args = parser.parse_args()
    try:
        run_loop(idle_sec=args.idle, radius=args.radius)
    except KeyboardInterrupt:
        write_mouse_status({"running": False, "note": "已停止"})


if __name__ == "__main__":
    main()
