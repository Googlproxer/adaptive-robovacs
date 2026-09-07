"""Public serialization tests for immutable snapshot values."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from custom_components.adaptive_robovacs.models import (
    CleaningOperation,
    CleaningProgram,
    JobPhase,
    JobSource,
    OccurrenceSource,
    RequestedCleaningProfile,
    ResolvedCleaningProfile,
    StageStatus,
)
from custom_components.adaptive_robovacs.presentation import (
    active_job_attributes,
    candidate_attributes,
    cleaning_stage_attributes,
    duration_estimate_attributes,
    effective_profile_attributes,
    eligibility_attributes,
    fault_attributes,
    floor_plan_attributes,
    manual_audit_attributes,
    occurrence_attributes,
    robot_hold_attributes,
    robot_settings_attributes,
    room_decision_attributes,
    scheduler_attributes,
    water_confirmation_attributes,
    water_episode_attributes,
)
from custom_components.adaptive_robovacs.snapshots import (
    ActiveJobView,
    CandidateView,
    CleaningOccurrenceView,
    CleaningStageView,
    DurationEstimateView,
    EffectiveRobotProfileView,
    EffectiveStageProfileView,
    FaultView,
    FloorPlanRoomView,
    FloorPlanSensorView,
    FloorPlanView,
    FloorView,
    FrozenJsonObject,
    ManualAuditView,
    RobotEligibilityView,
    RobotHoldView,
    RobotSettingsView,
    RoomDecisionView,
    SchedulerView,
    WaterConfirmationView,
    WaterNotificationEpisodeView,
)
from custom_components.adaptive_robovacs.state import (
    FloorPlanRectangle,
    FloorPlanSensorMarker,
)

WHEN = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
RESOLVED = ResolvedCleaningProfile(
    operation=CleaningOperation.VACUUM,
    fan_speed="max",
)
REQUESTED = RequestedCleaningProfile(fan_speed="max")


def fault() -> FaultView:
    return FaultView(
        failure_code="start_outcome_uncertain",
        failure_summary="The start could not be confirmed.",
        failure_since=WHEN,
        failure_phase="dispatch",
        robot_name="Alpha",
        room_name="Study",
    )


def stage() -> CleaningStageView:
    return CleaningStageView(
        operation=CleaningOperation.VACUUM,
        passes=2,
        status=StageStatus.RUNNING,
        reason=None,
        started_at=WHEN,
        completed_at=None,
        cleaning_profile=RESOLVED,
        requested_profile=REQUESTED,
        profile_sources=(("fan_speed", "room"),),
    )


def active_job() -> ActiveJobView:
    return ActiveJobView(
        room_id="study",
        room_ids=("study",),
        operation=CleaningOperation.VACUUM,
        phase=JobPhase.CLEANING,
        source=JobSource.SCHEDULER,
        started_at=WHEN,
        seen_cleaning=True,
        expected_minutes=20,
        expected_end=WHEN + timedelta(minutes=20),
        last_observed_at=WHEN,
        passes=2,
        requested_operations=(CleaningOperation.VACUUM,),
        manual_context_id=None,
        accepted_at=WHEN,
        mop_washing_at=None,
        observed_started_at=WHEN,
        recovered_at=None,
        cleaning_finished_at=None,
        completion_confidence=None,
        timer_start=1.5,
        native_timer_elapsed=2.5,
        duration_source="timer",
        measured_minutes=2.5,
        docked_at=None,
        interruption_started_at=None,
        interruption_minutes=0,
        forecast_sample_eligible=True,
        recovery_crossed=False,
        interrupted=False,
        hold_reason=None,
        held_at=None,
        completion_before_hold=False,
        cancelling_at=None,
        adapter_id="roborock",
        adapter_schema_version=2,
        occurrence_id="occurrence-1",
        stage_index=0,
        cleaning_profile=RESOLVED,
        requested_profile=REQUESTED,
        profile_sources=(("fan_speed", "room"),),
        manual_mode=None,
        q10_max_plus_fallback=False,
    )


def floor_plan() -> FloorPlanView:
    return FloorPlanView(
        revision=3,
        floors=(
            FloorView(
                floor_id="ground",
                rooms=(
                    FloorPlanRoomView(
                        area_id="study",
                        name="Study",
                        floor_id="ground",
                        rectangle=FloorPlanRectangle("ground", 1, 2, 8, 6),
                        sensors=(
                            FloorPlanSensorView(
                                registry_id="radar-registry",
                                entity_id="binary_sensor.study",
                                kind="radar",
                                state="active",
                                marker=FloorPlanSensorMarker("study", 500, 250),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        edges=(("hall", "study"),),
        orphaned_rooms=("removed",),
        orphaned_sensors=("old-radar",),
    )


class PresentationTests(unittest.TestCase):
    def test_optional_serializers_return_none_for_absent_values(self) -> None:
        serializers = (
            fault_attributes,
            active_job_attributes,
            robot_hold_attributes,
            occurrence_attributes,
            water_confirmation_attributes,
            water_episode_attributes,
            manual_audit_attributes,
            room_decision_attributes,
            candidate_attributes,
        )
        for serializer in serializers:
            with self.subTest(serializer=serializer.__name__):
                self.assertIsNone(serializer(None))

    def test_fault_active_job_hold_and_stage_keep_public_contract(self) -> None:
        self.assertEqual(
            fault_attributes(fault()),
            {
                "failure_code": "start_outcome_uncertain",
                "failure_summary": "The start could not be confirmed.",
                "failure_since": WHEN.isoformat(),
                "failure_phase": "dispatch",
                "repair_active": True,
                "robot": "Alpha",
                "room": "Study",
            },
        )
        active = active_job_attributes(active_job())
        self.assertEqual(active["operation"], "vacuum")
        self.assertEqual(active["phase"], "cleaning")
        self.assertEqual(active["source"], "scheduler")
        self.assertEqual(active["started"], WHEN.isoformat())
        self.assertEqual(active["cleaning_profile"]["fan_speed"], "max")
        self.assertEqual(active["profile_sources"], {"fan_speed": "room"})
        self.assertEqual(
            robot_hold_attributes(
                RobotHoldView(
                    reason="paused",
                    phase="held",
                    held_at=WHEN,
                    last_observed_at=WHEN,
                    returning_at=None,
                )
            )["reason"],
            "paused",
        )
        serialized_stage = cleaning_stage_attributes(stage())
        self.assertEqual(serialized_stage["status"], "running")
        self.assertEqual(serialized_stage["passes"], 2)

    def test_occurrence_water_and_audits_use_iso_and_plain_containers(self) -> None:
        occurrence = occurrence_attributes(
            CleaningOccurrenceView(
                occurrence_id="occurrence-1",
                room_id="study",
                robot_registry_id="registry-alpha",
                robot_entity_id="vacuum.alpha",
                program=CleaningProgram.VACUUM_THEN_MOP,
                stages=(stage(),),
                scheduled_at=WHEN,
                created_at=WHEN,
                adapter_id="roborock",
                adapter_schema_version=2,
                current_stage=0,
                source=OccurrenceSource.SCHEDULER,
                manual_mode=None,
                manual_override=False,
                bypass_desired_window=False,
                manual_context_id=None,
                manual_user_id=None,
            )
        )
        self.assertEqual(occurrence["program"], "vacuum_then_mop")
        self.assertEqual(occurrence["source"], "scheduler")
        self.assertIsInstance(occurrence["stages"], list)

        confirmation = water_confirmation_attributes(
            WaterConfirmationView("pending", WHEN, WHEN + timedelta(hours=1), None)
        )
        self.assertEqual(confirmation["sent_at"], WHEN.isoformat())
        episode = water_episode_attributes(
            WaterNotificationEpisodeView("study", "confirmation", WHEN, WHEN)
        )
        self.assertEqual(episode["room_id"], "study")

        audit = manual_audit_attributes(
            ManualAuditView(
                at=WHEN,
                robot_entity_id="vacuum.alpha",
                room_ids=("study",),
                operations=("vacuum",),
                context_id="context",
                user_id="user",
                mode="vacuum_only",
                source="dashboard",
                outcome="requested",
                reason=None,
                confidence="confirmed",
                changed=("study",),
                deferred=(),
            )
        )
        self.assertEqual(audit["rooms"], ["study"])
        self.assertEqual(audit["changed"], ["study"])

        decision = room_decision_attributes(
            RoomDecisionView(
                at=WHEN,
                room_area_id="study",
                reason="waiting",
                occupancy_source="radar",
                required_clear_minutes=20,
                clear_minutes=5,
                forecast_confidence=0.75,
                comparable_sample_count=4,
                forecast_reason="insufficient vacancy",
            )
        )
        self.assertEqual(decision["at"], WHEN.isoformat())

    def test_candidate_eligibility_duration_and_profile_are_stable(self) -> None:
        candidate = candidate_attributes(
            CandidateView(
                room_id="study",
                operation=CleaningOperation.VACUUM,
                due_at=WHEN,
                confidence=0.9,
                reason="due",
                duration_minutes=20,
                passes=2,
                manual_override=False,
                source=OccurrenceSource.SCHEDULER,
            )
        )
        self.assertEqual(candidate["operation"], "vacuum")
        self.assertEqual(candidate["source"], "scheduler")
        self.assertEqual(
            eligibility_attributes(
                RobotEligibilityView("vacuum.alpha", "Alpha", True, "ready")
            )["eligible"],
            True,
        )
        self.assertEqual(
            duration_estimate_attributes(
                DurationEstimateView("vacuum.alpha", "Alpha", 18, 22, 4, True)
            )["safe_minutes"],
            22,
        )
        profile = effective_profile_attributes(
            EffectiveRobotProfileView(
                robot_entity_id="vacuum.alpha",
                robot_name="Alpha",
                program=CleaningProgram.VACUUM_ONLY,
                compatible=True,
                stages=(
                    EffectiveStageProfileView(
                        CleaningOperation.VACUUM,
                        2,
                        RESOLVED,
                        REQUESTED,
                        (("fan_speed", "room"),),
                    ),
                ),
            )
        )
        self.assertEqual(profile["program"], "vacuum_only")
        self.assertEqual(profile["stages"][0]["passes"], 2)

    def test_robot_floor_plan_and_scheduler_serialization(self) -> None:
        settings = robot_settings_attributes(
            RobotSettingsView(
                enabled=True,
                minimum_battery=80,
                cleaning_program=CleaningProgram.VACUUM_ONLY,
                double_pass=True,
                mop_double_pass=False,
                mode="vacuum",
                mop_mode=None,
                mop_intensity=None,
                fan_speed="max",
                cleaning_depth="daily",
                cleaning_depth_configured=True,
                direct_custom_mop_migrated=False,
            )
        )
        self.assertTrue(settings["mopping_enabled"] is False)
        plan = floor_plan_attributes(floor_plan())
        self.assertEqual(plan["edges"], [["hall", "study"]])
        self.assertEqual(plan["floors"][0]["rooms"][0]["name"], "Study")
        self.assertEqual(
            plan["floors"][0]["rooms"][0]["sensors"][0]["state"],
            "active",
        )

        scheduler = scheduler_attributes(
            SchedulerView(
                observe_only=True,
                party_mode=False,
                scheduler_halted=True,
                scheduler_limited=True,
                storage_safe_mode=False,
                forecast_confidence=75,
                unresolved_start="00:00",
                unresolved_end="04:00",
                last_evaluation_at=WHEN,
                preview=FrozenJsonObject.from_mapping({"rooms": ["study"]}),
                robot_faults=(fault(),),
                room_faults=(),
                floor_plan=floor_plan(),
                failure=fault(),
            )
        )
        self.assertEqual(scheduler["last_evaluation"], WHEN.isoformat())
        self.assertEqual(scheduler["preview"], {"rooms": ["study"]})
        self.assertEqual(
            scheduler["scheduler_fault"]["failure_code"],
            "start_outcome_uncertain",
        )


if __name__ == "__main__":
    unittest.main()
