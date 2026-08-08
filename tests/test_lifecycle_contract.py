"""Contract checks for durable scheduler job lifecycle behaviour."""

from pathlib import Path
import unittest


COORDINATOR_PATH = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs" / "coordinator.py"
SENSOR_PATH = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs" / "sensor.py"
BUTTON_PATH = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs" / "button.py"
DISCOVERY_PATH = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs" / "discovery_core.py"


class LifecycleContractTests(unittest.TestCase):
    """Guard restart recovery and presentation without a Home Assistant runtime."""

    def test_active_jobs_persist_expected_end_and_observed_start(self) -> None:
        source = COORDINATOR_PATH.read_text(encoding="utf-8")
        self.assertIn('"expected_end"', source)
        self.assertIn('"observed_started"', source)
        self.assertIn('_async_recover_active_jobs', source)
        self.assertIn('"recovered_expected_end"', source)

    def test_recovered_estimates_do_not_train_duration_learning(self) -> None:
        source = COORDINATOR_PATH.read_text(encoding="utf-8")
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
        self.assertIn('state["unresolved_window_start"]', source)
        self.assertIn('"unresolved_window_start"', COORDINATOR_PATH.read_text(encoding="utf-8"))

    def test_cleaning_timer_is_discovered_from_the_robot_device(self) -> None:
        source = DISCOVERY_PATH.read_text(encoding="utf-8")
        self.assertIn("cleaning_time_entity_id", source)
        self.assertIn('device_class == "duration"', source)

    def test_paused_and_error_jobs_are_held_until_user_resolution(self) -> None:
        coordinator = COORDINATOR_PATH.read_text(encoding="utf-8")
        sensor = SENSOR_PATH.read_text(encoding="utf-8")
        button = BUTTON_PATH.read_text(encoding="utf-8")
        self.assertIn('"robot_holds"', coordinator)
        self.assertIn('"error_waiting"', coordinator)
        self.assertIn("active_job_should_stay_held", coordinator)
        self.assertIn("_cancel_recovery_timer", coordinator)
        self.assertIn("async_confirm_held_clean_cancelled", coordinator)
        self.assertIn('return "Scheduler held"', sensor)
        self.assertIn('return "Paused"', sensor)
        self.assertIn("Confirm held clean cancelled", button)


if __name__ == "__main__":
    unittest.main()
