"""Contracts for the visual room-topology foundation."""

from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "custom_components" / "adaptive_robovacs"


class FloorPlanContractTests(unittest.TestCase):
    def test_floor_plan_is_typed_registry_backed_and_non_dispatching(self) -> None:
        state = (PACKAGE / "state.py").read_text(encoding="utf-8")
        discovery = (PACKAGE / "discovery_core.py").read_text(encoding="utf-8")
        coordinator = (PACKAGE / "coordinator.py").read_text(encoding="utf-8")

        self.assertIn("class FloorPlanState", state)
        self.assertIn("floor_plan", state)
        self.assertIn("class DiscoveredOccupancySource", discovery)
        self.assertIn("registry_id", discovery)
        self.assertIn("async def async_save_floor_plan", coordinator)
        self.assertIn("async def async_set_room_adjacency", coordinator)
        save_section = coordinator.split("    async def async_save_floor_plan", 1)[1].split(
            "    async def async_set_room_adjacency", 1
        )[0]
        self.assertNotIn("async_evaluate", save_section)
        self.assertNotIn("_async_dispatch", save_section)

    def test_dashboard_and_services_expose_the_admin_floor_plan_surface(self) -> None:
        dashboard = (PACKAGE / "frontend" / "adaptive-robovacs-dashboard.js").read_text(
            encoding="utf-8"
        )
        services = (PACKAGE / "services.py").read_text(encoding="utf-8")
        yaml = (PACKAGE / "services.yaml").read_text(encoding="utf-8")

        self.assertIn('customElements.define("adaptive-robovacs-floorplan"', dashboard)
        self.assertIn("save_floor_plan", dashboard)
        self.assertIn("data-link-source", dashboard)
        self.assertIn("data-sensor", dashboard)
        self.assertIn("_placeUnplacedRoom", dashboard)
        self.assertIn("_placeUnplacedSensor", dashboard)
        self.assertIn("Place ${sensor.room_name} before adding", dashboard)
        self.assertIn("_setLinkPreview", dashboard)
        self.assertIn("link-preview-dot", dashboard)
        self.assertIn("var(--success-color, #4caf50)", dashboard)
        self.assertIn(".room.active rect { stroke:var(--success-color, #4caf50);", dashboard)
        self.assertIn(".sensor.unavailable circle { fill:var(--error-color); }", dashboard)
        self.assertIn("_require_admin", services)
        self.assertIn("SERVICE_SAVE_FLOOR_PLAN", services)
        self.assertIn("SERVICE_SET_ROOM_ADJACENCY", services)
        self.assertIn("save_floor_plan:", yaml)
        self.assertIn("set_room_adjacency:", yaml)


if __name__ == "__main__":
    unittest.main()
