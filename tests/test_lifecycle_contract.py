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
        self.assertIn('"recovered_expected_end"', source)

    def test_recovered_estimates_do_not_train_duration_learning(self) -> None:
        source = COORDINATOR_PATH.read_text(encoding="utf-8") + JOBS_PATH.read_text(encoding="utf-8")
        self.assertIn('confidence == "observed"', source)
        self.assertIn('"robot": robot_id', source)
        self.assertIn('active.get("duration_source", "state_transition")', source)

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
        self.assertIn('return state["state"]', source)
        self.assertIn('state["desired_window_start"]', source)
        self.assertIn('"desired_window_start"', source)
        self.assertIn('"unresolved_window_start"', source)
        self.assertIn('"unresolved_window_start"', PROJECTIONS_PATH.read_text(encoding="utf-8"))

    def test_desired_window_uses_existing_controls_with_a_room_override(self) -> None:
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
        self.assertIn('"desired_window_start"', coordinator)
        self.assertIn('"unresolved_window_start": desired_window_start', coordinator)
        self.assertIn('"ignore_desired_window"', switch)
        self.assertIn("Desired cleaning start", select)
        self.assertIn("Desired cleaning end", select)

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
        self.assertIn("_remove_deprecated_confirmation_buttons", coordinator)
        self.assertIn("_cancel_recovery_timer", coordinator)
        self.assertIn('return "Scheduler held"', sensor)
        self.assertIn('return "Paused"', sensor)
        self.assertIn('return "Returning to dock"', sensor)
        self.assertNotIn("Confirm held clean cancelled", button)
        self.assertNotIn("async_confirm_held_clean_cancelled", coordinator)


if __name__ == "__main__":
    unittest.main()
