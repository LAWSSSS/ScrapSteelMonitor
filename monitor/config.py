"""Paths and environment. Password never has a code default."""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
TZ = ZoneInfo("Asia/Shanghai")

ALLOWED_MAIN_TYPES = (1, 2)
# 与业务枚举一致：zhongfei1/2=1/2，medium=11。
STEEL_NAMES = {
    1: "重废1",
    2: "重废2",
    3: "重废3",
    4: "剪料1",
    5: "剪料2",
    6: "剪料3",
    7: "剪料4",
    8: "破碎料1",
    9: "破碎料2",
    10: "破碎料3",
    11: "中废",
    12: "生铁",
    13: "厚剪",
    14: "钢筋切粒",
    15: "汽车壳",
    16: "超标",
}
BAND_PP = 10.0
KIND_VOLUME = "volume"
KIND_DEDUCT = "deduct"
DEDUCT_BAND_KG = 150.0
# 非混料页面分区间加幅：业务日 >= 这天用新规则，更早用旧规则。
DISPLAY_RULE_SWITCH = date(2026, 7, 8)
HUNLIAO_MIX_THRESHOLD = 0.55
# 改成混料时只刚过 55%。若抓斗太少、刚过线也会 >=80%，整车跳过，不翻成 80%/100%。
HUNLIAO_AFTER_CAP = 0.80
WATCH_INTERVAL_SEC = 300
HEARTBEAT_STALE_SEC = 600
DUMP_TABLES = ("intelli_task_check", "intelli_task_info")
MOUSE_IDLE_SEC = 30.0
MOUSE_RADIUS_PX = 180.0
MOUSE_SHIFT_MIN_PX = 100
MOUSE_SHIFT_MAX_PX = 1000
MOUSE_CLICK_PROB_DEFAULT = 0.4


def _load_dotenv() -> None:
    for path in (ROOT / ".env", REPO_ROOT / ".env"):
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            os.environ.setdefault(key, value)


_load_dotenv()


def env_file() -> Path:
    """Machine-local settings. Prefer monitor/.env so Windows/Mac 起点可以不同。"""
    monitor_env = ROOT / ".env"
    if monitor_env.is_file():
        return monitor_env
    root_env = REPO_ROOT / ".env"
    return root_env if root_env.is_file() else monitor_env


def upsert_env(key: str, value: str, path: Path | None = None) -> Path:
    """Create or replace one KEY=value in monitor/.env. Does not print secrets."""
    path = path or env_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    prefix = f"{key}="
    replaced = False
    out: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith(prefix) or stripped.startswith(f"{key} ="):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(raw)
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.environ[key] = value
    return path


def mouse_origin() -> tuple[float, float] | None:
    raw_x = os.environ.get("MOUSE_ORIGIN_X", "").strip()
    raw_y = os.environ.get("MOUSE_ORIGIN_Y", "").strip()
    if not raw_x or not raw_y:
        return None
    return float(raw_x), float(raw_y)


def mouse_click_prob() -> float:
    raw = os.environ.get("MOUSE_CLICK_PROB", str(MOUSE_CLICK_PROB_DEFAULT)).strip()
    return min(1.0, max(0.0, float(raw)))


def mysql_settings() -> dict[str, object]:
    password = os.environ.get("MYSQL_PASSWORD", "")
    if not password:
        raise RuntimeError(
            "未设置 MYSQL_PASSWORD。复制 monitor/.env.example 为 monitor/.env 后填入密码。"
        )
    return {
        "host": os.environ.get("MYSQL_HOST", "172.16.16.101"),
        "port": int(os.environ.get("MYSQL_PORT", "33306")),
        "user": os.environ.get("MYSQL_USER", "root"),
        "password": password,
        "database": os.environ.get("MYSQL_DB", "slzj"),
        "charset": "utf8mb4",
    }


def dashboard_bind() -> tuple[str, int]:
    return (
        os.environ.get("DASHBOARD_HOST", "127.0.0.1"),
        int(os.environ.get("DASHBOARD_PORT", "8765")),
    )


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min)
    return start, start + timedelta(days=1)


def today_cn() -> date:
    return datetime.now(TZ).date()


def parse_day(text: str) -> date:
    return date.fromisoformat(text.strip())


def ensure_dirs() -> None:
    for name in ("state", "pending", "backups", "db_dumps", "logs"):
        (ROOT / name).mkdir(parents=True, exist_ok=True)


def steel_label(steel_type: int) -> str:
    return STEEL_NAMES.get(int(steel_type), f"料型{steel_type}")


def relabel_vehicle_steels(row: dict) -> dict:
    """Fill Chinese steel names on a scan/pending row. Does not write disk."""
    out = dict(row)
    for key in ("human1", "human2", "human3"):
        human = out.get(key)
        if isinstance(human, dict) and human.get("steel_type") is not None:
            out[key] = {**human, "label": steel_label(int(human["steel_type"]))}
    steel_type = out.get("main_type")
    if steel_type is None:
        first = out.get("human1")
        if isinstance(first, dict) and first.get("steel_type") is not None:
            steel_type = first["steel_type"]
    if steel_type is None:
        return out
    label = steel_label(int(steel_type))
    out["main_label"] = label
    reason = str(out.get("reason") or "")
    if "主料型不是重废1/2" in reason:
        out["reason"] = f"主料型不是重废1/2（{label}）"
    return out
