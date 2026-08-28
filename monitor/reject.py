from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monitor.apply import reject


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="拒绝待审单，不写库")
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--date", required=True)
    args = parser.parse_args()
    print(json.dumps(reject(args.ticket, args.date), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
