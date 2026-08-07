"""Contract checks for durable scheduler job lifecycle behaviour."""

from pathlib import Path
import unittest


COORDINATOR_PATH = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs" / "coordinator.py"
SENSOR_PATH = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs" / "sensor.py"
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
        self.assertIn('"robot_timer" if active.get("timer_start") is not None else "state_transition"', source)

    def test_room_status_is_in_progress_for_an_active_room(self) -> None:
        source = SENSOR_PATH.read_text(encoding="utf-8")
        self.assertIn('return "In Progress"', source)
        self.assertIn('"expected_end_at"', source)
        self.assertIn('if state["active"] else None', source)
        self.assertIn('"learned_duration_minutes"', source)

    def test_cleaning_timer_is_discovered_from_the_robot_device(self) -> None:
        source = DISCOVERY_PATH.read_text(encoding="utf-8")
        self.assertIn("cleaning_time_entity_id", source)
        self.assertIn('device_class == "duration"', source)


if __name__ == "__main__":
    unittest.main()
