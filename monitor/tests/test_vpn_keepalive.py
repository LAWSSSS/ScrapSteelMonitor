from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from unittest.mock import patch

from monitor.vpn_keepalive import (
    SweepPlan,
    _MacMouse,
    _WinMouse,
    default_mouse,
    inbound_path,
    outbound_path,
    plan_sweep,
    run_sweep,
    sweep_path,
)


class FakeMouse:
    def __init__(self) -> None:
        self.pos = (80.0, 400.0)
        self.moves: list[tuple[float, float]] = []
        self.clicks = 0

    def position(self) -> tuple[float, float]:
        return self.pos

    def screen_size(self) -> tuple[int, int]:
        return 2160, 1440

    def move(self, x: float, y: float) -> None:
        self.pos = (float(x), float(y))
        self.moves.append(self.pos)

    def click(self) -> None:
        self.clicks += 1


class SweepPlanTests(unittest.TestCase):
    def test_only_moves_right_on_same_y(self) -> None:
        rng = random.Random(1)
        plan = plan_sweep((200.0, 1300.0), screen=(2160, 1440), rng=rng, click_prob=0.0)
        self.assertTrue(plan.ok)
        self.assertEqual(plan.origin[1], plan.target[1])
        self.assertGreaterEqual(plan.distance, 100)
        self.assertLessEqual(plan.distance, 1000)
        self.assertAlmostEqual(plan.target[0], plan.origin[0] + plan.distance)

    def test_click_follows_probability(self) -> None:
        never = plan_sweep(
            (100.0, 200.0),
            screen=(2160, 1440),
            rng=random.Random(0),
            click_prob=0.0,
        )
        always = plan_sweep(
            (100.0, 200.0),
            screen=(2160, 1440),
            rng=random.Random(0),
            click_prob=1.0,
        )
        self.assertFalse(never.will_click)
        self.assertTrue(always.will_click)

    def test_no_click_when_disallowed(self) -> None:
        plan = plan_sweep(
            (100.0, 200.0),
            screen=(2160, 1440),
            rng=random.Random(0),
            click_prob=1.0,
            allow_click=False,
        )
        self.assertFalse(plan.will_click)

    def test_origin_too_far_right_is_rejected(self) -> None:
        plan = plan_sweep((2100.0, 200.0), screen=(2160, 1440), rng=random.Random(0), click_prob=1.0)
        self.assertFalse(plan.ok)
        self.assertIn("起点太靠右", plan.reason)

    def test_path_stays_horizontal_and_returns(self) -> None:
        plan = SweepPlan(origin=(300.0, 500.0), target=(800.0, 500.0), distance=500.0, will_click=True)
        pts = sweep_path(plan, steps=10)
        self.assertEqual(pts[0], (300.0, 500.0))
        self.assertEqual(pts[-1], (300.0, 500.0))
        self.assertTrue(any(p[0] == 800.0 for p in pts))
        self.assertTrue(all(p[1] == 500.0 for p in pts))
        self.assertTrue(all(p[0] >= 300.0 for p in pts))

    def test_click_happens_at_right_end_then_returns(self) -> None:
        mouse = FakeMouse()
        plan = SweepPlan(origin=(120.0, 900.0), target=(520.0, 900.0), distance=400.0, will_click=True)
        clicked_at: list[tuple[float, float]] = []

        def _click() -> None:
            clicked_at.append(mouse.position())
            mouse.clicks += 1

        mouse.click = _click  # type: ignore[method-assign]
        run_sweep(mouse, plan, sleeper=lambda _s: None)
        self.assertEqual(clicked_at, [(520.0, 900.0)])
        self.assertEqual(mouse.position(), (120.0, 900.0))
        self.assertEqual(outbound_path(plan, 4)[-1], (520.0, 900.0))
        self.assertEqual(inbound_path(plan, 4)[-1], (120.0, 900.0))

    def test_default_mouse_darwin(self) -> None:
        with patch("monitor.vpn_keepalive.sys.platform", "darwin"):
            self.assertIsInstance(default_mouse(), _MacMouse)

    def test_default_mouse_windows(self) -> None:
        with patch("monitor.vpn_keepalive.sys.platform", "win32"):
            with patch("monitor.vpn_keepalive._enable_win_dpi"):
                self.assertIsInstance(default_mouse(), _WinMouse)


if __name__ == "__main__":
    unittest.main()
