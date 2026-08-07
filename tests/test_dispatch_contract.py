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

    def test_schema_error_recovery_unblocks_the_room(self) -> None:
        source = COORDINATOR_PATH.read_text(encoding="utf-8")
        self.assertIn("_async_clear_legacy_dispatch_schema_errors", source)
        self.assertIn("detail[\"map_status\"] = \"unknown\"", source)


if __name__ == "__main__":
    unittest.main()
