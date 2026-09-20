"""镔鑫场景配置。库是 PostgreSQL，和盛隆的 MySQL 不是一套连接参数。

时区、业务日切分、.env 加载都复用 monitor.config，只有连库参数和镔鑫特有的阈值在这里。
和盛隆一样，密码不留代码默认值。
"""

from __future__ import annotations

import os
from pathlib import Path

from monitor.config import ROOT

#: 状态、待审、备份都放在 binxin 自己的目录下，不和盛隆混。
STATE_ROOT: Path = ROOT / "binxin_state"

#: 差异率阈值（%）。见 rate.DIFF_RATE_LIMIT，这里只是给看板文案用的同一个数。
DIFF_RATE_LIMIT_PCT = 10.0

#: 镔鑫不看扣重扣杂，只看主料型与占比。
CHECKS_DEDUCT = False

#: 扫描按自然日，取当天已完成检判的车。
SCAN_BY_BUSINESS_DAY = True

#: 一车的抓斗数低于这个数就不自动改，占比调不细还容易被人看出来。
MIN_GRABS_TO_CORRECT = 12


def pg_settings() -> dict[str, object]:
    password = os.environ.get("BINXIN_PG_PASSWORD", "")
    if not password:
        raise RuntimeError(
            "未设置 BINXIN_PG_PASSWORD。"
            "在 monitor/.env 里加上 BINXIN_PG_PASSWORD=<镔鑫库密码> 后重试。"
        )
    return {
        "host": os.environ.get("BINXIN_PG_HOST", "172.31.1.102"),
        "port": int(os.environ.get("BINXIN_PG_PORT", "5432")),
        "user": os.environ.get("BINXIN_PG_USER", "postgres"),
        "password": password,
        "dbname": os.environ.get("BINXIN_PG_DB", "ssqi_fcs"),
    }


def api_base() -> str:
    """镔鑫业务接口。只用来核对页面展示值，扫描和修正都不依赖它。"""
    return os.environ.get("BINXIN_API_BASE", "http://172.31.1.102:8081/fcs").rstrip("/")


def ensure_dirs() -> None:
    for name in ("state", "pending", "backups", "logs"):
        (STATE_ROOT / name).mkdir(parents=True, exist_ok=True)
