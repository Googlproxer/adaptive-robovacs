"""Focused lifecycle tests for extracted job mutations without Home Assistant."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
import types
import unittest


PACKAGE_PATH = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs"
PACKAGE_NAME = "adaptive_robovacs_jobs_test"
package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(PACKAGE_PATH)]
sys.modules[PACKAGE_NAME] = package
SPEC = importlib.util.spec_from_file_location(f"{PACKAGE_NAME}.jobs", PACKAGE_PATH / "jobs.py")
assert SPEC and SPEC.loader
jobs_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = jobs_module
SPEC.loader.exec_module(jobs_module)

JobLifecycle = jobs_module.JobLifecycle


class _Coordinator:
    def __init__(self) -> None:
        self.data = {
            "active": {},
            "robot_cooldowns": {},
            "manual_events": [],
            "recovery_events": [],
            "occurrences": {},
            "water_confirmations": {},
            "water_notification_episodes": {},
        }
        self._rooms: dict[str, dict[str, object]] = {}
        self.discovery = types.SimpleNamespace(
            robots={"vacuum.alpha": types.SimpleNamespace()}
        )

    def _room_data(self, area_id: str) -> dict[str, object]:
        return self._rooms.setdefault(area_id, {"duration_samples": []})

    def _cancel_recovery_timer(self, _robot_id: str) -> None:
        return None

    def robot_registry_id(self, entity_id: str) -> str:
        return {"vacuum.alpha": "registry-alpha"}.get(entity_id, entity_id)


class JobLifecycleTests(unittest.TestCase):
    def test_active_rooms_retains_v1_single_room_checkpoints(self) -> None:
        self.assertEqual(JobLifecycle.active_rooms({"room": "study"}), ["study"])
        self.assertEqual(
            JobLifecycle.active_rooms({"room": "study", "rooms": ["study", "hall", "study"]}),
            ["study", "hall"],
        )

    def test_observed_scheduler_completion_trains_duration_and_closes_job(self) -> None:
        coordinator = _Coordinator()
        lifecycle = JobLifecycle(coordinator)
        completed = datetime(2026, 8, 9, 9, 30, tzinfo=timezone.utc)
        active = {
            "room": "study",
            "operation": "vacuum",
            "source": "scheduler",
            "passes": 1,
            "measured_minutes": 24.5,
            "duration_source": "elapsed_total_v2",
            "forecast_sample_eligible": True,
        }
        coordinator.data["active"]["vacuum.alpha"] = active

        lifecycle.complete("vacuum.alpha", active, completed, "observed")

        self.assertIsNone(coordinator.data["active"]["vacuum.alpha"])
        self.assertEqual(coordinator._rooms["study"]["vacuum"], completed.isoformat())
        self.assertEqual(
            coordinator._rooms["study"]["duration_samples"],
            [
                {
                    "minutes": 24.5,
                    "operation": "vacuum",
                    "passes": 1,
                    "robot": "registry-alpha",
                    "source": "elapsed_total_v2",
                    "at": completed.isoformat(),
                    "measurement_version": 2,
                }
            ],
        )

    def test_cancelled_manual_job_is_audited_without_recording_completion(self) -> None:
        coordinator = _Coordinator()
        lifecycle = JobLifecycle(coordinator)
        cancelled = datetime(2026, 8, 9, 9, 30, tzinfo=timezone.utc)
        active = {
            "room": "study",
            "rooms": ["study", "hall"],
            "source": "manual_home_assistant",
            "requested_operations": ["vacuum"],
            "manual_context_id": "ctx-1",
        }
        coordinator.data["active"]["vacuum.alpha"] = active

        lifecycle.cancel("vacuum.alpha", active, cancelled, "physical_cancelled")

        self.assertIsNone(coordinator.data["active"]["vacuum.alpha"])
        self.assertEqual(coordinator._rooms, {})
        self.assertEqual(coordinator.data["manual_events"][0]["outcome"], "cancelled")
        self.assertEqual(coordinator.data["recovery_events"][0]["reason"], "physical_cancelled")

    def test_recovered_completion_updates_cadence_without_training_duration(self) -> None:
        coordinator = _Coordinator()
        lifecycle = JobLifecycle(coordinator)
        completed = datetime(2026, 8, 9, 9, 30, tzinfo=timezone.utc)
        active = {
            "room": "study",
            "operation": "vacuum",
            "source": "scheduler",
            "passes": 1,
            "measured_minutes": 24.5,
            "duration_source": "elapsed_total_v2",
            "forecast_sample_eligible": True,
            "recovery_crossed": True,
        }
        coordinator.data["active"]["vacuum.alpha"] = active

        lifecycle.complete("vacuum.alpha", active, completed, "recovered_terminal_status")

        self.assertEqual(coordinator._rooms["study"]["vacuum"], completed.isoformat())
        self.assertEqual(coordinator._rooms["study"]["duration_samples"], [])

    def test_dashboard_manual_completion_updates_cadence_and_duration_once(self) -> None:
        coordinator = _Coordinator()
        lifecycle = JobLifecycle(coordinator)
        completed = datetime(2026, 8, 9, 9, 30, tzinfo=timezone.utc)
        coordinator.data["occurrences"]["study"] = {
            "occurrence_id": "occ-1",
            "current_stage": 0,
            "stages": [
                {
                    "operation": "vacuum",
                    "passes": 2,
                    "status": "running",
                }
            ],
        }
        active = {
            "room": "study",
            "operation": "vacuum",
            "source": "manual_dashboard",
            "manual_mode": "vacuum_only",
            "manual_context_id": "ctx-2",
            "occurrence_id": "occ-1",
            "stage_index": 0,
            "passes": 2,
            "measured_minutes": 18.0,
            "duration_source": "elapsed_total_v2",
            "forecast_sample_eligible": True,
        }
        coordinator.data["active"]["vacuum.alpha"] = active

        lifecycle.complete("vacuum.alpha", active, completed, "observed")

        self.assertEqual(coordinator._rooms["study"]["cleaning"], completed.isoformat())
        self.assertEqual(len(coordinator._rooms["study"]["duration_samples"]), 1)
        self.assertEqual(coordinator.data["manual_events"][-1]["outcome"], "completed")
        self.assertNotIn("study", coordinator.data["occurrences"])

    def test_physical_cancellation_cools_down_only_the_cancelled_robot(self) -> None:
        coordinator = _Coordinator()
        lifecycle = JobLifecycle(coordinator)
        cancelled = datetime(2026, 8, 9, 9, 30, tzinfo=timezone.utc)

        changed = lifecycle.rebase_cancelled_floor("vacuum.alpha", cancelled)

        self.assertEqual(changed, [])
        self.assertEqual(
            coordinator.data["robot_cooldowns"]["vacuum.alpha"]["until"],
            (cancelled + jobs_module.CANCELLATION_COOLDOWN).isoformat(),
        )
        self.assertEqual(coordinator._rooms, {})


if __name__ == "__main__":
    unittest.main()
