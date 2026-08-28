from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from unittest.mock import patch

from monitor.vpn_keepalive import SHAPE_CYCLE, _MacMouse, _WinMouse, default_mouse, shape_points


class ShapeTests(unittest.TestCase):
    def test_all_shapes_have_travel(self) -> None:
        for name in SHAPE_CYCLE:
            pts = shape_points(name, 400.0, 300.0, 180.0)
            self.assertGreaterEqual(len(pts), 8, name)
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            self.assertGreater(max(xs) - min(xs), 80, name)
            self.assertGreater(max(ys) - min(ys), 40, name)

    def test_default_mouse_darwin(self) -> None:
        with patch("monitor.vpn_keepalive.sys.platform", "darwin"):
            self.assertIsInstance(default_mouse(), _MacMouse)

    def test_default_mouse_windows(self) -> None:
        with patch("monitor.vpn_keepalive.sys.platform", "win32"):
            self.assertIsInstance(default_mouse(), _WinMouse)


if __name__ == "__main__":
    unittest.main()
