from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monitor.config import STEEL_NAMES, relabel_vehicle_steels, steel_label


class SteelNameTests(unittest.TestCase):
    def test_agent_1_2_still_zhongfei(self) -> None:
        self.assertEqual(steel_label(1), "重废1")
        self.assertEqual(steel_label(2), "重废2")

    def test_business_enum_names(self) -> None:
        self.assertEqual(steel_label(3), "重废3")
        self.assertEqual(steel_label(4), "剪料1")
        self.assertEqual(steel_label(5), "剪料2")
        self.assertEqual(steel_label(6), "剪料3")
        self.assertEqual(steel_label(8), "破碎料1")
        self.assertEqual(steel_label(11), "中废")
        self.assertEqual(steel_label(12), "生铁")
        self.assertEqual(steel_label(13), "厚剪")
        self.assertEqual(steel_label(14), "钢筋切粒")
        self.assertEqual(steel_label(15), "汽车壳")

    def test_unknown_keeps_fallback(self) -> None:
        self.assertEqual(steel_label(99), "料型99")

    def test_relabel_fixes_type11_reason(self) -> None:
        row = relabel_vehicle_steels(
            {
                "human1": {"steel_type": 11, "label": "料型11", "pct": 80},
                "reason": "主料型不是重废1/2（料型11）",
            }
        )
        self.assertEqual(row["main_label"], "中废")
        self.assertEqual(row["human1"]["label"], "中废")
        self.assertEqual(row["reason"], "主料型不是重废1/2（中废）")

    def test_map_covers_1_to_16(self) -> None:
        self.assertEqual(set(STEEL_NAMES), set(range(1, 17)))


if __name__ == "__main__":
    unittest.main()
