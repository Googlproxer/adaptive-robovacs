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


def delayed_mop_occurrence(
    completed_at: datetime | None,
    *,
    source=None,
    program=None,
    current_stage: int = 1,
    mop_status=None,
):
    vacuum_profile = models.ResolvedCleaningProfile(
        models.CleaningOperation.VACUUM,
        fan_speed="turbo",
    )
    mop_profile = models.ResolvedCleaningProfile(
        models.CleaningOperation.MOP,
        mop_intensity="deep",
    )
    return state.CleaningOccurrence(
        occurrence_id="occ-delayed",
        room_id="rumpus",
        robot_registry_id="registry-rob",
        robot_entity_id="vacuum.rob",
        program=program or models.CleaningProgram.VACUUM_THEN_MOP,
        stages=[
            state.CleaningStage(
                models.CleaningOperation.VACUUM,
                2,
                status=models.StageStatus.COMPLETED,
                reason="observed",
                started_at=(completed_at - timedelta(minutes=20))
                if completed_at
                else None,
                completed_at=completed_at,
                cleaning_profile=vacuum_profile,
                profile_sources=(("fan_speed", "room"),),
            ),
            state.CleaningStage(
                models.CleaningOperation.MOP,
                3,
                status=mop_status or models.StageStatus.PENDING,
                reason="robot_error_recovery",
                started_at=datetime(2026, 8, 9, 9, 0, tzinfo=UTC),
                completed_at=datetime(2026, 8, 9, 9, 5, tzinfo=UTC),
                cleaning_profile=mop_profile,
                profile_sources=(("mop_intensity", "room"),),
            ),
        ],
        scheduled_at=datetime(2026, 8, 1, 8, 0, tzinfo=UTC),
        created_at=datetime(2026, 8, 1, 7, 55, tzinfo=UTC),
        adapter_id="roborock",
        adapter_schema_version=7,
        current_stage=current_stage,
        source=source or models.OccurrenceSource.SCHEDULER,
        manual_mode="configured" if source else None,
        manual_override=bool(source),
        bypass_desired_window=bool(source),
        manual_context_id="context-1" if source else None,
        manual_user_id="user-1" if source else None,
    )


class JobReducerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.when = datetime(2026, 8, 9, 9, 30, tzinfo=UTC)

    def test_active_rooms_retains_v1_single_room_checkpoints(self) -> None:
        self.assertEqual(jobs.active_rooms(active_job(room_ids=[])), ("study",))
        self.assertEqual(
            jobs.active_rooms(active_job(room_ids=["study", "hall", "study"])),
            ("study", "hall"),
        )

    def test_delayed_mop_stays_eligible_until_exact_freshness_limit(self) -> None:
        completed_at = self.when - timedelta(hours=11, minutes=59, seconds=59)
        occurrence = delayed_mop_occurrence(completed_at)

        self.assertIsNone(jobs.rewind_expired_mop_stage(occurrence, self.when))

        at_limit = completed_at + timedelta(hours=12)
        rewound = jobs.rewind_expired_mop_stage(occurrence, at_limit)
        self.assertIsNotNone(rewound)

    def test_expired_mop_rewinds_same_occurrence_without_losing_contract(self) -> None:
        occurrence = delayed_mop_occurrence(
            self.when - timedelta(days=8),
            source=models.OccurrenceSource.MANUAL_DASHBOARD,
        )

        rewound = jobs.rewind_expired_mop_stage(occurrence, self.when)

        assert rewound is not None
        self.assertEqual(rewound.occurrence_id, occurrence.occurrence_id)
        self.assertEqual(rewound.room_id, occurrence.room_id)
        self.assertEqual(rewound.robot_registry_id, occurrence.robot_registry_id)
        self.assertEqual(rewound.robot_entity_id, occurrence.robot_entity_id)
        self.assertEqual(rewound.scheduled_at, occurrence.scheduled_at)
        self.assertEqual(rewound.created_at, occurrence.created_at)
        self.assertEqual(rewound.adapter_id, occurrence.adapter_id)
        self.assertEqual(
            rewound.adapter_schema_version, occurrence.adapter_schema_version
        )
        self.assertEqual(rewound.source, occurrence.source)
        self.assertEqual(rewound.manual_context_id, occurrence.manual_context_id)
        self.assertEqual(rewound.manual_user_id, occurrence.manual_user_id)
        self.assertEqual(rewound.current_stage, 0)
        self.assertEqual(rewound.stages[0].status, models.StageStatus.PENDING)
        self.assertEqual(rewound.stages[0].reason, "mop_delay_expired")
        self.assertIsNone(rewound.stages[0].started_at)
        self.assertIsNone(rewound.stages[0].completed_at)
        self.assertEqual(rewound.stages[0].passes, 2)
        self.assertIs(
            rewound.stages[0].cleaning_profile,
            occurrence.stages[0].cleaning_profile,
        )
        self.assertEqual(rewound.stages[1].status, models.StageStatus.PENDING)
        self.assertIsNone(rewound.stages[1].reason)
        self.assertIsNone(rewound.stages[1].started_at)
        self.assertIsNone(rewound.stages[1].completed_at)
        self.assertEqual(rewound.stages[1].passes, 3)
        self.assertIs(
            rewound.stages[1].cleaning_profile,
            occurrence.stages[1].cleaning_profile,
        )
        self.assertEqual(occurrence.current_stage, 1)
        self.assertEqual(occurrence.stages[0].status, models.StageStatus.COMPLETED)

    def test_missing_completion_time_is_expired_and_restore_safe(self) -> None:
        occurrence = delayed_mop_occurrence(None)
        restored = state.CleaningOccurrence.from_mapping(occurrence.to_store())
        assert restored is not None

        rewound = jobs.rewind_expired_mop_stage(restored, self.when)

        self.assertIsNotNone(rewound)
        assert rewound is not None
        self.assertIsNone(jobs.rewind_expired_mop_stage(rewound, self.when))

    def test_mop_freshness_guard_excludes_other_program_and_stage_states(self) -> None:
        cases = (
            delayed_mop_occurrence(
                self.when - timedelta(days=1),
                program=models.CleaningProgram.MOP_THEN_VACUUM,
            ),
            delayed_mop_occurrence(
                self.when - timedelta(days=1),
                program=models.CleaningProgram.MOP_ONLY,
            ),
            delayed_mop_occurrence(
                self.when - timedelta(days=1),
                current_stage=0,
            ),
            delayed_mop_occurrence(
                self.when - timedelta(days=1),
                mop_status=models.StageStatus.RUNNING,
            ),
        )
        for occurrence in cases:
            with self.subTest(
                program=occurrence.program,
                current_stage=occurrence.current_stage,
                status=occurrence.stages[1].status,
            ):
                self.assertIsNone(jobs.rewind_expired_mop_stage(occurrence, self.when))

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
