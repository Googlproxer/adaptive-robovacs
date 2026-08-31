"""Contract checks for durable scheduler job lifecycle behaviour."""

from pathlib import Path
import unittest


COORDINATOR_PATH = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs" / "coordinator.py"
JOBS_PATH = COORDINATOR_PATH.with_name("jobs.py")
PROJECTIONS_PATH = COORDINATOR_PATH.with_name("projections.py")
SENSOR_PATH = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs" / "sensor.py"
BUTTON_PATH = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs" / "button.py"
DISCOVERY_PATH = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs" / "discovery_core.py"


class LifecycleContractTests(unittest.TestCase):
    """Guard restart recovery and presentation without a Home Assistant runtime."""

    def test_active_jobs_persist_expected_end_and_observed_start(self) -> None:
        source = COORDINATOR_PATH.read_text(encoding="utf-8") + JOBS_PATH.read_text(encoding="utf-8")
        self.assertIn('"expected_end"', source)
        self.assertIn('"observed_started"', source)
        self.assertIn('"last_observed_at"', source)
        self.assertIn('_async_recover_active_jobs', source)
        self.assertIn('"docked_at"', source)
        self.assertIn('"dock_completion_pending"', source)
        self.assertIn('"recovered_terminal_status"', source)

    def test_recovered_estimates_do_not_train_duration_learning(self) -> None:
        source = COORDINATOR_PATH.read_text(encoding="utf-8") + JOBS_PATH.read_text(encoding="utf-8")
        self.assertIn('confidence == "observed"', source)
        self.assertIn('active.get("forecast_sample_eligible")', source)
        self.assertIn('not active.get("recovery_crossed")', source)
        self.assertIn('"elapsed_total_v2"', source)

    def test_live_native_transition_wins_over_a_recovery_estimate(self) -> None:
        source = COORDINATOR_PATH.read_text(encoding="utf-8")
        self.assertIn("async_track_point_in_utc_time", source)
        self.assertIn("recovery_transition_is_observed", source)
        self.assertIn("transition=transition", source)

    def test_room_status_is_in_progress_for_an_active_room(self) -> None:
        source = SENSOR_PATH.read_text(encoding="utf-8")
        self.assertIn('return "In Progress"', source)
        self.assertIn('"expected_end_at"', source)
        self.assertIn('if state["active"] else None', source)
        self.assertIn('"learned_duration_minutes"', source)
        self.assertIn('return "Completion pending"', source)
        self.assertIn('return "Dock servicing"', source)
        self.assertIn('"predicted_total_minutes"', source)
        self.assertIn('"last_completion_confidence"', source)
        self.assertIn('return state["state"]', source)
        self.assertIn('state["desired_window_start"]', source)
        self.assertIn('"desired_window_start"', source)
        self.assertIn('"unresolved_window_start"', source)
        self.assertIn('"unresolved_window_start"', PROJECTIONS_PATH.read_text(encoding="utf-8"))

    def test_desired_window_supports_per_room_inheritance_and_the_existing_bypass(self) -> None:
        coordinator = (
            COORDINATOR_PATH.read_text(encoding="utf-8")
            + PROJECTIONS_PATH.read_text(encoding="utf-8")
        )
        switch = (Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs" / "switch.py").read_text(
            encoding="utf-8"
        )
        select = (Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs" / "select.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"ignore_desired_window": False', coordinator)
        self.assertIn('"waiting for desired cleaning window"', coordinator)
        self.assertIn("if not self._desired_window_allows(room, now)", coordinator)
        self.assertIn("unresolved_window_allowed = self._unresolved_allowed(room, now)", coordinator)
        self.assertIn("window = self._desired_window(room)", coordinator)
        self.assertIn('"desired_window_start": None', coordinator)
        self.assertIn('"desired_window_end": None', coordinator)
        self.assertIn('"desired_window_effective_start"', coordinator)
        self.assertIn('"desired_window_effective_end"', coordinator)
        self.assertIn('"desired_window_start_inherited"', coordinator)
        self.assertIn('"desired_window_end_inherited"', coordinator)
        self.assertIn('"desired_window_start"', coordinator)
        self.assertIn('"unresolved_window_start": desired_window_start', coordinator)
        self.assertIn('"ignore_desired_window"', switch)
        self.assertIn("Desired cleaning start", select)
        self.assertIn("Desired cleaning end", select)
        self.assertIn('USE_GLOBAL_OPTION = "Use global"', select)
        self.assertIn('f"room_window_{bound}_control"', select)

    def test_cleaning_timer_is_discovered_from_the_robot_device(self) -> None:
        source = DISCOVERY_PATH.read_text(encoding="utf-8")
        self.assertIn("cleaning_time_entity_id", source)
        self.assertIn('device_class == "duration"', source)

    def test_paused_and_error_jobs_follow_physical_robot_recovery(self) -> None:
        coordinator = COORDINATOR_PATH.read_text(encoding="utf-8")
        sensor = SENSOR_PATH.read_text(encoding="utf-8")
        button = BUTTON_PATH.read_text(encoding="utf-8")
        self.assertIn('"robot_holds"', coordinator)
        self.assertIn('"error_waiting"', coordinator)
        self.assertIn("held_job_transition", coordinator)
        self.assertIn("offline_held_recovery_outcome", coordinator)
        self.assertIn("_apply_robot_cancellation_deferral", coordinator)
        self.assertIn("_cancel_recovery_timer", coordinator)
        self.assertIn('return "Scheduler held"', sensor)
        self.assertIn('return "Paused"', sensor)
        self.assertIn('return "Returning to dock"', sensor)
        self.assertNotIn("Confirm held clean cancelled", button)
        self.assertNotIn("async_confirm_held_clean_cancelled", coordinator)

    def test_mop_washing_is_start_evidence_without_claiming_room_completion(self) -> None:
        coordinator = COORDINATOR_PATH.read_text(encoding="utf-8")
        models = COORDINATOR_PATH.with_name("models.py").read_text(encoding="utf-8")
        self.assertIn("def mop_stage_start_is_observed", models)
        self.assertIn("def _mop_washing_is_observed", coordinator)
        self.assertIn('active["phase"] = "mop_washing"', coordinator)
        self.assertIn('active["mop_washing_at"]', coordinator)
        self.assertIn("self._cancel_start_confirmation(robot.entity_id)", coordinator)
        self.assertIn('state_text not in {"cleaning", "returning"}', coordinator)


if __name__ == "__main__":
    unittest.main()
