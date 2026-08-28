"""Optional desktop notification. Failure is ignored."""

from __future__ import annotations

import logging
import subprocess
import sys

logger = logging.getLogger(__name__)


def notify(title: str, body: str) -> None:
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{_esc(body)}" with title "{_esc(title)}"',
            ],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("notify skipped: %s", exc)


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')
