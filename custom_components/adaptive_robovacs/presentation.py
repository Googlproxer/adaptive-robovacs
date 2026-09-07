"""Home Assistant serialization for immutable integration snapshots."""

from __future__ import annotations

from datetime import datetime

from .models import RequestedCleaningProfile, ResolvedCleaningProfile
from .snapshots import (
    ActiveJobView,
    CandidateView,
    CleaningOccurrenceView,
    CleaningStageView,
    DurationEstimateView,
    EffectiveRobotProfileView,
    FaultView,
    FloorPlanView,
    ManualAuditView,
    RobotEligibilityView,
    RobotHoldView,
    RobotSettingsView,
    RoomDecisionView,
    RoomRecoveryView,
    SchedulerView,
    WaterConfirmationView,
    WaterNotificationEpisodeView,
    thaw_json,
)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _resolved_profile(
    profile: ResolvedCleaningProfile | None,
) -> dict[str, str | None]:
    return profile.to_mapping() if profile else {}


def _requested_profile(
    profile: RequestedCleaningProfile | None,
) -> dict[str, str | None]:
    return profile.to_mapping() if profile else {}


def fault_attributes(fault: FaultView | None) -> dict[str, object] | None:
    if fault is None:
        return None
    return {
        "failure_code": fault.failure_code,
        "failure_summary": fault.failure_summary,
        "failure_since": fault.failure_since.isoformat(),
        "failure_phase": fault.failure_phase,
        "repair_active": True,
        "robot": fault.robot_name,
        "room": fault.room_name,
    }


def room_recovery_attributes(
    recovery: RoomRecoveryView | None,
) -> dict[str, object] | None:
    if recovery is None:
        return None
    return {
        "recovery_id": recovery.recovery_id,
        "occurrence_id": recovery.occurrence_id,
        "stage_index": recovery.stage_index,
        "operation": str(recovery.operation),
        "detached_at": _iso(recovery.detached_at),
        "failure": fault_attributes(recovery.failure),
    }


def active_job_attributes(active: ActiveJobView | None) -> dict[str, object] | None:
    if active is None:
        return None
    return {
        "room": active.room_id,
        "rooms": list(active.room_ids),
        "operation": active.operation.value,
        "phase": active.phase.value,
        "source": active.source.value,
        "started": _iso(active.started_at),
        "seen_cleaning": active.seen_cleaning,
        "expected_minutes": active.expected_minutes,
        "expected_end": _iso(active.expected_end),
        "last_observed_at": _iso(active.last_observed_at),
        "passes": active.passes,
        "requested_operations": [item.value for item in active.requested_operations],
        "manual_context_id": active.manual_context_id,
        "accepted_at": _iso(active.accepted_at),
        "mop_washing_at": _iso(active.mop_washing_at),
        "observed_started": _iso(active.observed_started_at),
        "recovered_at": _iso(active.recovered_at),
        "cleaning_finished": _iso(active.cleaning_finished_at),
        "completion_confidence": active.completion_confidence,
        "timer_start": active.timer_start,
        "native_timer_elapsed": active.native_timer_elapsed,
        "duration_source": active.duration_source,
        "measured_minutes": active.measured_minutes,
        "docked_at": _iso(active.docked_at),
        "interruption_started_at": _iso(active.interruption_started_at),
        "interruption_minutes": active.interruption_minutes,
        "forecast_sample_eligible": active.forecast_sample_eligible,
        "recovery_crossed": active.recovery_crossed,
        "interrupted": active.interrupted,
        "hold_reason": active.hold_reason,
        "held_at": _iso(active.held_at),
        "completion_before_hold": active.completion_before_hold,
        "cancelling_at": _iso(active.cancelling_at),
        "adapter_id": active.adapter_id,
        "adapter_schema_version": active.adapter_schema_version,
        "occurrence_id": active.occurrence_id,
        "stage_index": active.stage_index,
        "cleaning_profile": _resolved_profile(active.cleaning_profile),
        "requested_profile": _requested_profile(active.requested_profile),
        "profile_sources": dict(active.profile_sources),
        "manual_mode": active.manual_mode,
        "q10_max_plus_fallback": active.q10_max_plus_fallback,
    }


def robot_hold_attributes(hold: RobotHoldView | None) -> dict[str, object] | None:
    if hold is None:
        return None
    return {
        "reason": hold.reason,
        "phase": hold.phase,
        "held_at": _iso(hold.held_at),
        "last_observed_at": _iso(hold.last_observed_at),
        "returning_at": _iso(hold.returning_at),
        "requested_map_id": hold.requested_map_id,
    }


def cleaning_stage_attributes(stage: CleaningStageView) -> dict[str, object]:
    return {
        "operation": stage.operation.value,
        "passes": stage.passes,
        "status": stage.status.value,
        "reason": stage.reason,
        "started_at": _iso(stage.started_at),
        "completed_at": _iso(stage.completed_at),
        "cleaning_profile": _resolved_profile(stage.cleaning_profile),
        "requested_profile": _requested_profile(stage.requested_profile),
        "profile_sources": dict(stage.profile_sources),
    }


def occurrence_attributes(
    occurrence: CleaningOccurrenceView | None,
) -> dict[str, object] | None:
    if occurrence is None:
        return None
    return {
        "occurrence_id": occurrence.occurrence_id,
        "room_id": occurrence.room_id,
        "robot_registry_id": occurrence.robot_registry_id,
        "robot_entity_id": occurrence.robot_entity_id,
        "program": occurrence.program.value,
        "stages": [cleaning_stage_attributes(stage) for stage in occurrence.stages],
        "scheduled_at": occurrence.scheduled_at.isoformat(),
        "created_at": occurrence.created_at.isoformat(),
        "adapter_id": occurrence.adapter_id,
        "adapter_schema_version": occurrence.adapter_schema_version,
        "current_stage": occurrence.current_stage,
        "source": occurrence.source.value,
        "manual_mode": occurrence.manual_mode,
        "manual_override": occurrence.manual_override,
        "bypass_desired_window": occurrence.bypass_desired_window,
        "manual_context_id": occurrence.manual_context_id,
        "manual_user_id": occurrence.manual_user_id,
    }


def water_confirmation_attributes(
    confirmation: WaterConfirmationView | None,
) -> dict[str, object] | None:
    if confirmation is None:
        return None
    return {
        "status": confirmation.status,
        "sent_at": confirmation.sent_at.isoformat(),
        "expires_at": confirmation.expires_at.isoformat(),
        "responded_at": _iso(confirmation.responded_at),
    }


def water_episode_attributes(
    episode: WaterNotificationEpisodeView | None,
) -> dict[str, object] | None:
    if episode is None:
        return None
    return {
        "room_id": episode.room_id,
        "reason": episode.reason,
        "first_sent_at": episode.first_sent_at.isoformat(),
        "last_sent_at": episode.last_sent_at.isoformat(),
    }


def manual_audit_attributes(
    event: ManualAuditView | None,
) -> dict[str, object] | None:
    if event is None:
        return None
    return {
        "at": _iso(event.at),
        "robot": event.robot_entity_id,
        "rooms": list(event.room_ids),
        "operations": list(event.operations),
        "context_id": event.context_id,
        "user_id": event.user_id,
        "mode": event.mode,
        "source": event.source,
        "outcome": event.outcome,
        "reason": event.reason,
        "confidence": event.confidence,
        "changed": list(event.changed),
        "deferred": list(event.deferred),
    }


def room_decision_attributes(
    decision: RoomDecisionView | None,
) -> dict[str, object] | None:
    if decision is None:
        return None
    return {
        "at": _iso(decision.at),
        "room_area_id": decision.room_area_id,
        "reason": decision.reason,
        "occupancy_source": decision.occupancy_source,
        "required_clear_minutes": decision.required_clear_minutes,
        "clear_minutes": decision.clear_minutes,
        "forecast_confidence": decision.forecast_confidence,
        "comparable_sample_count": decision.comparable_sample_count,
        "forecast_reason": decision.forecast_reason,
    }


def candidate_attributes(candidate: CandidateView | None) -> dict[str, object] | None:
    if candidate is None:
        return None
    return {
        "room": candidate.room_id,
        "operation": candidate.operation.value,
        "due_at": candidate.due_at,
        "confidence": candidate.confidence,
        "reason": candidate.reason,
        "duration_minutes": candidate.duration_minutes,
        "passes": candidate.passes,
        "manual_override": candidate.manual_override,
        "source": candidate.source.value,
    }


def eligibility_attributes(item: RobotEligibilityView) -> dict[str, object]:
    return {
        "robot_entity_id": item.robot_entity_id,
        "robot_name": item.robot_name,
        "eligible": item.eligible,
        "reason": item.reason,
    }


def duration_estimate_attributes(item: DurationEstimateView) -> dict[str, object]:
    return {
        "robot_entity_id": item.robot_entity_id,
        "robot_name": item.robot_name,
        "typical_minutes": item.typical_minutes,
        "safe_minutes": item.safe_minutes,
        "sample_count": item.sample_count,
        "learned": item.learned,
    }


def effective_profile_attributes(
    profile: EffectiveRobotProfileView,
) -> dict[str, object]:
    return {
        "robot_entity_id": profile.robot_entity_id,
        "robot_name": profile.robot_name,
        "program": profile.program.value if profile.program else None,
        "compatible": profile.compatible,
        "stages": [
            {
                "operation": stage.operation.value,
                "passes": stage.passes,
                "cleaning_profile": stage.cleaning_profile.to_mapping(),
                "requested_profile": stage.requested_profile.to_mapping(),
                "profile_sources": dict(stage.profile_sources),
            }
            for stage in profile.stages
        ],
    }


def robot_settings_attributes(settings: RobotSettingsView) -> dict[str, object]:
    return {
        "enabled": settings.enabled,
        "minimum_battery": settings.minimum_battery,
        "cleaning_program": settings.cleaning_program.value,
        "double_pass": settings.double_pass,
        "mop_double_pass": settings.mop_double_pass,
        "mode": settings.mode,
        "mop_mode": settings.mop_mode,
        "mop_intensity": settings.mop_intensity,
        "fan_speed": settings.fan_speed,
        "cleaning_depth": settings.cleaning_depth,
        "cleaning_depth_configured": settings.cleaning_depth_configured,
        "direct_custom_mop_migrated": settings.direct_custom_mop_migrated,
        "mopping_enabled": settings.mopping_enabled,
    }


def floor_plan_attributes(plan: FloorPlanView) -> dict[str, object]:
    return {
        "revision": plan.revision,
        "floors": [
            {
                "floor_id": floor.floor_id,
                "rooms": [
                    {
                        "area_id": room.area_id,
                        "name": room.name,
                        "floor_id": room.floor_id,
                        "rectangle": (
                            room.rectangle.to_store() if room.rectangle else None
                        ),
                        "sensors": [
                            {
                                "registry_id": sensor.registry_id,
                                "entity_id": sensor.entity_id,
                                "kind": sensor.kind,
                                "state": sensor.state,
                                "marker": (
                                    sensor.marker.to_store() if sensor.marker else None
                                ),
                            }
                            for sensor in room.sensors
                        ],
                    }
                    for room in floor.rooms
                ],
            }
            for floor in plan.floors
        ],
        "edges": [list(edge) for edge in plan.edges],
        "orphaned_rooms": list(plan.orphaned_rooms),
        "orphaned_sensors": list(plan.orphaned_sensors),
    }


def scheduler_attributes(scheduler: SchedulerView) -> dict[str, object]:
    singular = fault_attributes(scheduler.failure)
    return {
        "last_evaluation": _iso(scheduler.last_evaluation_at),
        "preview": thaw_json(scheduler.preview),
        "scheduler_fault": singular,
        "robot_faults": [fault_attributes(item) for item in scheduler.robot_faults],
        "room_faults": [fault_attributes(item) for item in scheduler.room_faults],
        "room_recoveries": [
            room_recovery_attributes(item) for item in scheduler.room_recoveries
        ],
        "floor_plan": floor_plan_attributes(scheduler.floor_plan),
    }
