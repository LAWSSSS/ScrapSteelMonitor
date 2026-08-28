"""Periodic local dump of tables we may update. Never uses DELETE."""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from monitor.config import DUMP_TABLES, ROOT, TZ, ensure_dirs, mysql_settings
from monitor.store import append_log

logger = logging.getLogger(__name__)


def dump_tables() -> Path:
    ensure_dirs()
    stamp = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
    out = ROOT / "db_dumps" / f"slzj_{stamp}.sql"
    settings = mysql_settings()
    mysqldump = shutil.which("mysqldump")
    if mysqldump:
        cmd = [
            mysqldump,
            "-h",
            str(settings["host"]),
            "-P",
            str(settings["port"]),
            "-u",
            str(settings["user"]),
            f"-p{settings['password']}",
            "--single-transaction",
            "--quick",
            "--no-tablespaces",
            str(settings["database"]),
            *DUMP_TABLES,
        ]
        result = subprocess.run(cmd, check=False, capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[:400])
        out.write_bytes(result.stdout)
    else:
        _python_dump_values(out, settings)
    append_log("dump", {"path": str(out)})
    return out


def _python_dump_values(out: Path, settings: dict) -> None:
    import pymysql
    from pymysql.cursors import SSCursor

    conn = pymysql.connect(
        host=str(settings["host"]),
        port=int(settings["port"]),
        user=str(settings["user"]),
        password=str(settings["password"]),
        database=str(settings["database"]),
        charset="utf8mb4",
        cursorclass=SSCursor,
        connect_timeout=15,
        read_timeout=300,
    )
    try:
        with out.open("w", encoding="utf-8") as fh, conn.cursor() as cur:
            fh.write("SET NAMES utf8mb4;\n")
            for table in DUMP_TABLES:
                cur.execute(f"SELECT * FROM `{table}`")
                cols = [d[0] for d in cur.description]
                fh.write(f"-- {table}\n")
                batch: list[str] = []
                for row in cur:
                    vals = ",".join(_sql_literal(v) for v in row)
                    batch.append(f"({vals})")
                    if len(batch) >= 200:
                        fh.write(
                            f"INSERT INTO `{table}` ({','.join('`'+c+'`' for c in cols)}) "
                            f"VALUES {','.join(batch)};\n"
                        )
                        batch = []
                if batch:
                    fh.write(
                        f"INSERT INTO `{table}` ({','.join('`'+c+'`' for c in cols)}) "
                        f"VALUES {','.join(batch)};\n"
                    )
    finally:
        conn.close()


def _sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{text}'"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    argparse.ArgumentParser(description="导出相关表到 monitor/db_dumps").parse_args()
    path = dump_tables()
    print(path)


if __name__ == "__main__":
    main()
