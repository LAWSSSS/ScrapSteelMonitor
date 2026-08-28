from __future__ import annotations

import sys

if len(sys.argv) > 1 and sys.argv[1] == "dashboard":
    sys.argv = [sys.argv[0], *sys.argv[2:]]
    from monitor.dashboard import main
else:
    from monitor.agent import main

main()
