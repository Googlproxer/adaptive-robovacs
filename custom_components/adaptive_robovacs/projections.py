"""Build immutable presentation snapshots from settled scheduler state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Protocol

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .discovery import DiscoveredRobot, DiscoveredRoom, DiscoverySnapshot
from .models import (
    AdjacencyDecision,
    CleaningOperation,
    DurationEstimate,
    Forecast,
    OccurrenceSource,
    ResolvedDailyWindow,
    cleaning_profile_sources,
    effective_cleaning_program,
    effective_scheduled_due,
    expand_cleaning_program,
    format_last_cleaned_age,
    learned_battery_rate,
    next_clean_schedule,
    next_usable_window_start,
    next_window_start,
    requested_cleaning_profile,
    resolve_cleaning_profile,
    stage_pass_count,
)
from .opportunities import missed_cadences, opportunity_advice
from .planner import (
    CandidateRobotDecision,
    RoomEvaluation,
    ScheduleCandidate,
    VacancyDiagnostic,
)
from .presentation import adjacency_block_reason
from .repairs_manager import fault_summary, room_recovery_summary
from .room_status import room_robot_previews
from .snapshots import (
    ActiveJobView,
    CandidateView,
    CleaningOccurrenceView,
    CleaningRunView,
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
    IntegrationSnapshot,
    ManualAuditView,
    ObservedProfileView,
    OccupancyQualityView,
    OpportunityView,
    OpportunityWindowView,
    RobotEligibilityView,
    RobotHoldView,
    RobotSettingsView,
    RobotView,
    RoomAdjacencyBlockerView,
    RoomDecisionView,
    RoomRecoveryView,
    RoomView,
    SchedulerView,
    WaterConfirmationView,
    WaterNotificationEpisodeView,
)
from .state import (
    ActiveJob,
    CleaningOccurrence,
    CleaningStage,
    ManualAuditRecord,
    RobotHold,
    RobotSettings,
    RoomDecisionRecord,
    RoomHistory,
    RoomRecovery,
    RoomSettings,
    SchedulerFault,
    SchedulerState,
)


class ProjectionSource(Protocol):
    """Narrow read interface used by the presentation projection."""

    hass: HomeAssistant
    discovery: DiscoverySnapshot
    state: SchedulerState
    _room_evaluations: dict[str, RoomEvaluation]

    @property
    def observe_only(self) -> bool: ...

    @property
    def party_mode(self) -> bool: ...

    @property
    def scheduler_halted(self) -> bool: ...

    @property
    def scheduler_limited(self) -> bool: ...

    @property
    def storage_safe_mode(self) -> bool: ...

    def get_global_setting(self, key: str) -> object: ...

    def _room_data(self, area_id: str) -> RoomHistory: ...

    def _room_settings(self, room: DiscoveredRoom) -> RoomSettings: ...

    def _robot_settings(self, robot: DiscoveredRobot) -> RobotSettings: ...

    def _desired_window(self, room: DiscoveredRoom) -> ResolvedDailyWindow: ...

    def _adjacency_decision(
        self,
        room: DiscoveredRoom,
        now: datetime,
        duration_minutes: float | None = None,
    ) -> AdjacencyDecision: ...

    def _room_due(
        self,
        room: DiscoveredRoom,
        operation: str,
        now: datetime,
    ) -> datetime: ...

    def _room_deferral(
        self, room: DiscoveredRoom, operation: str
    ) -> datetime | None: ...

    def _room_candidate(
        self,
        room: DiscoveredRoom,
        now: datetime,
    ) -> tuple[ScheduleCandidate | None, str]: ...

    def _candidate_robot_diagnostics(
        self,
        candidate: ScheduleCandidate,
        readiness: Mapping[str, tuple[bool, str]] | None = None,
    ) -> tuple[CandidateRobotDecision, ...]: ...

    def _active_rooms(self, active: ActiveJob) -> list[str]: ...

    def _duration_estimate(
        self,
        room: DiscoveredRoom,
        operation: str,
        passes: int,
        robot_id: str | None = None,
    ) -> DurationEstimate: ...

    def _vacancy_diagnostic(
        self,
        room: DiscoveredRoom,
        now: datetime,
        duration_minutes: float,
    ) -> VacancyDiagnostic: ...

    def robot_for_registry_id(self, registry_id: str) -> DiscoveredRobot | None: ...

    def robot_unique_fragment(self, entity_id: str) -> str: ...

    def room_cleaning_period(self, area_id: str) -> str: ...

    def room_cleaning_profile(self, area_id: str) -> str: ...

    def _robot_ready(self, robot: DiscoveredRobot) -> tuple[bool, str]: ...

    def _robot_battery(self, robot: DiscoveredRobot) -> float | None: ...


def _now() -> datetime:
    return datetime.now(UTC)


def _active_job_view(active: ActiveJob | None) -> ActiveJobView | None:
    if active is None:
        return None
    return ActiveJobView(
        room_id=active.room_id,
        room_ids=tuple(active.room_ids),
        operation=active.operation,
        phase=active.phase,
        source=active.source,
        started_at=active.started_at,
        seen_cleaning=active.seen_cleaning,
        expected_minutes=active.expected_minutes,
        expected_end=active.expected_end,
        last_observed_at=active.last_observed_at,
        passes=active.passes,
        requested_operations=tuple(active.requested_operations),
        manual_context_id=active.manual_context_id,
        accepted_at=active.accepted_at,
        mop_washing_at=active.mop_washing_at,
        observed_started_at=active.observed_started_at,
        recovered_at=active.recovered_at,
        cleaning_finished_at=active.cleaning_finished_at,
        completion_confidence=active.completion_confidence,
        timer_start=active.timer_start,
        native_timer_elapsed=active.native_timer_elapsed,
        duration_source=active.duration_source,
        measured_minutes=active.measured_minutes,
        docked_at=active.docked_at,
        interruption_started_at=active.interruption_started_at,
        interruption_minutes=active.interruption_minutes,
        forecast_sample_eligible=active.forecast_sample_eligible,
        recovery_crossed=active.recovery_crossed,
        interrupted=active.interrupted,
        hold_reason=active.hold_reason,
        held_at=active.held_at,
        completion_before_hold=active.completion_before_hold,
        cancelling_at=active.cancelling_at,
        adapter_id=active.adapter_id,
        adapter_schema_version=active.adapter_schema_version,
        occurrence_id=active.occurrence_id,
        stage_index=active.stage_index,
        cleaning_profile=active.cleaning_profile,
        requested_profile=active.requested_profile,
        profile_sources=active.profile_sources,
        manual_mode=active.manual_mode,
        q10_max_plus_fallback=active.q10_max_plus_fallback,
    )


def _hold_view(hold: RobotHold | None) -> RobotHoldView | None:
    if hold is None:
        return None
    return RobotHoldView(
        reason=hold.reason,
        phase=hold.phase,
        held_at=hold.held_at,
        last_observed_at=hold.last_observed_at,
        returning_at=hold.returning_at,
    )


def _stage_view(stage: CleaningStage) -> CleaningStageView:
    return CleaningStageView(
        operation=stage.operation,
        passes=stage.passes,
        status=stage.status,
        reason=stage.reason,
        started_at=stage.started_at,
        completed_at=stage.completed_at,
        cleaning_profile=stage.cleaning_profile,
        requested_profile=stage.requested_profile,
        profile_sources=stage.profile_sources,
    )


def _occurrence_view(
    source: ProjectionSource,
    occurrence: CleaningOccurrence | None,
) -> CleaningOccurrenceView | None:
    if occurrence is None:
        return None
    robot = source.robot_for_registry_id(occurrence.robot_registry_id)
    return CleaningOccurrenceView(
        occurrence_id=occurrence.occurrence_id,
        room_id=occurrence.room_id,
        robot_registry_id=occurrence.robot_registry_id,
        robot_entity_id=robot.entity_id if robot else None,
        program=occurrence.program,
        stages=tuple(_stage_view(stage) for stage in occurrence.stages),
        scheduled_at=occurrence.scheduled_at,
        created_at=occurrence.created_at,
        adapter_id=occurrence.adapter_id,
        adapter_schema_version=occurrence.adapter_schema_version,
        current_stage=occurrence.current_stage,
        source=occurrence.source,
        manual_mode=occurrence.manual_mode,
        manual_override=occurrence.manual_override,
        bypass_desired_window=occurrence.bypass_desired_window,
        manual_context_id=occurrence.manual_context_id,
        manual_user_id=occurrence.manual_user_id,
        mop_delay_expirations=occurrence.mop_delay_expirations,
    )


def _manual_audit_view(
    source: ProjectionSource,
    record: ManualAuditRecord | None,
) -> ManualAuditView | None:
    if record is None:
        return None
    robot = (
        source.robot_for_registry_id(record.robot_registry_id)
        if record.robot_registry_id
        else None
    )
    return ManualAuditView(
        at=record.at,
        robot_entity_id=robot.entity_id if robot else None,
        room_ids=record.room_ids,
        operations=record.operations,
        context_id=record.context_id,
        user_id=record.user_id,
        mode=record.mode,
        source=record.source,
        outcome=record.outcome,
        reason=record.reason,
        confidence=record.confidence,
        changed=record.changed,
        deferred=record.deferred,
    )


def _room_decision_view(record: RoomDecisionRecord | None) -> RoomDecisionView | None:
    if record is None:
        return None
    return RoomDecisionView(
        at=record.at,
        room_area_id=record.room_area_id,
        reason=record.reason,
        occupancy_source=record.occupancy_source,
        required_clear_minutes=record.required_clear_minutes,
        clear_minutes=record.clear_minutes,
        forecast_confidence=record.forecast_confidence,
        comparable_sample_count=record.comparable_sample_count,
        forecast_reason=record.forecast_reason,
        evidence=FrozenJsonObject.from_mapping(record.evidence.to_mapping()),
    )


def _candidate_view(candidate: ScheduleCandidate) -> CandidateView:
    return CandidateView(
        room_id=candidate.room_id,
        operation=CleaningOperation(candidate.operation),
        due_at=candidate.due_at,
        confidence=candidate.confidence,
        reason=candidate.reason,
        duration_minutes=candidate.duration_minutes,
        passes=candidate.passes,
        manual_override=candidate.manual_override,
        source=OccurrenceSource(candidate.source),
    )


def _fault_view(source: ProjectionSource, fault: SchedulerFault) -> FaultView:
    robot = source.robot_for_registry_id(fault.robot_registry_id)
    room = source.discovery.rooms.get(fault.room_area_id)
    return FaultView(
        failure_code=fault.reason_code,
        failure_summary=fault_summary(fault.reason_code),
        failure_since=fault.occurred_at,
        failure_phase=fault.phase,
        robot_name=robot.name if robot else None,
        room_name=room.name if room else None,
        retry_attempts=fault.retry_attempts,
        retry_after=fault.retry_after,
    )


def room_recovery_view(
    source: ProjectionSource, recovery: RoomRecovery
) -> RoomRecoveryView:
    robot = source.robot_for_registry_id(recovery.robot_registry_id)
    room = source.discovery.rooms.get(recovery.room_area_id)
    return RoomRecoveryView(
        recovery.recovery_id,
        recovery.occurrence_id,
        recovery.stage_index,
        recovery.operation,
        recovery.detached_at,
        FaultView(
            "room_error_recovery",
            room_recovery_summary(recovery.error_category),
            recovery.interrupted_at,
            "awaiting_confirmation" if recovery.detached_at else "awaiting_safe_dock",
            robot.name if robot else None,
            room.name if room else None,
        ),
    )


def _occupancy_quality_view(
    history: RoomHistory, now: datetime
) -> OccupancyQualityView:
    transitions = [
        item
        for item in history.occupancy_transitions
        if item.at >= now - timedelta(hours=24)
    ]
    spells = [
        item.vacancy_seconds for item in transitions if item.vacancy_seconds is not None
    ]
    return OccupancyQualityView(
        len(transitions),
        len(spells),
        sum(value < 60 for value in spells),
        median(spells) if spells else None,
        len(history.occupancy_samples),
        len(transitions) >= 4096,
    )


def _opportunity_view(
    history: RoomHistory,
    due: datetime | None,
    settings: RoomSettings,
    now: datetime,
    occurrence: CleaningOccurrence | None,
) -> OpportunityView:
    record = history.opportunity
    cause, advice = opportunity_advice(
        record, repeated_mop=bool(occurrence and occurrence.mop_delay_expirations >= 2)
    )
    return OpportunityView(
        missed_cadences(now, due, settings.cleaning_interval)
        if due and settings.enabled
        else 0,
        record.observed_window,
        record.totals,
        tuple(
            OpportunityWindowView(item.started_at, item.ended_at, item.minutes)
            for item in record.windows
        ),
        cause,
        advice,
        record.alert_signature is not None,
    )


def _adjacency_blocker_views(
    source: ProjectionSource, decision: AdjacencyDecision | None
) -> tuple[RoomAdjacencyBlockerView, ...]:
    if decision is None:
        return ()
    return tuple(
        RoomAdjacencyBlockerView(
            item.area_id,
            source.discovery.rooms[item.area_id].name,
            item.occupancy,
            VacancyDiagnostic.from_forecast(
                source._room_data(item.area_id).occupancy_source,
                source._room_data(item.area_id).unoccupied_since,
                item.vacancy,
            )
            if item.vacancy
            else None,
        )
        for item in decision.blockers
    )


def room_view(source: ProjectionSource, area_id: str) -> RoomView:
    """Build typed, immutable state for one discovered area."""

    room = source.discovery.rooms[area_id]
    detail = source._room_data(area_id)
    settings = source._room_settings(room)
    now = _now()
    desired_window = source._desired_window(room)
    local_now = dt_util.as_local(now)
    desired_window_start = (
        next_usable_window_start(local_now, desired_window.start, desired_window.end)
        if desired_window.valid
        else next_window_start(local_now, desired_window.start)
    )
    next_due = source._room_due(room, "cleaning", now)
    evaluation = getattr(source, "_room_evaluations", {}).get(area_id)
    if evaluation is None:
        # Initial publication can precede the first settled transaction.
        candidate, reason = source._room_candidate(room, now)
        diagnostics = (
            source._candidate_robot_diagnostics(candidate) if candidate else ()
        )
        evaluation = RoomEvaluation(now, candidate, reason, diagnostics)
    candidate, reason, diagnostics = (
        evaluation.candidate,
        evaluation.reason,
        evaluation.robots,
    )
    chosen = evaluation.display_decision
    evidence = chosen.evidence if chosen else None
    eligibility = tuple(
        RobotEligibilityView(
            robot_entity_id=item.eligibility.robot_id,
            robot_name=item.eligibility.robot_name,
            eligible=item.eligibility.eligible,
            reason=item.eligibility.reason,
            operation=item.evidence.operation if item.evidence else None,
            program=item.evidence.program if item.evidence else None,
            stage_index=item.evidence.stage_index if item.evidence else None,
            passes=item.evidence.passes if item.evidence else None,
            duration_minutes=item.evidence.duration_minutes if item.evidence else None,
            vacancy_diagnostic=item.evidence.vacancy_diagnostic
            if item.evidence
            else None,
            adjacency_blockers=_adjacency_blocker_views(source, item.adjacency),
        )
        for item in diagnostics
    )
    assignment_available = any(
        item.eligibility.eligible and item.candidate is not None for item in diagnostics
    )
    if candidate and not assignment_available:
        reason = next(
            (
                item.eligibility.reason
                for item in diagnostics
                if item.eligibility.reason
            ),
            "no ready compatible robot",
        )

    active_registry_id, active = next(
        (
            (registry_id, job)
            for registry_id, job in source.state.active_jobs.items()
            if job and area_id in source._active_rooms(job)
        ),
        (None, None),
    )
    active_robot = (
        source.robot_for_registry_id(active_registry_id) if active_registry_id else None
    )
    active_robot_id = active_robot.entity_id if active_robot else None
    active_robot_state_object = (
        source.hass.states.get(active_robot_id) if active_robot_id else None
    )
    duration_operation = (
        active.operation if active else evidence.operation if evidence else "vacuum"
    )
    duration_passes = active.passes if active else evidence.passes if evidence else 1
    diagnostic_robot = (
        source.discovery.robots.get(chosen.eligibility.robot_id) if chosen else None
    )
    duration_estimate = source._duration_estimate(
        room,
        duration_operation,
        duration_passes,
        active_registry_id
        or (diagnostic_robot.registry_id if diagnostic_robot else None),
    )
    adjacency = (chosen.adjacency if chosen else None) or source._adjacency_decision(
        room, now
    )
    adjacency_reason = adjacency_block_reason(
        adjacency, {key: item.name for key, item in source.discovery.rooms.items()}
    )
    duration_estimates = []
    for robot in source.discovery.robots.values():
        if robot.floor_id != room.floor_id or not robot.adapter_capabilities.supports(
            duration_operation,
            duration_passes,
        ):
            continue
        estimate = source._duration_estimate(
            room,
            duration_operation,
            duration_passes,
            robot.registry_id,
        )
        duration_estimates.append(
            DurationEstimateView(
                robot_entity_id=robot.entity_id,
                robot_name=robot.name,
                typical_minutes=estimate.typical_minutes,
                safe_minutes=estimate.safe_minutes,
                sample_count=estimate.sample_count,
                learned=estimate.learned,
            )
        )

    latest_decision = next(
        (
            item
            for item in reversed(source.state.audit.room_decisions)
            if item.room_area_id == area_id
        ),
        None,
    )
    latest_manual = next(
        (
            item
            for item in reversed(source.state.audit.manual_events)
            if item.source == "manual_dashboard" and area_id in item.room_ids
        ),
        None,
    )
    occurrence = source.state.occurrences.get(area_id)
    confirmation = (
        source.state.water_confirmations.get(occurrence.occurrence_id)
        if occurrence
        else None
    )
    episode = source.state.water_notification_episodes.get(area_id)

    effective_profiles = []
    for robot in source.discovery.robots.values():
        if robot.floor_id != room.floor_id:
            continue
        robot_settings = source._robot_settings(robot)
        program = effective_cleaning_program(
            settings.cleaning_program,
            robot_settings.cleaning_program,
        )
        stages = []
        compatible = bool(expand_cleaning_program(program or ""))
        for operation in expand_cleaning_program(program or ""):
            passes = stage_pass_count(
                operation,
                settings.vacuum_pass_count,
                settings.mop_pass_count,
                robot_settings.double_pass,
                robot_settings.mop_double_pass,
                robot.adapter_capabilities,
            )
            profile = resolve_cleaning_profile(
                operation,
                settings,
                robot_settings,
                robot.adapter_capabilities,
            )
            if passes is None or profile is None:
                compatible = False
                break
            stages.append(
                EffectiveStageProfileView(
                    operation=CleaningOperation(operation),
                    passes=passes,
                    cleaning_profile=profile,
                    requested_profile=requested_cleaning_profile(
                        settings,
                        robot_settings,
                    ),
                    profile_sources=cleaning_profile_sources(settings),
                )
            )
        effective_profiles.append(
            EffectiveRobotProfileView(
                robot_entity_id=robot.entity_id,
                robot_name=robot.name,
                program=program,
                compatible=compatible,
                stages=tuple(stages),
            )
        )

    cleaning_deferral = detail.deferrals.get("cleaning")
    vacancy = (
        evidence.vacancy_diagnostic if evidence else None
    ) or VacancyDiagnostic.from_forecast(
        detail.occupancy_source,
        detail.unoccupied_since,
        Forecast(False, 0.0, "not evaluated", decision_source="not_evaluated"),
    )
    room_fault = source.state.room_faults.get(room.area_id)
    schedule_due = effective_scheduled_due(
        next_due
        if detail.cleaning_completed_at or source.state.first_scheduler_online_at
        else None,
        occurrence.scheduled_at if occurrence else None,
        source._room_deferral(room, "cleaning"),
        manual_request=bool(occurrence and occurrence.source == "manual_dashboard"),
    )
    bypass_window = settings.ignore_desired_window or bool(
        occurrence and (occurrence.bypass_desired_window or occurrence.manual_override)
    )
    next_clean_at, window_end = next_clean_schedule(
        schedule_due,
        local_now,
        desired_window.start,
        desired_window.end,
        enabled=settings.enabled
        and not (source.observe_only or source.party_mode or source.storage_safe_mode),
        active=active is not None,
        bypass_window=bypass_window,
    )
    return RoomView(
        cleaning_run_id=next(
            (
                run.run_id
                for run in source.state.cleaning_runs.values()
                if area_id in run.rooms
            ),
            None,
        ),
        queue_position=next(
            (
                run.rooms.index(area_id) + 1
                for run in source.state.cleaning_runs.values()
                if area_id in run.rooms
            ),
            None,
        ),
        queue_current_room=any(
            run.started_room_id == area_id
            for run in source.state.cleaning_runs.values()
        ),
        area_id=room.area_id,
        name=room.name,
        floor_id=room.floor_id,
        bedroom=room.is_bedroom,
        radar_entity_ids=room.radar_entity_ids,
        fallback_entity_ids=room.fallback_entity_ids,
        cleaning_period=source.room_cleaning_period(room.area_id),
        cleaning_profile=source.room_cleaning_profile(room.area_id),
        enabled=settings.enabled,
        occupancy_sensitivity_minutes=settings.occupancy_sensitivity_minutes,
        vacancy_fallback_enabled=settings.vacancy_fallback_enabled,
        vacancy_fallback_minutes=settings.vacancy_fallback_minutes,
        selected_robot_id=evaluation.selected_robot_id,
        decision_evaluated_at=evaluation.evaluated_at,
        overdue=settings.enabled and schedule_due is not None and schedule_due <= now,
        opportunity=_opportunity_view(detail, schedule_due, settings, now, occurrence),
        occupancy_quality=_occupancy_quality_view(detail, now),
        adjacency_mode=settings.adjacency_mode,
        adjacency_active=adjacency.active,
        adjacent_area_ids=adjacency.neighbor_area_ids,
        adjacency_blockers=tuple(
            RoomAdjacencyBlockerView(
                item.area_id,
                source.discovery.rooms[item.area_id].name,
                item.occupancy,
                (
                    VacancyDiagnostic.from_forecast(
                        source._room_data(item.area_id).occupancy_source,
                        source._room_data(item.area_id).unoccupied_since,
                        item.vacancy,
                    )
                    if item.vacancy is not None
                    else None
                ),
            )
            for item in adjacency.blockers
        ),
        adjacency_reason=(
            None if occurrence and occurrence.manual_override else adjacency_reason
        ),
        cleaning_interval=settings.cleaning_interval,
        expected_minutes=settings.expected_minutes,
        ignore_desired_window=settings.ignore_desired_window,
        desired_window_configured_start=desired_window.configured_start,
        desired_window_configured_end=desired_window.configured_end,
        desired_window_effective_start=desired_window.start,
        desired_window_effective_end=desired_window.end,
        desired_window_start_inherited=desired_window.start_inherited,
        desired_window_end_inherited=desired_window.end_inherited,
        desired_window_valid=desired_window.valid,
        vacuum_pass_count=settings.vacuum_pass_count,
        mop_pass_count=settings.mop_pass_count,
        cleaning_program=settings.cleaning_program,
        fan_speed=settings.fan_speed,
        mode=settings.mode,
        mop_mode=settings.mop_mode,
        mop_intensity=settings.mop_intensity,
        cleaning_depth=settings.cleaning_depth,
        effective_profiles=tuple(effective_profiles),
        latest_manual_request=_manual_audit_view(source, latest_manual),
        occupancy=detail.occupancy,
        occupancy_source=detail.occupancy_source,
        unavailable_radars=detail.unavailable_radars,
        last_cleaned=detail.cleaning_completed_at,
        last_cleaned_display=format_last_cleaned_age(detail.cleaning_completed_at, now),
        using_initial_cadence_baseline=bool(
            detail.cleaning_completed_at is None
            and source.state.first_scheduler_online_at
        ),
        last_vacuum=detail.vacuum_completed_at,
        last_mop=detail.mop_completed_at,
        next_due=next_due,
        next_clean_at=next_clean_at,
        schedule_due_at=schedule_due,
        next_clean_window_end_at=window_end,
        desired_window_start=desired_window_start,
        next_candidate=(
            _candidate_view(candidate) if candidate and assignment_available else None
        ),
        assignment_available=assignment_available,
        robot_eligibility=eligibility,
        active=_active_job_view(active),
        active_robot=active_robot_id,
        active_robot_state=(
            active_robot_state_object.state if active_robot_state_object else None
        ),
        effective_duration_minutes=duration_estimate.safe_minutes,
        duration_sample_count=duration_estimate.sample_count,
        predicted_total_minutes=duration_estimate.typical_minutes,
        required_vacancy_minutes=vacancy.required_clear_minutes,
        duration_model_version=2,
        duration_model_learned=duration_estimate.learned,
        duration_estimates_by_robot=tuple(duration_estimates),
        block_reason=reason,
        vacancy_diagnostic=vacancy,
        latest_scheduler_decision=_room_decision_view(latest_decision),
        legacy_deferral_review_needed=bool(
            cleaning_deferral and cleaning_deferral.source == "legacy_unknown"
        ),
        map_status=detail.map_status,
        map_error=detail.map_error,
        occurrence=_occurrence_view(source, occurrence),
        water_confirmation=(
            WaterConfirmationView(
                status=confirmation.status,
                sent_at=confirmation.sent_at,
                expires_at=confirmation.expires_at,
                responded_at=confirmation.responded_at,
            )
            if confirmation
            else None
        ),
        last_stage_outcome=detail.last_stage_outcome,
        last_stage_reason=detail.last_stage_reason,
        last_stage_at=detail.last_stage_at,
        last_stage_summary=detail.last_stage_summary,
        water_notification_episode=(
            WaterNotificationEpisodeView(
                room_id=episode.room_id,
                reason=episode.reason,
                first_sent_at=episode.first_sent_at,
                last_sent_at=episode.last_sent_at,
            )
            if episode
            else None
        ),
        failure=_fault_view(source, room_fault) if room_fault else None,
        recovery=(
            room_recovery_view(source, recovery)
            if (recovery := source.state.room_recoveries.get(area_id))
            else None
        ),
    )


def robot_view(source: ProjectionSource, entity_id: str) -> RobotView:
    """Build typed, immutable state for one discovered vacuum."""

    robot = source.discovery.robots[entity_id]
    state = source.hass.states.get(entity_id)
    ready, reason = source._robot_ready(robot)
    active = source.state.active_jobs.get(robot.registry_id)
    hold = source.state.robot_holds.get(robot.registry_id)
    active_rooms = (
        tuple(
            source.discovery.rooms[area_id].name
            for area_id in source._active_rooms(active)
            if area_id in source.discovery.rooms
        )
        if active
        else ()
    )
    settings = source._robot_settings(robot)
    active_mop_profile = (
        active.cleaning_profile if active and active.operation == "mop" else None
    )
    direct_route = (
        active_mop_profile.mop_mode if active_mop_profile else None
    ) or settings.mop_mode
    direct_intensity = (
        active_mop_profile.mop_intensity if active_mop_profile else None
    ) or settings.mop_intensity
    mop_profile_summary = (
        "Mop mode with suction off"
        f"; route: {str(direct_route or 'standard').replace('_', ' ')}"
        f"; water: {str(direct_intensity or 'medium').replace('_', ' ')}"
        if robot.adapter_capabilities.native_mop_profile
        else None
    )

    def observed(control_entity_id: str | None) -> str | None:
        observed_state = (
            source.hass.states.get(control_entity_id) if control_entity_id else None
        )
        return observed_state.state if observed_state else None

    robot_fault = source.state.robot_faults.get(robot.registry_id)
    run = source.state.cleaning_runs.get(robot.registry_id)
    samples = source.state.battery_samples.get(robot.registry_id, [])
    learning = tuple(
        (
            key,
            len([sample for sample in samples if sample.profile == key]),
            learned_battery_rate(
                sample.percent_per_minute for sample in samples if sample.profile == key
            )[0],
        )
        for key in sorted({sample.profile for sample in samples})
    )
    return RobotView(
        cleaning_run=(
            CleaningRunView(
                run.run_id,
                tuple(run.rooms),
                tuple(
                    source.discovery.rooms[key].name
                    if key in source.discovery.rooms
                    else key
                    for key in run.rooms
                ),
                run.started_room_id,
                run.expected_minutes,
                run.battery_percent,
                tuple(run.removed.items()),
                run.estimated_end_at,
            )
            if run
            else None
        ),
        battery_learning=learning,
        registry_id=robot.registry_id,
        entity_id=robot.entity_id,
        unique_fragment=source.robot_unique_fragment(robot.entity_id),
        name=robot.name,
        floor_id=robot.floor_id,
        state=state.state if state else "unavailable",
        battery=source._robot_battery(robot),
        ready=ready,
        reason=reason,
        active=_active_job_view(active),
        scheduler_hold=_hold_view(hold),
        active_room=", ".join(active_rooms) if active_rooms else None,
        active_rooms=active_rooms,
        profile=robot.profile,
        adapter_id=robot.adapter_id,
        adapter_schema_version=robot.adapter_schema_version,
        adapter_capabilities=robot.adapter_capabilities,
        adapter_diagnostic=robot.adapter_diagnostic,
        failure=_fault_view(source, robot_fault) if robot_fault else None,
        settings=RobotSettingsView(
            enabled=settings.enabled,
            minimum_battery=settings.minimum_battery,
            cleaning_program=settings.cleaning_program,
            double_pass=settings.double_pass,
            mop_double_pass=settings.mop_double_pass,
            mode=settings.mode,
            mop_mode=settings.mop_mode,
            mop_intensity=settings.mop_intensity,
            fan_speed=settings.fan_speed,
            cleaning_depth=settings.cleaning_depth,
            cleaning_depth_configured=settings.cleaning_depth_configured,
            direct_custom_mop_migrated=settings.direct_custom_mop_migrated,
        ),
        observed_profile=ObservedProfileView(
            fan_speed=(
                str(state.attributes.get("fan_speed"))
                if state and state.attributes.get("fan_speed") is not None
                else None
            ),
            mode=observed(robot.profile.mode_select_entity_id),
            mop_mode=observed(robot.profile.mop_mode_select_entity_id),
            mop_intensity=observed(robot.profile.mop_intensity_select_entity_id),
            passes=observed(robot.profile.passes_select_entity_id),
        ),
        mop_profile_summary=mop_profile_summary,
    )


def floor_plan_view(source: ProjectionSource) -> FloorPlanView:
    """Build a typed floor-plan projection from live registry discovery."""

    plan = source.state.floor_plan
    live_room_ids = set(source.discovery.rooms)
    source_registry_ids = {
        occupancy_source.registry_id
        for room in source.discovery.rooms.values()
        for occupancy_source in room.occupancy_sources
    }
    floors: dict[str, list[FloorPlanRoomView]] = {}
    for room in source.discovery.rooms.values():
        rectangle = plan.rooms.get(room.area_id)
        sensors = []
        for occupancy_source in room.occupancy_sources:
            observed = source.hass.states.get(occupancy_source.entity_id)
            state = (
                "active"
                if observed and observed.state == "on"
                else "inactive"
                if observed and observed.state == "off"
                else "unavailable"
            )
            marker = plan.sensors.get(occupancy_source.registry_id)
            sensors.append(
                FloorPlanSensorView(
                    registry_id=occupancy_source.registry_id,
                    entity_id=occupancy_source.entity_id,
                    kind=occupancy_source.kind,
                    state=state,
                    marker=(
                        marker if marker and marker.area_id == room.area_id else None
                    ),
                )
            )
        floors.setdefault(room.floor_id, []).append(
            FloorPlanRoomView(
                area_id=room.area_id,
                name=room.name,
                floor_id=room.floor_id,
                rectangle=(
                    rectangle
                    if rectangle and rectangle.floor_id == room.floor_id
                    else None
                ),
                sensors=tuple(sensors),
            )
        )
    return FloorPlanView(
        revision=plan.revision,
        floors=tuple(
            FloorView(
                floor_id=floor_id,
                rooms=tuple(
                    sorted(rooms, key=lambda room: (room.name.lower(), room.area_id))
                ),
            )
            for floor_id, rooms in sorted(floors.items())
        ),
        edges=tuple(sorted(plan.edges)),
        orphaned_rooms=tuple(
            sorted(area_id for area_id in plan.rooms if area_id not in live_room_ids)
        ),
        orphaned_sensors=tuple(
            sorted(
                registry_id
                for registry_id in plan.sensors
                if registry_id not in source_registry_ids
            )
        ),
    )


def build_snapshot(source: ProjectionSource) -> IntegrationSnapshot:
    """Build one immutable snapshot after an application transaction settles."""

    plan = floor_plan_view(source)
    robot_faults = tuple(
        _fault_view(source, fault)
        for _key, fault in sorted(source.state.robot_faults.items())
    )
    room_faults = tuple(
        _fault_view(source, fault)
        for _key, fault in sorted(source.state.room_faults.items())
    )
    recoveries = tuple(
        room_recovery_view(source, recovery)
        for _, recovery in sorted(source.state.room_recoveries.items())
    )
    all_faults = (
        *robot_faults,
        *room_faults,
        *(recovery.failure for recovery in recoveries),
    )
    singular_fault = all_faults[0] if len(all_faults) == 1 else None
    robots = tuple(
        robot_view(source, robot.entity_id)
        for robot in sorted(
            source.discovery.robots.values(),
            key=lambda item: item.registry_id,
        )
    )
    rooms = tuple(
        room_view(source, room.area_id)
        for room in sorted(
            source.discovery.rooms.values(),
            key=lambda item: item.area_id,
        )
    )
    confidence = source.get_global_setting("forecast_confidence")
    if not isinstance(confidence, (int, float)):
        raise TypeError("forecast_confidence must be numeric")
    scheduler = SchedulerView(
        adjacency_night_start=source.state.global_settings.adjacency_night_start,
        adjacency_night_end=source.state.global_settings.adjacency_night_end,
        observe_only=source.observe_only,
        party_mode=source.party_mode,
        scheduler_halted=source.scheduler_halted,
        scheduler_limited=source.scheduler_limited,
        storage_safe_mode=source.storage_safe_mode,
        forecast_confidence=float(confidence),
        unresolved_start=str(source.get_global_setting("unresolved_start")),
        unresolved_end=str(source.get_global_setting("unresolved_end")),
        last_evaluation_at=source.state.evaluation.last_evaluation_at,
        preview=FrozenJsonObject.from_mapping(
            source.state.evaluation.last_preview.to_mapping()
        ),
        robot_faults=robot_faults,
        room_faults=room_faults,
        room_recoveries=recoveries,
        floor_plan=plan,
        failure=singular_fault,
    )
    return IntegrationSnapshot(
        scheduler=scheduler,
        rooms=tuple(
            replace(room, robot_previews=room_robot_previews(room, robots, scheduler))
            for room in rooms
        ),
        robots=robots,
        floor_plan=plan,
    )
