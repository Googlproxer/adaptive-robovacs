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

    def test_dashboard_groups_rooms_into_floor_and_bedroom_columns(self) -> None:
        source = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertIn('type: "grid"', source)
        self.assertIn('"Scheduler & robot settings"', source)
        self.assertIn('"Bedrooms"', source)
        self.assertIn("schedule.attrs.bedroom", source)
        self.assertIn("this._config.columns", source)

    def test_local_dashboard_copy_matches_served_card(self) -> None:
        local_copy = (
            Path(__file__).parents[1] / "dashboard" / "adaptive-robovacs-dashboard.js"
        )
        self.assertEqual(
            DASHBOARD_PATH.read_text(encoding="utf-8"),
            local_copy.read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
