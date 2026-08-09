"""Contract checks for user-initiated Home Assistant room-clean tracking."""

from pathlib import Path
import unittest


COORDINATOR_PATH = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs" / "coordinator.py"
JOBS_PATH = COORDINATOR_PATH.with_name("jobs.py")
SENSOR_PATH = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs" / "sensor.py"


class ManualTrackingContractTests(unittest.TestCase):
    def test_only_user_context_clean_area_calls_are_captured(self) -> None:
        source = COORDINATOR_PATH.read_text(encoding="utf-8") + JOBS_PATH.read_text(encoding="utf-8")
        self.assertIn("EVENT_CALL_SERVICE", source)
        self.assertIn("parse_manual_clean_request", source)
        self.assertIn('"manual_home_assistant"', source)
        self.assertIn('"manual_context_id"', source)

    def test_physical_cancellation_does_not_change_home_assistant_room_tracking(self) -> None:
        source = COORDINATOR_PATH.read_text(encoding="utf-8") + JOBS_PATH.read_text(encoding="utf-8")
        self.assertNotIn("parse_manual_cancel_request", source)
        self.assertNotIn("cancel_requested_at", source)
        self.assertIn("_cancel_job", source)
        self.assertIn('"physical_cancelled"', source)

    def test_scheduler_and_unstarted_manual_jobs_are_not_misclassified(self) -> None:
        source = COORDINATOR_PATH.read_text(encoding="utf-8")
        self.assertIn('"scheduler job already active"', source)
        self.assertIn('"not_started_or_cancelled"', source)

    def test_completion_defers_without_resetting_room_cadence(self) -> None:
        source = COORDINATOR_PATH.read_text(encoding="utf-8") + JOBS_PATH.read_text(encoding="utf-8")
        self.assertIn("_apply_manual_deferral", source)
        self.assertIn('active.get("source") == "manual_home_assistant"', source)
        self.assertIn('active.get("source") == "scheduler"', source)

    def test_robot_status_exposes_manual_source_and_room_names(self) -> None:
        source = SENSOR_PATH.read_text(encoding="utf-8")
        self.assertIn('"activity_source"', source)
        self.assertIn('"rooms"', source)


if __name__ == "__main__":
    unittest.main()
