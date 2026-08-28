"""Reject any statement that is not SELECT / UPDATE / INSERT / txn control."""

from __future__ import annotations

import re

_FORBIDDEN = re.compile(
    r"\b(DELETE|DROP|TRUNCATE|ALTER|REPLACE|GRANT|REVOKE|CREATE|RENAME|LOAD\s+DATA)\b",
    re.IGNORECASE,
)
_ALLOWED = re.compile(
    r"^\s*(SELECT|UPDATE|INSERT|START\s+TRANSACTION|BEGIN|COMMIT|ROLLBACK|SET)\b",
    re.IGNORECASE,
)


class SqlGuardError(ValueError):
    pass


def assert_safe_sql(sql: str) -> None:
    text = sql.strip()
    if not text:
        raise SqlGuardError("空 SQL")
    if _FORBIDDEN.search(text):
        raise SqlGuardError(f"拒绝危险语句: {text[:120]}")
    if not _ALLOWED.match(text):
        raise SqlGuardError(f"只允许 SELECT/UPDATE/INSERT: {text[:120]}")
