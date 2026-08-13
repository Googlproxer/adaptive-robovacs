"""Contract checks for live discovery and dashboard packaging."""

from pathlib import Path
import unittest


ENTITY_PATH = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs" / "entity.py"
DISCOVERY_PATH = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs" / "discovery_core.py"
COORDINATOR_PATH = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs" / "coordinator.py"
DASHBOARD_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "adaptive_robovacs"
    / "frontend"
    / "adaptive-robovacs-dashboard.js"
)


class DynamicDiscoveryContractTests(unittest.TestCase):
    """Keep registry discovery and the packaged frontend contracts stable."""

    def test_stale_room_entities_do_not_write_state(self) -> None:
        source = ENTITY_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "self._area_id not in self.coordinator.discovery.rooms",
            source,
        )

    def test_dashboard_registers_only_the_target_scoped_cards(self) -> None:
        source = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertIn(
            'customElements.define("adaptive-robovacs-global"',
            source,
        )
        self.assertIn(
            'customElements.define("adaptive-robovacs-vacuum"',
            source,
        )
        self.assertIn(
            'customElements.define("adaptive-robovacs-room"',
            source,
        )
        self.assertNotIn(
            'customElements.define("adaptive-robovacs-dashboard"',
            source,
        )

    def test_local_dashboard_copy_matches_served_cards(self) -> None:
        local_copy = (
            Path(__file__).parents[1] / "dashboard" / "adaptive-robovacs-dashboard.js"
        )
        self.assertEqual(
            DASHBOARD_PATH.read_text(encoding="utf-8"),
            local_copy.read_text(encoding="utf-8"),
        )

    def test_robot_entities_follow_the_robot_friendly_name(self) -> None:
        entity_source = ENTITY_PATH.read_text(encoding="utf-8")
        discovery_source = DISCOVERY_PATH.read_text(encoding="utf-8")
        self.assertIn("robot_name_suffix", entity_source)
        self.assertIn("robot.name if robot else self._robot_entity_id", entity_source)
        self.assertIn("device.name_by_user", discovery_source)

    def test_capability_changes_trigger_dynamic_entity_discovery(self) -> None:
        source = COORDINATOR_PATH.read_text(encoding="utf-8")
        self.assertIn("prior_discovery = self.discovery", source)
        self.assertIn("if prior_discovery != self.discovery:", source)
        self.assertNotIn("prior_robots = set(self.discovery.robots)", source)


if __name__ == "__main__":
    unittest.main()
