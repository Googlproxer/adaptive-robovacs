"""Contract checks for Home Assistant-native vacuum dispatch."""

from pathlib import Path
import unittest


COORDINATOR_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "adaptive_robovacs"
    / "coordinator.py"
)
RUNTIME_PATH = COORDINATOR_PATH.with_name("runtime.py")


class VacuumDispatchContractTests(unittest.TestCase):
    """Guard the native clean-area service payload without importing HA."""

    def test_native_cleaning_area_id_payload_is_used(self) -> None:
        source = RUNTIME_PATH.read_text(encoding="utf-8")
        self.assertIn('"cleaning_area_id": [room.area_id]', source)
        self.assertNotIn('{"entity_id": robot.entity_id, "area_id": [room.area_id]}', source)

    def test_dispatch_errors_are_generic_in_state_and_detailed_in_logs(self) -> None:
        source = (
            RUNTIME_PATH.read_text(encoding="utf-8")
            + COORDINATOR_PATH.read_text(encoding="utf-8")
        )
        self.assertIn('detail["map_status"] = "error"', source)
        self.assertIn('detail["map_error"] = "unknown dispatch error"', source)
        self.assertIn("_LOGGER.exception(", source)
        self.assertIn("enter cleaning within 10 minutes", source)
        self.assertNotIn("unmapped; awaiting native map repair", source)

    def test_a_recorded_dispatch_error_does_not_exclude_future_candidates(self) -> None:
        source = COORDINATOR_PATH.read_text(encoding="utf-8")
        candidate_start = source.index("    def _room_candidate")
        candidate_end = source.index("    async def _async_apply_profile", candidate_start)
        candidate_source = source[candidate_start:candidate_end]
        self.assertNotIn('detail.get("map_status")', candidate_source)


if __name__ == "__main__":
    unittest.main()
