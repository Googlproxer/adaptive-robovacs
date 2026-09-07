"""Contract checks for Home Assistant label registry IDs."""

import unittest
from pathlib import Path

CONST_PATH = (
    Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs" / "const.py"
)


class LabelContractTests(unittest.TestCase):
    """Ensure discovery matches Home Assistant's normalized label IDs."""

    def test_robovac_labels_use_normalized_ids(self) -> None:
        source = CONST_PATH.read_text(encoding="utf-8")
        for label_id in (
            "robovac_bedroom",
            "robovac_exclude",
            "robovac_radar",
        ):
            self.assertIn(f'"{label_id}"', source)


if __name__ == "__main__":
    unittest.main()
