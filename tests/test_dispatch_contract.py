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
ADAPTERS_PATH = COORDINATOR_PATH.parent / "adapters"
GENERIC_PATH = ADAPTERS_PATH / "generic.py"
ROBOROCK_PATH = ADAPTERS_PATH / "roborock.py"


class VacuumDispatchContractTests(unittest.TestCase):
    """Guard the native clean-area service payload without importing HA."""

    def test_native_cleaning_area_id_payload_is_used(self) -> None:
        source = GENERIC_PATH.read_text(encoding="utf-8")
        self.assertIn('"cleaning_area_id": list(request.area_ids)', source)
        self.assertNotIn('{"entity_id": robot.entity_id, "area_id": [room.area_id]}', source)

    def test_dispatch_errors_are_generic_in_state_and_detailed_in_logs(self) -> None:
        source = (
            RUNTIME_PATH.read_text(encoding="utf-8")
            + COORDINATOR_PATH.read_text(encoding="utf-8")
        )
        self.assertIn('detail["map_status"] = "error"', source)
        self.assertIn('detail["map_error"] = fault_summary(reason_code)', source)
        self.assertIn("_LOGGER.exception(", source)
        self.assertIn('"start_confirmation_failed"', source)
        self.assertNotIn("unmapped; awaiting native map repair", source)

    def test_roborock_native_paths_are_protocol_specific(self) -> None:
        source = ROBOROCK_PATH.read_text(encoding="utf-8")
        self.assertIn('"command": "app_segment_clean"', source)
        self.assertIn('"repeat": 2', source)
        self.assertIn('"command": "dpCommon"', source)
        self.assertIn('"command": "dpStartClean"', source)
        self.assertIn('"clean_paramters"', source)
        self.assertIn('"max_plus": 8', source)
        self.assertIn('"q10_max_plus_profile_write_failed"', source)
        self.assertIn('"q10_max_plus_start_failed"', source)
        self.assertIn("supports_roborock_native_two_pass", source)
        self.assertIn("is_roborock_q10_protocol", source)
        self.assertIn("return await self._generic.async_dispatch", source)

    def test_start_failure_holds_only_its_robot_and_later_assignments_continue(self) -> None:
        source = COORDINATOR_PATH.read_text(encoding="utf-8")
        self.assertIn('section = "room_faults" if reason_code in room_scoped_codes else "robot_faults"', source)
        self.assertIn('robot.registry_id in self.data.get("robot_faults", {})', source)
        dispatch_loop = source[source.index("                    if not ok:"):]
        dispatch_loop = dispatch_loop[:dispatch_loop.index("            elif self.scheduler_limited:")]
        self.assertIn("continue", dispatch_loop)
        self.assertNotIn("break", dispatch_loop)

    def test_q10_max_plus_failure_downgrades_only_the_effective_setting(self) -> None:
        coordinator = COORDINATOR_PATH.read_text(encoding="utf-8")
        runtime = RUNTIME_PATH.read_text(encoding="utf-8")
        self.assertIn("async def _async_downgrade_q10_max_plus", coordinator)
        self.assertIn('settings["fan_speed"] = "max"', coordinator)
        self.assertIn('"q10_max_plus_profile_write_failed"', runtime)
        self.assertIn('"q10_max_plus_start_failed"', runtime)
        self.assertIn('active.get("q10_max_plus_fallback")', coordinator)

    def test_native_targets_do_not_cross_the_adapter_boundary(self) -> None:
        public_source = "".join(
            path.read_text(encoding="utf-8")
            for path in (
                COORDINATOR_PATH,
                RUNTIME_PATH,
                COORDINATOR_PATH.with_name("state.py"),
                COORDINATOR_PATH.with_name("sensor.py"),
                COORDINATOR_PATH.with_name("projections.py"),
            )
        )
        self.assertNotIn('"segments"', public_source)
        self.assertNotIn('"app_segment_clean"', public_source)

    def test_a_recorded_dispatch_error_does_not_exclude_future_candidates(self) -> None:
        source = COORDINATOR_PATH.read_text(encoding="utf-8")
        candidate_start = source.index("    def _room_candidate")
        candidate_end = source.index("    async def _async_apply_profile", candidate_start)
        candidate_source = source[candidate_start:candidate_end]
        self.assertNotIn('detail.get("map_status")', candidate_source)


if __name__ == "__main__":
    unittest.main()
