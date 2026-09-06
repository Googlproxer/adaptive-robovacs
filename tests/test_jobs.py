"""Focused tests for pure job lifecycle and recovery reducers."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

PACKAGE_PATH = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs"
PACKAGE_NAME = "adaptive_robovacs_jobs_test"
package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(PACKAGE_PATH)]
sys.modules[PACKAGE_NAME] = package
SPEC = importlib.util.spec_from_file_location(
    f"{PACKAGE_NAME}.jobs", PACKAGE_PATH / "jobs.py"
)
assert SPEC and SPEC.loader
jobs = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = jobs
SPEC.loader.exec_module(jobs)
models = sys.modules[f"{PACKAGE_NAME}.models"]
state = sys.modules[f"{PACKAGE_NAME}.state"]


def active_job(**overrides):
    values = {
        "room_id": "study",
        "room_ids": ["study"],
        "operation": models.CleaningOperation.VACUUM,
        "phase": models.JobPhase.CLEANING,
        "source": models.JobSource.SCHEDULER,
        "passes": 1,
    }
    values.update(overrides)
    return state.ActiveJob(**values)


class JobReducerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.when = datetime(2026, 8, 9, 9, 30, tzinfo=UTC)

    def test_active_rooms_retains_v1_single_room_checkpoints(self) -> None:
        self.assertEqual(jobs.active_rooms(active_job(room_ids=[])), ("study",))
        self.assertEqual(
            jobs.active_rooms(active_job(room_ids=["study", "hall", "study"])),
            ("study", "hall"),
        )

    def test_observed_scheduler_completion_emits_duration_sample(self) -> None:
        transition = jobs.reduce_job_completion(
            "vacuum.alpha",
            "registry-alpha",
            active_job(
                measured_minutes=24.5,
                duration_source="elapsed_total_v2",
                forecast_sample_eligible=True,
            ),
            self.when,
            "observed",
            None,
        )

        self.assertTrue(transition.set_room_operation_completion)
        self.assertEqual(transition.robot_registry_id, "registry-alpha")
        self.assertIsNotNone(transition.duration_sample)
        self.assertEqual(transition.duration_sample.robot_registry_id, "registry-alpha")
        self.assertEqual(transition.duration_sample.minutes, 24.5)

    def test_cancelled_manual_job_is_audited_without_completion(self) -> None:
        transition = jobs.reduce_job_cancellation(
            "vacuum.alpha",
            "registry-alpha",
            active_job(
                room_ids=["study", "hall"],
                source=models.JobSource.MANUAL_HOME_ASSISTANT,
                requested_operations=[models.CleaningOperation.VACUUM],
                manual_context_id="ctx-1",
            ),
            self.when,
            "physical_cancelled",
            None,
        )

        self.assertFalse(transition.set_room_operation_completion)
        self.assertEqual(transition.manual_audit.outcome, "cancelled")
        self.assertEqual(
            transition.manual_audit.robot_registry_id,
            "registry-alpha",
        )
        self.assertEqual(transition.recovery_audit.reason, "physical_cancelled")

    def test_recovered_completion_never_trains_duration(self) -> None:
        transition = jobs.reduce_job_completion(
            "vacuum.alpha",
            "registry-alpha",
            active_job(
                measured_minutes=24.5,
                duration_source="elapsed_total_v2",
                forecast_sample_eligible=True,
                recovery_crossed=True,
            ),
            self.when,
            "recovered_terminal_status",
            None,
        )

        self.assertTrue(transition.set_room_operation_completion)
        self.assertIsNone(transition.duration_sample)

    def test_dashboard_completion_advances_and_closes_occurrence(self) -> None:
        occurrence = state.CleaningOccurrence(
            occurrence_id="occ-1",
            room_id="study",
            robot_registry_id="registry-alpha",
            robot_entity_id=None,
            program=models.CleaningProgram.VACUUM_ONLY,
            stages=[
                state.CleaningStage(
                    operation=models.CleaningOperation.VACUUM,
                    passes=2,
                    status=models.StageStatus.RUNNING,
                )
            ],
            scheduled_at=self.when,
            created_at=self.when,
            adapter_id="generic",
            adapter_schema_version=1,
            source=models.OccurrenceSource.MANUAL_DASHBOARD,
        )
        transition = jobs.reduce_job_completion(
            "vacuum.alpha",
            "registry-alpha",
            active_job(
                source=models.JobSource.MANUAL_DASHBOARD,
                occurrence_id="occ-1",
                stage_index=0,
                passes=2,
                measured_minutes=18.0,
                duration_source="elapsed_total_v2",
                forecast_sample_eligible=True,
            ),
            self.when,
            "observed",
            occurrence,
        )

        self.assertTrue(transition.remove_occurrence)
        self.assertTrue(transition.set_room_cleaning_completion)
        self.assertEqual(transition.manual_audit.outcome, "completed")
        self.assertIsNotNone(transition.duration_sample)

    def test_cancellation_cooldown_is_scoped_to_stable_robot(self) -> None:
        cooldown = jobs.cancellation_cooldown(True, self.when)
        assert cooldown is not None
        self.assertEqual(
            cooldown.until,
            self.when + jobs.CANCELLATION_COOLDOWN,
        )
        self.assertEqual(cooldown.reason, "physical_cancelled")
        self.assertIsNone(jobs.cancellation_cooldown(False, self.when))

    def test_pending_profile_refresh_requires_unstarted_scheduler_stage(self) -> None:
        occurrence = state.CleaningOccurrence(
            occurrence_id="occ-1",
            room_id="study",
            robot_registry_id="registry-alpha",
            robot_entity_id=None,
            program=models.CleaningProgram.VACUUM_ONLY,
            stages=[],
            scheduled_at=self.when,
            created_at=self.when,
            adapter_id="generic",
            adapter_schema_version=1,
        )
        pending = state.CleaningStage(models.CleaningOperation.VACUUM, 1)
        running = state.CleaningStage(
            models.CleaningOperation.VACUUM,
            1,
            status=models.StageStatus.RUNNING,
            started_at=self.when,
        )

        self.assertTrue(
            jobs.can_refresh_pending_occurrence_profile(
                occurrence, pending, "docked", False
            )
        )
        self.assertFalse(
            jobs.can_refresh_pending_occurrence_profile(
                occurrence, running, "docked", False
            )
        )
        self.assertFalse(
            jobs.can_refresh_pending_occurrence_profile(
                occurrence, pending, "idle", False
            )
        )

    def test_unconfirmed_scheduler_clean_is_native_app_activity(self) -> None:
        fault = state.SchedulerFault(
            reason_code="start_outcome_uncertain",
            robot_registry_id="registry-alpha",
            room_area_id="study",
            occurred_at=self.when,
            phase="dispatch",
        )
        active = active_job(seen_cleaning=False)
        self.assertTrue(
            jobs.should_assume_native_app_clean(
                "cleaning", fault, "registry-alpha", active
            )
        )
        self.assertFalse(
            jobs.should_assume_native_app_clean(
                "docked", fault, "registry-alpha", active
            )
        )
        active.seen_cleaning = True
        self.assertFalse(
            jobs.should_assume_native_app_clean(
                "cleaning", fault, "registry-alpha", active
            )
        )

    def test_manual_deferral_reducer_keeps_only_due_work(self) -> None:
        effects = jobs.reduce_manual_deferrals(
            self.when,
            (
                jobs.DeferralCandidate(
                    "study",
                    models.CleaningOperation.VACUUM,
                    self.when + timedelta(hours=23),
                ),
                jobs.DeferralCandidate(
                    "hall",
                    models.CleaningOperation.VACUUM,
                    self.when + timedelta(hours=25),
                ),
            ),
        )
        self.assertEqual(tuple(item.room_id for item in effects), ("study",))


if __name__ == "__main__":
    unittest.main()
