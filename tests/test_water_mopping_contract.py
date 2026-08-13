"""Static contracts for fail-closed water-aware notification orchestration."""

from pathlib import Path
import unittest


PACKAGE = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs"


class WaterMoppingContractTests(unittest.TestCase):
    def test_confirmation_has_exact_actions_timeout_and_dedicated_channel(self) -> None:
        source = (PACKAGE / "coordinator.py").read_text(encoding="utf-8")
        self.assertIn('"title": "Confirm water"', source)
        self.assertIn('"title": "Cancel mopping"', source)
        self.assertIn('"timeout": 3600', source)
        self.assertIn('"Adaptive RoboVacs - Mop confirmation"', source)
        self.assertIn('"mobile_app_notification_action"', source)
        self.assertIn('"mobile_app_notification_cleared"', source)
        self.assertIn('"message": "clear_notification"', source)

    def test_action_tokens_are_hashed_before_persistence(self) -> None:
        source = (PACKAGE / "coordinator.py").read_text(encoding="utf-8")
        state = (PACKAGE / "state.py").read_text(encoding="utf-8")
        self.assertIn("secrets.token_urlsafe", source)
        self.assertIn("hashlib.sha256", source)
        self.assertIn('"confirm_hash": self._action_hash(confirm_action)', source)
        self.assertIn('"cancel_hash": self._action_hash(cancel_action)', source)
        self.assertNotIn("confirm_action:", state)
        self.assertNotIn("cancel_action:", state)

    def test_water_blocks_are_not_routed_into_the_global_fault_latch(self) -> None:
        runtime = (PACKAGE / "runtime.py").read_text(encoding="utf-8")
        blocked = runtime.index("if preflight.blocked")
        generic_fault = runtime.index("if not preflight.ready", blocked)
        self.assertLess(blocked, generic_fault)
        section = runtime[blocked:generic_fault]
        self.assertIn("_async_handle_mop_preflight_blocked", section)
        self.assertNotIn("_async_latch_scheduler_fault", section)

    def test_vacuum_stage_does_not_apply_mop_only_profile_controls(self) -> None:
        runtime = (PACKAGE / "runtime.py").read_text(encoding="utf-8")
        apply_profile = runtime.index("async def async_apply_profile")
        request = runtime.index("def _request", apply_profile)
        section = runtime[apply_profile:request]
        mop_gate = section.index('if operation == "mop":')
        self.assertGreater(section.index("profile.mop_mode_select_entity_id"), mop_gate)
        self.assertGreater(section.index("profile.mop_intensity_select_entity_id"), mop_gate)
        self.assertNotIn('stage_mode or settings.get("mode")', section)

    def test_every_physical_dispatch_gets_a_fresh_safety_evaluation(self) -> None:
        coordinator = (PACKAGE / "coordinator.py").read_text(encoding="utf-8")
        dispatch_loop = coordinator.index("for robot, candidate in assignments:")
        dispatch_call = coordinator.index("ok, message = await self._async_dispatch", dispatch_loop)
        section = coordinator[dispatch_loop:dispatch_call]
        self.assertIn("self._observe_occupancy(dispatch_now)", section)
        self.assertIn("self._room_candidate(", section)
        self.assertIn("self._robot_ready(robot)", section)
        self.assertIn("self._candidate_for_robot(fresh_candidate, robot)", section)

    def test_program_options_and_capability_sources_refresh_dynamically(self) -> None:
        selects = (PACKAGE / "select.py").read_text(encoding="utf-8")
        coordinator = (PACKAGE / "coordinator.py").read_text(encoding="utf-8")
        self.assertIn("def options(self) -> tuple[str, ...]:", selects)
        self.assertIn(
            '"mop" in robot.adapter_capabilities.supported_operations', selects
        )
        for field in (
            "mode_select_entity_id",
            "mop_mode_select_entity_id",
            "mop_intensity_select_entity_id",
            "passes_select_entity_id",
        ):
            self.assertIn(f"robot.profile.{field}", coordinator)
        self.assertIn('if evidence.domain == "select"', coordinator)

    def test_frontend_copies_are_identical_with_new_room_roles(self) -> None:
        served = (PACKAGE / "frontend" / "adaptive-robovacs-dashboard.js").read_bytes()
        standalone = (
            Path(__file__).parents[1] / "dashboard" / "adaptive-robovacs-dashboard.js"
        ).read_bytes()
        self.assertEqual(served, standalone)
        text = served.decode("utf-8")
        self.assertIn("room_cleaning_program_control", text)
        self.assertIn("room_mop_pass_count_control", text)


if __name__ == "__main__":
    unittest.main()
