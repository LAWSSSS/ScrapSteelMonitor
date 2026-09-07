"""Idle mouse sweep + optional left-click so the office VPN does not drop.

Moves from a configured origin, horizontally right 100–1000px, maybe clicks,
then returns. Does not wander the screen. macOS needs Accessibility once.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import random
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monitor.config import (
    MOUSE_CLICK_PROB_DEFAULT,
    MOUSE_IDLE_SEC,
    MOUSE_SHIFT_MAX_PX,
    MOUSE_SHIFT_MIN_PX,
    TZ,
    ensure_dirs,
    mouse_click_prob,
    mouse_origin,
    upsert_env,
)
from monitor.store import append_log

logger = logging.getLogger(__name__)

_thread: threading.Thread | None = None
_stop = threading.Event()


def _status_path() -> Path:
    ensure_dirs()
    return Path(__file__).resolve().parent / "state" / "vpn_keepalive.json"


def write_mouse_status(payload: dict) -> None:
    path = _status_path()
    data = {"ts": datetime.now(TZ).isoformat(timespec="seconds"), **payload}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_mouse_status() -> dict:
    path = _status_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


@dataclass(frozen=True)
class SweepPlan:
    origin: tuple[float, float]
    target: tuple[float, float]
    distance: float
    will_click: bool
    ok: bool = True
    reason: str = ""


def plan_sweep(
    origin: tuple[float, float],
    *,
    screen: tuple[int, int],
    rng: random.Random,
    click_prob: float,
    shift_min: float = MOUSE_SHIFT_MIN_PX,
    shift_max: float = MOUSE_SHIFT_MAX_PX,
    allow_click: bool = True,
) -> SweepPlan:
    """Stay on one horizontal line. Never go left of origin or off-screen."""
    x0, y0 = float(origin[0]), float(origin[1])
    wide, high = int(screen[0]), int(screen[1])
    y0 = min(high - 8.0, max(8.0, y0))
    x0 = min(wide - 8.0, max(8.0, x0))
    room = wide - 8.0 - x0
    if room < shift_min:
        return SweepPlan(
            origin=(x0, y0),
            target=(x0, y0),
            distance=0.0,
            will_click=False,
            ok=False,
            reason=f"起点太靠右，右边空不到 {shift_min:.0f}px，请往左重设起点",
        )
    hi = min(float(shift_max), room)
    lo = min(float(shift_min), hi)
    distance = float(rng.randint(int(lo), int(hi)))
    target = (x0 + distance, y0)
    will_click = bool(allow_click and rng.random() < max(0.0, min(1.0, click_prob)))
    return SweepPlan(
        origin=(x0, y0),
        target=target,
        distance=distance,
        will_click=will_click,
    )


def _line_path(
    start: tuple[float, float],
    end: tuple[float, float],
    steps: int,
) -> list[tuple[float, float]]:
    steps = max(2, int(steps))
    x0, y0 = start
    x1, y1 = end
    return [
        (x0 + (x1 - x0) * (i / steps), y0 + (y1 - y0) * (i / steps))
        for i in range(steps + 1)
    ]


def outbound_path(plan: SweepPlan, steps: int = 24) -> list[tuple[float, float]]:
    if not plan.ok:
        return []
    return _line_path(plan.origin, plan.target, steps)


def inbound_path(plan: SweepPlan, steps: int = 24) -> list[tuple[float, float]]:
    if not plan.ok:
        return []
    return _line_path(plan.target, plan.origin, steps)


def sweep_path(plan: SweepPlan, steps: int = 24) -> list[tuple[float, float]]:
    """Origin → right → origin. Click happens at the right end, between the two legs."""
    out = outbound_path(plan, steps)
    back = inbound_path(plan, steps)
    if not out or not back:
        return []
    return out + back[1:]


def _enable_win_dpi() -> None:
    """Use physical pixels so 2160x1440 @ 125% matches what the user sees."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class _WinMouse:
    def __init__(self) -> None:
        _enable_win_dpi()

    def position(self) -> tuple[float, float]:
        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        pt = POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            raise RuntimeError("无法读取鼠标位置")
        return float(pt.x), float(pt.y)

    def screen_size(self) -> tuple[int, int]:
        user32 = ctypes.windll.user32
        return max(int(user32.GetSystemMetrics(0)), 200), max(int(user32.GetSystemMetrics(1)), 200)

    def move(self, x: float, y: float) -> None:
        user32 = ctypes.windll.user32
        wide, high = self.screen_size()
        x = min(wide - 8, max(8, x))
        y = min(high - 8, max(8, y))
        if not user32.SetCursorPos(int(round(x)), int(round(y))):
            raise RuntimeError("无法移动鼠标")

    def click(self) -> None:
        user32 = ctypes.windll.user32
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.04)
        user32.mouse_event(0x0004, 0, 0, 0, 0)


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

    def screen_size(self) -> tuple[int, int]:
        display = self._cg.CGMainDisplayID()
        wide = max(int(self._cg.CGDisplayPixelsWide(display)), 200)
        high = max(int(self._cg.CGDisplayPixelsHigh(display)), 200)
        return wide, high

    def _post_mouse(self, event_type: int, x: float, y: float, button: int) -> None:
        wide, high = self.screen_size()
        x = min(wide - 8, max(8, x))
        y = min(high - 8, max(8, y))
        pt = self._CGPoint(x, y)
        ev = self._cg.CGEventCreateMouseEvent(None, event_type, pt, button)
        if not ev:
            raise RuntimeError("无法控制鼠标：系统设置 → 隐私与安全性 → 辅助功能，勾选终端/Python")
        try:
            self._cg.CGEventPost(0, ev)
        finally:
            self._cg.CFRelease(ev)

    def move(self, x: float, y: float) -> None:
        self._post_mouse(5, x, y, 0)

    def click(self) -> None:
        x, y = self.position()
        self._post_mouse(1, x, y, 0)
        time.sleep(0.04)
        self._post_mouse(2, x, y, 0)


def default_mouse() -> Any:
    if sys.platform == "win32":
        return _WinMouse()
    if sys.platform == "darwin":
        return _MacMouse()
    raise RuntimeError("当前系统不支持 VPN 鼠标保活，请用 --no-mouse 启动")


def run_sweep(mouse: Any, plan: SweepPlan, *, sleeper: Callable[[float], None] = time.sleep) -> None:
    if not plan.ok:
        raise RuntimeError(plan.reason or "保活轨迹无效")
    for x, y in outbound_path(plan):
        mouse.move(x, y)
        sleeper(0.012)
    if plan.will_click:
        mouse.click()
        sleeper(0.05)
    for x, y in inbound_path(plan):
        mouse.move(x, y)
        sleeper(0.012)


def save_origin(mouse: Any | None = None) -> tuple[float, float, Path]:
    mouse = mouse or default_mouse()
    x, y = mouse.position()
    path = upsert_env("MOUSE_ORIGIN_X", f"{x:.0f}")
    upsert_env("MOUSE_ORIGIN_Y", f"{y:.0f}")
    write_mouse_status(
        {
            "running": False,
            "note": f"已保存起点 ({x:.0f},{y:.0f})",
            "origin": [round(x), round(y)],
        }
    )
    return x, y, path


def _format_action(plan: SweepPlan) -> str:
    click_txt = "已点击" if plan.will_click else "未点击"
    return f"右移{plan.distance:.0f}px {click_txt}"


def run_loop(
    *,
    idle_sec: float = MOUSE_IDLE_SEC,
    stop: threading.Event | None = None,
    mouse_factory: Callable[[], Any] | None = None,
    rng: random.Random | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> None:
    stop = stop or _stop
    sleep = sleeper or time.sleep
    mouse = (mouse_factory or default_mouse)()
    dice = rng or random.Random()
    last_pos = mouse.position()
    idle_from = time.monotonic()
    origin = mouse_origin()
    if origin is None:
        write_mouse_status(
            {
                "running": True,
                "last_action": None,
                "note": "未设起点，只平移不点击。把鼠标放到安全空白处后运行 --save-origin",
            }
        )
        logger.warning("未设置 MOUSE_ORIGIN_X/Y：保活只平移，不点击")
    else:
        write_mouse_status(
            {
                "running": True,
                "last_action": None,
                "origin": [round(origin[0]), round(origin[1])],
                "note": "等待鼠标停住",
            }
        )
    logger.info("VPN 鼠标保活已开：停住 %.0f 秒后从起点水平右移", idle_sec)
    while not stop.is_set():
        sleep(0.4)
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
        origin = mouse_origin()
        allow_click = origin is not None
        start = origin if origin is not None else pos
        try:
            plan = plan_sweep(
                start,
                screen=mouse.screen_size(),
                rng=dice,
                click_prob=mouse_click_prob() if allow_click else 0.0,
                allow_click=allow_click,
            )
            if not plan.ok:
                write_mouse_status({"running": True, "error": plan.reason, "last_action": None})
                logger.warning("%s", plan.reason)
                idle_from = time.monotonic()
                continue
            run_sweep(mouse, plan, sleeper=sleep)
            last_pos = mouse.position()
            idle_from = time.monotonic()
            action = _format_action(plan)
            write_mouse_status(
                {
                    "running": True,
                    "last_action": action,
                    "clicked": plan.will_click,
                    "origin": [round(plan.origin[0]), round(plan.origin[1])],
                    "note": None if allow_click else "未设起点，只平移不点击",
                }
            )
            logger.info("保活：%s", action)
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
    parser = argparse.ArgumentParser(description="鼠标停住后从指定起点水平右移，概率左键，防止 VPN 掉线")
    parser.add_argument("--idle", type=float, default=MOUSE_IDLE_SEC)
    parser.add_argument(
        "--save-origin",
        action="store_true",
        help="把当前鼠标位置写入 .env 作为保活起点，然后退出",
    )
    parser.add_argument(
        "--click-prob",
        type=float,
        default=None,
        help=f"每次右移后左键概率，0~1，默认 {MOUSE_CLICK_PROB_DEFAULT}",
    )
    args = parser.parse_args()
    if args.save_origin:
        x, y, path = save_origin()
        print(f"已保存起点 X={x:.0f} Y={y:.0f} → {path}")
        print("请选桌面空白处。Windows 2160x1440 @125% 用本命令抓坐标，不要手算缩放。")
        print("改完后重启看板。")
        return
    if args.click_prob is not None:
        upsert_env("MOUSE_CLICK_PROB", str(max(0.0, min(1.0, args.click_prob))))
    try:
        run_loop(idle_sec=args.idle)
    except KeyboardInterrupt:
        write_mouse_status({"running": False, "note": "已停止"})


if __name__ == "__main__":
    main()
