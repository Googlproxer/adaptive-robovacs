"""Contract checks for live discovery changes."""

from pathlib import Path
import unittest


ENTITY_PATH = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs" / "entity.py"
DASHBOARD_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "adaptive_robovacs"
    / "frontend"
    / "adaptive-robovacs-dashboard.js"
)


class DynamicDiscoveryContractTests(unittest.TestCase):
    """Ensure excluded rooms do not break state updates or clutter the card."""

    def test_stale_room_entities_do_not_write_state(self) -> None:
        source = ENTITY_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "self._area_id not in self.coordinator.discovery.rooms",
            source,
        )

    def test_dashboard_honours_hidden_area_ids(self) -> None:
        source = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertIn("hidden_area_ids", source)
        self.assertIn("hiddenAreaIds.has(areaId)", source)


if __name__ == "__main__":
    unittest.main()
