"""Pure active-job lifecycle and recovery reducers.

The reducers in this module do not import Home Assistant and never mutate an
application or a supplied state object.  The application applies the returned
effects inside its serialized transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from .models import CleaningOperation, StageStatus, manual_deferral
from .state import (
    ActiveJob,
    CleaningOccurrence,
    CleaningStage,
    DurationSample,
    RobotCooldown,
    SchedulerFault,
)

CANCELLATION_COOLDOWN = timedelta(minutes=15)


def can_refresh_pending_occurrence_profile(
    occurrence: CleaningOccurrence | None,
    stage: CleaningStage | None,
    robot_state: str | None,
    has_active_job: bool,
) -> bool:
    """Allow profile refresh only for an unstarted scheduled docked stage."""

    return bool(
        occurrence
        and occurrence.source == "scheduler"
        and stage
        and stage.status == "pending"
        and stage.started_at is None
        and robot_state == "docked"
        and not has_active_job
    )


def should_assume_native_app_clean(
    robot_state: str | None,
    scheduler_fault: SchedulerFault | None,
    robot_registry_id: str,
    active: ActiveJob | None,
) -> bool:
    """Keep an uncertain physical clean outside scheduler room accounting."""

    return bool(
        robot_state == "cleaning"
        and scheduler_fault
        and scheduler_fault.robot_registry_id == robot_registry_id
        and active
        and active.source in {"scheduler", "manual_dashboard"}
        and not active.seen_cleaning
    )


@dataclass(frozen=True, slots=True)
class ManualAuditEffect:
    """One typed manual-clean audit entry emitted by a reducer."""

    at: datetime
    robot_registry_id: str
    room_ids: tuple[str, ...]
    operations: tuple[CleaningOperation, ...]
    outcome: str
    context_id: str | None = None
    mode: str | None = None
    reason: str | None = None
    confidence: str | None = None
    deferred: tuple[str, ...] = ()
    source: str | None = None


@dataclass(frozen=True, slots=True)
class RecoveryAuditEffect:
    """One typed recovery record emitted by a reducer."""

    robot_registry_id: str
    room_ids: tuple[str, ...]
    at: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class DeferralCandidate:
    """Inputs required for the pure manual-deferral decision."""

    room_id: str
    operation: CleaningOperation
    next_due: datetime


@dataclass(frozen=True, slots=True)
class DeferralEffect:
    """One room operation whose due date should be deferred."""

    room_id: str
    operation: str
    until: datetime


@dataclass(frozen=True, slots=True)
class JobTransition:
    """Typed state changes resulting from cancelling or completing one job."""

    robot_entity_id: str
    robot_registry_id: str
    room_ids: tuple[str, ...]
    room_id: str
    occurrence_id: str | None
    operation: str
    completed_at: datetime | None
    set_room_operation_completion: bool
    set_room_cleaning_completion: bool
    stage_completed: bool
    updated_occurrence: CleaningOccurrence | None
    remove_occurrence: bool
    remove_water_confirmation: bool
    clear_water_notification_episode: bool
    duration_sample: DurationSample | None
    manual_audit: ManualAuditEffect | None
    recovery_audit: RecoveryAuditEffect


def active_rooms(active: ActiveJob) -> tuple[str, ...]:
    """Return unique tracked rooms, retaining schema-one compatibility."""

    values = (*active.room_ids, active.room_id)
    return tuple(dict.fromkeys(value for value in values if value))


def reduce_manual_deferrals(
    completed_at: datetime,
    candidates: tuple[DeferralCandidate, ...],
) -> tuple[DeferralEffect, ...]:
    """Return the narrow one-day manual-clean deferrals that apply."""

    effects: list[DeferralEffect] = []
    for candidate in candidates:
        if candidate.operation not in {"vacuum", "mop"}:
            continue
        until = manual_deferral(completed_at, candidate.next_due)
        if until is not None:
            effects.append(
                DeferralEffect(
                    candidate.room_id,
                    candidate.operation,
                    until,
                )
            )
    return tuple(effects)


def reduce_job_cancellation(
    robot_entity_id: str,
    robot_registry_id: str,
    active: ActiveJob,
    cancelled_at: datetime,
    reason: str,
    occurrence: CleaningOccurrence | None,
) -> JobTransition:
    """Return effects for a physically cancelled tracked job."""

    room_ids = active_rooms(active)
    manual_audit: ManualAuditEffect | None = None
    remove_occurrence = False
    remove_confirmation = False
    updated_occurrence = occurrence
    if active.source == "manual_home_assistant":
        manual_audit = ManualAuditEffect(
            at=cancelled_at,
            robot_registry_id=robot_registry_id,
            room_ids=room_ids,
            operations=tuple(active.requested_operations or [CleaningOperation.VACUUM]),
            context_id=active.manual_context_id,
            outcome="cancelled",
        )
    elif active.source == "manual_dashboard":
        manual_audit = ManualAuditEffect(
            at=cancelled_at,
            robot_registry_id=robot_registry_id,
            room_ids=room_ids,
            operations=(active.operation,),
            context_id=active.manual_context_id,
            mode=active.manual_mode,
            outcome="cancelled",
            reason=reason,
            source="manual_dashboard",
        )
        remove_occurrence = True
        remove_confirmation = True
        updated_occurrence = None
    elif (
        active.source == "scheduler"
        and active.occurrence_id
        and occurrence is not None
        and occurrence.occurrence_id == active.occurrence_id
        and active.stage_index is not None
        and active.stage_index < len(occurrence.stages)
    ):
        stages = list(occurrence.stages)
        stages[active.stage_index] = replace(
            stages[active.stage_index],
            status=StageStatus.PENDING,
            started_at=None,
        )
        updated_occurrence = replace(occurrence, stages=stages)

    return JobTransition(
        robot_entity_id=robot_entity_id,
        robot_registry_id=robot_registry_id,
        room_ids=room_ids,
        room_id=active.room_id,
        occurrence_id=active.occurrence_id,
        operation=active.operation,
        completed_at=None,
        set_room_operation_completion=False,
        set_room_cleaning_completion=False,
        stage_completed=False,
        updated_occurrence=updated_occurrence,
        remove_occurrence=remove_occurrence,
        remove_water_confirmation=remove_confirmation,
        clear_water_notification_episode=False,
        duration_sample=None,
        manual_audit=manual_audit,
        recovery_audit=RecoveryAuditEffect(
            robot_registry_id,
            room_ids,
            cancelled_at,
            reason,
        ),
    )


def interrupted_occurrence(
    occurrence: CleaningOccurrence, stage_index: int
) -> CleaningOccurrence:
    """Retain completed work and reset exactly one abandoned physical attempt."""

    stages = list(occurrence.stages)
    stages[stage_index] = replace(
        stages[stage_index],
        status=StageStatus.PENDING,
        started_at=None,
        completed_at=None,
        reason="robot_error_recovery",
    )
    return replace(occurrence, stages=stages)


def reduce_job_completion(
    robot_entity_id: str,
    robot_registry_id: str,
    active: ActiveJob,
    completion: datetime,
    confidence: str,
    occurrence: CleaningOccurrence | None,
    manual_deferred: tuple[str, ...] = (),
) -> JobTransition:
    """Return effects for one authoritative tracked-job completion."""

    room_ids = active_rooms(active)
    set_operation = active.source != "manual_home_assistant"
    set_cleaning = False
    stage_completed = False
    remove_occurrence = False
    remove_confirmation = False
    clear_water_episode = False
    updated_occurrence = occurrence
    manual_audit: ManualAuditEffect | None = None

    if active.source == "manual_home_assistant":
        manual_audit = ManualAuditEffect(
            at=completion,
            robot_registry_id=robot_registry_id,
            room_ids=room_ids,
            operations=tuple(active.requested_operations or [CleaningOperation.VACUUM]),
            context_id=active.manual_context_id,
            outcome="completed",
            confidence=confidence,
            deferred=manual_deferred,
        )
    elif (
        occurrence is not None
        and occurrence.occurrence_id == active.occurrence_id
        and active.stage_index is not None
        and active.stage_index < len(occurrence.stages)
    ):
        stages = list(occurrence.stages)
        stages[active.stage_index] = replace(
            stages[active.stage_index],
            status=StageStatus.COMPLETED,
            reason=confidence,
            completed_at=completion,
        )
        next_stage = active.stage_index + 1
        occurrence_complete = next_stage >= len(stages)
        updated_occurrence = replace(
            occurrence,
            stages=stages,
            current_stage=next_stage,
        )
        stage_completed = True
        set_cleaning = occurrence_complete
        remove_occurrence = occurrence_complete
        remove_confirmation = occurrence_complete
        clear_water_episode = occurrence_complete and active.operation == "mop"
        if active.source == "manual_dashboard":
            manual_audit = ManualAuditEffect(
                at=completion,
                robot_registry_id=robot_registry_id,
                room_ids=(active.room_id,),
                operations=(active.operation,),
                context_id=active.manual_context_id,
                mode=active.manual_mode,
                outcome=("completed" if occurrence_complete else "stage_completed"),
                confidence=confidence,
                source="manual_dashboard",
            )
    else:
        set_cleaning = True

    measured = active.measured_minutes
    duration_sample = (
        DurationSample(
            minutes=float(measured),
            operation=active.operation,
            passes=active.passes,
            robot_registry_id=robot_registry_id,
            source=active.duration_source or "state_transition",
            recorded_at=completion,
            measurement_version=2,
        )
        if active.source in {"scheduler", "manual_dashboard"}
        and confidence == "observed"
        and active.forecast_sample_eligible
        and not active.recovery_crossed
        and active.duration_source == "elapsed_total_v2"
        and isinstance(measured, (float, int))
        and measured > 0
        else None
    )

    return JobTransition(
        robot_entity_id=robot_entity_id,
        robot_registry_id=robot_registry_id,
        room_ids=room_ids,
        room_id=active.room_id,
        occurrence_id=active.occurrence_id,
        operation=active.operation,
        completed_at=completion,
        set_room_operation_completion=set_operation,
        set_room_cleaning_completion=set_cleaning,
        stage_completed=stage_completed,
        updated_occurrence=updated_occurrence,
        remove_occurrence=remove_occurrence,
        remove_water_confirmation=remove_confirmation,
        clear_water_notification_episode=clear_water_episode,
        duration_sample=duration_sample,
        manual_audit=manual_audit,
        recovery_audit=RecoveryAuditEffect(
            robot_registry_id,
            room_ids,
            completion,
            confidence,
        ),
    )


def cancellation_cooldown(
    robot_known: bool,
    cancelled_at: datetime,
) -> RobotCooldown | None:
    """Return the cancelled robot's cooldown without changing room cadence."""

    if not robot_known:
        return None
    return RobotCooldown(
        until=cancelled_at + CANCELLATION_COOLDOWN,
        cancelled_at=cancelled_at,
        reason="physical_cancelled",
    )
