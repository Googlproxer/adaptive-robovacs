"""Regression contracts for the 2026-08-13 full-project review."""

from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]
INTEGRATION = ROOT / "custom_components" / "adaptive_robovacs"


class ProjectReviewRemediationTests(unittest.TestCase):
    def test_offline_hold_classification_precedes_held_early_return(self) -> None:
        source = (INTEGRATION / "coordinator.py").read_text(encoding="utf-8")
        recovery = source[source.index("    async def _async_recover_active_jobs"):]
        outcome = recovery.index("outcome = offline_held_recovery_outcome(")
        held_return = recovery.index('if action == "held":')
        self.assertLess(outcome, held_return)

    def test_durable_robot_state_uses_registry_identity(self) -> None:
        coordinator = (INTEGRATION / "coordinator.py").read_text(encoding="utf-8")
        jobs = (INTEGRATION / "jobs.py").read_text(encoding="utf-8")
        self.assertIn('robot.registry_id,\n            {', coordinator)
        self.assertIn('entity_to_registry.get(key, key)', coordinator)
        self.assertIn('"robot": durable_robot_id', jobs)
        self.assertIn("robot_entity_aliases", coordinator)

    def test_robot_specific_duration_is_forecast_before_assignment(self) -> None:
        source = (INTEGRATION / "coordinator.py").read_text(encoding="utf-8")
        resolver = source[
            source.index("    def _resolve_candidate_for_robot"):source.index(
                "    def _skip_occurrence_stage"
            )
        ]
        self.assertIn("robot.registry_id", resolver)
        self.assertGreaterEqual(resolver.count("self._forecast("), 2)
        self.assertGreaterEqual(resolver.count("if not forecast.allowed:"), 2)
        self.assertIn("def _candidate_robot_diagnostics", source)
        self.assertIn("robot_eligibility", source)

        dispatch = source[source.index("                for robot, candidate in assignments:"):]
        prepare = dispatch.index("await self._async_prepare_occurrence(")
        first_recheck = dispatch.index("prepare_candidate, prepare_reason = self._room_candidate(")
        self.assertLess(first_recheck, prepare)

    def test_unload_gates_and_drains_config_entry_tasks(self) -> None:
        coordinator = (INTEGRATION / "coordinator.py").read_text(encoding="utf-8")
        integration = (INTEGRATION / "integration_core.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("self.hass.async_create_task", coordinator)
        self.assertIn("self.entry.async_create_task", coordinator)
        self.assertIn("_, pending = await asyncio.wait(tasks, timeout=10)", coordinator)
        self.assertIn("await asyncio.gather(*pending, return_exceptions=True)", coordinator)
        self.assertIn("coordinator.begin_shutdown()", integration)

    def test_remove_entry_does_not_read_runtime_data(self) -> None:
        source = (INTEGRATION / "integration_core.py").read_text(encoding="utf-8")
        removal = source[source.index("async def async_remove_entry"):]
        self.assertNotIn("runtime_data", removal)
        self.assertIn("await store.async_remove()", removal)
        self.assertIn("notification_delivery_issue_id", removal)
        self.assertIn("cleaning_program_issue_id", removal)

    def test_unset_profile_has_an_explicit_option(self) -> None:
        source = (INTEGRATION / "select.py").read_text(encoding="utf-8")
        self.assertIn('NOT_CONFIGURED_OPTION = "Not configured"', source)
        self.assertIn("None if option == NOT_CONFIGURED_OPTION else option", source)
        self.assertNotIn("else (self.options[0]", source)

    def test_discovery_prefers_translation_metadata(self) -> None:
        discovery = (INTEGRATION / "discovery_core.py").read_text(encoding="utf-8")
        self.assertIn("profile_control_kind(translation_key, options", discovery)
        self.assertIn('kind == "mop_intensity"', discovery)


if __name__ == "__main__":
    unittest.main()
