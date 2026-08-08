"""Contract checks for Home Assistant-native vacuum dispatch."""

from pathlib import Path
import unittest


COORDINATOR_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "adaptive_robovacs"
    / "coordinator.py"
)


class VacuumDispatchContractTests(unittest.TestCase):
    """Guard the native clean-area service payload without importing HA."""

    def test_native_cleaning_area_id_payload_is_used(self) -> None:
        source = COORDINATOR_PATH.read_text(encoding="utf-8")
        self.assertIn('"cleaning_area_id": [room.area_id]', source)
        self.assertNotIn('{"entity_id": robot.entity_id, "area_id": [room.area_id]}', source)

    def test_legacy_schema_error_recovery_unblocks_the_room(self) -> None:
        source = COORDINATOR_PATH.read_text(encoding="utf-8")
        self.assertIn("_async_migrate_legacy_dispatch_errors", source)
        self.assertIn("detail[\"map_status\"] = \"unknown\"", source)

    def test_dispatch_errors_are_generic_in_state_and_detailed_in_logs(self) -> None:
        source = COORDINATOR_PATH.read_text(encoding="utf-8")
        self.assertIn('detail["map_status"] = "error"', source)
        self.assertIn('detail["map_error"] = "unknown dispatch error"', source)
        self.assertIn("_LOGGER.exception(", source)
        self.assertIn("enter cleaning within 10 minutes", source)
        self.assertNotIn("unmapped; awaiting native map repair", source)


if __name__ == "__main__":
    unittest.main()
