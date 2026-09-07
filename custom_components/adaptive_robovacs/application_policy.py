"""Observation, readiness, and candidate policy for the scheduler application.

The methods in this component make no outbound cleaning calls.  They turn the
current typed discovery/state aggregate into observations and pure planner
candidates; dispatch remains a separate checkpointed transaction.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .commands import EvaluateCommand, SchedulerCommand, SchedulerCommandResult
from .const import (
    EXTRA_CLEAR_MINUTES,
    FALLBACK_SAMPLE_COUNT,
    HISTORY_DAYS,
    READY_CONFIRMATION_DELAY,
)
from .discovery import DiscoveredRobot, DiscoveredRoom, DiscoverySnapshot
from .models import (
    CleaningOperation,
    CleaningProgram,
    EvaluationCause,
    EvaluationMode,
    Forecast,
    JobPhase,
    OccurrenceSource,
    ResolvedDailyWindow,
    can_start_scheduled_clean,
    cleaning_profile_is_supported,
    cleaning_profile_sources,
    desired_window_allows,
    detailed_status_confirms_completion,
    detailed_status_is_dispatchable,
    dock_completion_deadline,
    due_at,
    effective_cadence_anchor,
    effective_cleaning_program,
    expand_cleaning_program,
    forecast_vacancy,
    learned_duration_estimate,
    manual_clean_robot_is_docked,
    map_recovery_hold_is_manual,
    ready_confirmation_elapsed,
    requested_cleaning_profile,
    resolve_cleaning_profile,
    stage_pass_count,
    startup_dispatch_allowed,
    unresolved_occupancy_allowed,
)
from .observations import HomeAssistantObserver
from .planner import (
    CandidateRobotDecision,
    RobotEligibility,
    ScheduleCandidate,
    VacancyDiagnostic,
)
from .state import (
    ActiveJob,
    CleaningStage,
    Deferral,
    OccupancySample,
    RobotSettings,
    RoomDecisionRecord,
    RoomHistory,
    RoomSettings,
    SchedulerState,
)

ROOM_DECISION_LIMIT = 100
DOCK_COMPLETION_DWELL = timedelta(minutes=5)


def _now() -> datetime:
    from . import application

    return application._now()


def _local(value: datetime) -> datetime:
    from . import application

    return application._local(value)


def async_track_point_in_utc_time(
    hass: HomeAssistant,
    action: Callable[[datetime], None],
    deadline: datetime,
) -> Callable[[], None]:
    from . import application

    return application._track_point(hass, action, deadline)


class ApplicationPolicyMixin:
    """Observe scheduler inputs and build revalidated typed candidates."""

    hass: HomeAssistant
    entry: ConfigEntry
    state: SchedulerState
    discovery: DiscoverySnapshot
    observer: HomeAssistantObserver
    _closing: bool
    _storage_safe_mode: bool
    _startup_state_settle_until: datetime | None
    _ready_since: dict[str, datetime]
    _ready_confirmation_timers: dict[str, Callable[[], None]]

    if TYPE_CHECKING:

        def _async_create_task(
            self, coro: Coroutine[Any, Any, Any], *, name: str | None = None
        ) -> asyncio.Task[Any] | None: ...

        async def async_execute(
            self, command: SchedulerCommand
        ) -> SchedulerCommandResult: ...

        def _room_data(self, area_id: str) -> RoomHistory: ...

        def _room_settings(self, room: DiscoveredRoom) -> RoomSettings: ...

        def _robot_settings(self, robot: DiscoveredRobot) -> RobotSettings: ...

        def _desired_window(self, room: DiscoveredRoom) -> ResolvedDailyWindow: ...

        def _active_rooms(self, active: ActiveJob) -> list[str]: ...

        def _schedule_recovery_completion(
            self, robot_id: str, deadline: datetime
        ) -> None: ...

        def _map_recovery_dispatch_block_reason(self) -> str | None: ...

    def fault_affects_robot(self, robot: DiscoveredRobot) -> bool:
        return robot.registry_id in self.state.robot_faults

    def _observe_occupancy(self, now: datetime) -> None:
        """Update durable occupancy history from one typed house observation."""

        cutoff = now - timedelta(days=HISTORY_DAYS)
        observation = self.observer.house(self.discovery)
        for observed_room in observation.rooms:
            detail = self._room_data(observed_room.area_id)
            observed = observed_room.observation
            state = observed.occupancy
            source = observed.source
            unavailable = observed.unavailable_radars
            prior = detail.occupancy
            if prior == "unoccupied" and state != "unoccupied":
                started = detail.unoccupied_since
                if started:
                    duration = int((now - started).total_seconds() / 60)
                    if duration > 0:
                        detail.occupancy_samples.append(
                            OccupancySample(started_at=started, minutes=duration)
                        )
                detail.unoccupied_since = None
            elif prior != "unoccupied" and state == "unoccupied":
                detail.unoccupied_since = now
            detail.occupancy = state
            detail.occupancy_source = source
            detail.unavailable_radars = unavailable
            detail.occupancy_samples = [
                sample
                for sample in detail.occupancy_samples
                if sample.started_at >= cutoff
            ]

    def _robot_battery(self, robot: DiscoveredRobot) -> float | None:
        return self.observer.robot(robot).battery

    def _expire_robot_cooldowns(self, now: datetime) -> None:
        """Drop elapsed robot-only cancellation cooldowns before evaluation."""

        for robot_id, cooldown in tuple(self.state.robot_cooldowns.items()):
            if cooldown.until <= now:
                self.state.robot_cooldowns.pop(robot_id, None)

    def _robot_technically_ready(
        self, robot: DiscoveredRobot, *, ignore_scheduler_fault: bool = False
    ) -> tuple[bool, str]:
        """Check every immediate physical and scheduler gate except the delay."""

        settings = self._robot_settings(robot)
        if robot.registry_id in self.state.robot_faults and not ignore_scheduler_fault:
            return False, "scheduler held after robot dispatch fault"
        if not settings.enabled:
            return False, "robot disabled"
        if not robot.supports_area_clean:
            return False, "does not support Home Assistant area cleaning"
        cooldown = self.state.robot_cooldowns.get(robot.registry_id)
        if cooldown and cooldown.until > _now():
            return False, "cooling down after physical cancellation"
        hold = self.state.robot_holds.get(robot.registry_id)
        if hold:
            if hold.phase == "cancelling":
                return False, "held clean returning to dock"
            if hold.phase == "completion_pending":
                return False, "held clean awaiting physical completion"
            if hold.reason == "robot_error":
                return False, "scheduler held after robot error"
            if map_recovery_hold_is_manual(hold.reason):
                return False, "map selection confirmation pending"
            return False, "scheduler held while robot is paused"
        active = self.state.active_jobs.get(robot.registry_id)
        if active:
            if active.phase == "cancelling":
                return False, "active clean returning to dock"
            if active.phase == "completion_held":
                return False, "active clean held after completion"
            if active.phase == "error_waiting":
                return False, "active job held after robot error"
            if active.phase == "paused":
                return False, "active job held while robot is paused"
            return False, "active job"
        state = self.hass.states.get(robot.entity_id)
        if not state or not can_start_scheduled_clean(state.state):
            return False, f"robot is {state.state if state else 'unavailable'}"
        readiness_entity_id = robot.adapter_capabilities.readiness_entity_id
        readiness = (
            self.hass.states.get(readiness_entity_id) if readiness_entity_id else None
        )
        readiness_state = readiness.state if readiness else None
        if not detailed_status_is_dispatchable(
            readiness_state,
            required=bool(readiness_entity_id),
            ready_states=robot.adapter_capabilities.readiness_states,
        ):
            return False, "awaiting robot servicing"
        battery = self._robot_battery(robot)
        if battery is None:
            return False, "battery unavailable"
        if battery < settings.minimum_battery:
            return False, "battery below minimum"
        return True, "dispatchable"

    def _robot_ready(
        self, robot: DiscoveredRobot, *, ignore_scheduler_fault: bool = False
    ) -> tuple[bool, str]:
        """Return whether a robot has remained dispatchable for ten seconds."""

        technical_ready, reason = self._robot_technically_ready(
            robot, ignore_scheduler_fault=ignore_scheduler_fault
        )
        if not technical_ready:
            return False, reason
        now = _now()
        ready_since = self._ready_since.get(robot.entity_id)
        if not ready_confirmation_elapsed(ready_since, now, READY_CONFIRMATION_DELAY):
            return False, "confirming robot readiness"
        return True, "ready"

    def _reset_ready_confirmation(self, robot_id: str) -> None:
        """Forget a readiness interval when any prerequisite is no longer true."""

        self._ready_since.pop(robot_id, None)
        unsubscribe = self._ready_confirmation_timers.pop(robot_id, None)
        if unsubscribe:
            unsubscribe()

    def _terminal_completion_is_observed(self, robot: DiscoveredRobot | None) -> bool:
        """Return whether a vendor detailed status proves docked work is done."""

        if robot is None:
            return False
        capabilities = robot.adapter_capabilities
        entity_id = capabilities.completion_status_entity_id
        status = self.hass.states.get(entity_id) if entity_id else None
        return detailed_status_confirms_completion(
            status.state if status else None,
            required=bool(entity_id),
            terminal_states=capabilities.terminal_completion_states,
        )

    def _dock_completion_deadline(
        self, active: ActiveJob, docked_at: datetime
    ) -> datetime:
        return dock_completion_deadline(
            active.expected_end,
            docked_at,
            DOCK_COMPLETION_DWELL,
        )

    def _set_dock_completion_pending(
        self, robot_id: str, active: ActiveJob, docked_at: datetime
    ) -> None:
        """Wait at the dock until the safe inferred-completion deadline."""

        active.phase = JobPhase.DOCK_COMPLETION_PENDING
        active.docked_at = docked_at
        self._schedule_recovery_completion(
            robot_id, self._dock_completion_deadline(active, docked_at)
        )

    def _schedule_ready_confirmation(
        self, robot_id: str, ready_since: datetime
    ) -> None:
        self._reset_ready_confirmation_timer(robot_id)
        deadline = ready_since + READY_CONFIRMATION_DELAY

        @callback
        def check_ready(_timestamp: datetime) -> None:
            self._ready_confirmation_timers.pop(robot_id, None)
            self._async_create_task(
                self.async_execute(
                    EvaluateCommand(
                        mode=EvaluationMode.DISPATCH,
                        cause=EvaluationCause.READY_CONFIRMATION,
                        detail=f"ready-confirmation:{robot_id}",
                    )
                )
            )

        self._ready_confirmation_timers[robot_id] = async_track_point_in_utc_time(
            self.hass, check_ready, deadline
        )

    def _reset_ready_confirmation_timer(self, robot_id: str) -> None:
        unsubscribe = self._ready_confirmation_timers.pop(robot_id, None)
        if unsubscribe:
            unsubscribe()

    def _refresh_robot_readiness(self, now: datetime) -> None:
        """Start or reset transient continuous-ready timers for all robots."""

        for robot in self.discovery.robots.values():
            ready, _ = self._robot_technically_ready(robot)
            if not ready:
                self._reset_ready_confirmation(robot.entity_id)
                continue
            if robot.entity_id in self._ready_since:
                continue
            self._ready_since[robot.entity_id] = now
            self._schedule_ready_confirmation(robot.entity_id, now)

    def _manual_robot_ready(self, robot: DiscoveredRobot) -> tuple[bool, str]:
        """Apply the documented physical readiness rule for manual work."""

        state = self.hass.states.get(robot.entity_id)
        observed = state.state if state else None
        if not manual_clean_robot_is_docked(observed):
            return False, "robot is not docked"
        return True, "docked"

    def _room_deferral(self, room: DiscoveredRoom, operation: str) -> datetime | None:
        """Return only a room-scoped, recognised deferral."""

        detail = self._room_data(room.area_id)
        deferral = detail.deferrals.get(operation)
        if deferral is None:
            return None
        if deferral.room_area_id not in {None, room.area_id}:
            return None
        if deferral.source not in {
            "manual_clean",
            "affected_cancellation",
            "legacy_unknown",
        }:
            return None
        return deferral.until

    def _set_room_deferral(
        self,
        room: DiscoveredRoom,
        operation: str,
        deferred_until: datetime,
        source: str,
        created_at: datetime,
    ) -> None:
        """Persist an explainable deferral for exactly one room."""

        detail = self._room_data(room.area_id)
        detail.deferrals[operation] = Deferral(
            until=deferred_until,
            source=source,
            created_at=created_at,
            room_area_id=room.area_id,
        )

    def _room_due(
        self, room: DiscoveredRoom, operation: str, now: datetime
    ) -> datetime:
        del operation
        detail = self._room_data(room.area_id)
        settings = self._room_settings(room)
        effective_completed = effective_cadence_anchor(
            detail.cleaning_completed_at,
            self.state.first_scheduler_online_at,
        )
        return due_at(
            effective_completed,
            settings.cleaning_interval,
            self._room_deferral(room, "cleaning"),
            now,
        )

    def _duration_estimate(
        self,
        room: DiscoveredRoom,
        operation: str,
        passes: int,
        robot_id: str | None = None,
    ) -> Any:
        """Return the verified duration estimate for one executable stage."""

        detail = self._room_data(room.area_id)
        samples: list[float] = []
        for sample in detail.duration_samples:
            if (
                sample.operation != operation
                or sample.source != "elapsed_total_v2"
                or sample.measurement_version != 2
            ):
                continue
            if robot_id is not None and sample.robot_registry_id != robot_id:
                continue
            if sample.passes == passes:
                samples.append(sample.minutes)
        return learned_duration_estimate(
            samples,
            self._room_settings(room).expected_minutes,
        )

    def _effective_duration(
        self,
        room: DiscoveredRoom,
        operation: str,
        passes: int,
        robot_id: str | None = None,
    ) -> tuple[float, int]:
        """Return the conservative duration retained by existing callers."""

        estimate = self._duration_estimate(room, operation, passes, robot_id)
        return estimate.safe_minutes, estimate.sample_count

    def _forecast(
        self, room: DiscoveredRoom, now: datetime, duration_minutes: float
    ) -> Forecast:
        detail = self._room_data(room.area_id)
        if detail.occupancy_source == "no_sensor":
            return Forecast(True, 1.0, "no-sensor policy")
        samples = [
            {"start": _local(sample.started_at), "minutes": sample.minutes}
            for sample in detail.occupancy_samples
        ]
        return forecast_vacancy(
            samples,
            _local(now),
            _local(detail.unoccupied_since) if detail.unoccupied_since else None,
            int(duration_minutes) + EXTRA_CLEAR_MINUTES,
            self.state.global_settings.forecast_confidence,
            FALLBACK_SAMPLE_COUNT,
        )

    def _vacancy_diagnostic(
        self, room: DiscoveredRoom, now: datetime, duration_minutes: float
    ) -> VacancyDiagnostic:
        """Expose safe vacancy evidence without exposing raw occupancy payloads."""

        detail = self._room_data(room.area_id)
        forecast = self._forecast(room, now, duration_minutes)
        return VacancyDiagnostic(
            occupancy_source=detail.occupancy_source,
            unoccupied_since=detail.unoccupied_since,
            required_clear_minutes=forecast.required_minutes,
            clear_minutes=(
                round(forecast.clear_minutes, 1)
                if forecast.clear_minutes is not None
                else None
            ),
            forecast_confidence=forecast.confidence,
            comparable_sample_count=forecast.comparable_samples,
            successful_sample_count=forecast.successful_samples,
            reason=forecast.reason,
            allowed=forecast.allowed,
        )

    def _record_room_decision(
        self,
        room: DiscoveredRoom,
        reason: str,
        now: datetime,
        duration_minutes: float,
    ) -> None:
        """Keep a bounded, safe audit when an eligibility outcome changes."""

        decisions = self.state.audit.room_decisions
        diagnostic = self._vacancy_diagnostic(room, now, duration_minutes)
        event = RoomDecisionRecord(
            at=now,
            room_area_id=room.area_id,
            reason=reason,
            occupancy_source=diagnostic.occupancy_source,
            required_clear_minutes=diagnostic.required_clear_minutes,
            clear_minutes=diagnostic.clear_minutes,
            forecast_confidence=diagnostic.forecast_confidence,
            comparable_sample_count=diagnostic.comparable_sample_count,
            forecast_reason=diagnostic.reason,
        )
        previous = next(
            (item for item in reversed(decisions) if item.room_area_id == room.area_id),
            None,
        )
        if previous and (
            previous.reason,
            previous.occupancy_source,
            previous.required_clear_minutes,
            previous.forecast_reason,
        ) == (
            event.reason,
            event.occupancy_source,
            event.required_clear_minutes,
            event.forecast_reason,
        ):
            return
        decisions.append(event)
        self.state.audit.room_decisions = decisions[-ROOM_DECISION_LIMIT:]

    def _desired_window_allows(self, room: DiscoveredRoom, now: datetime) -> bool:
        """Apply the room's effective window unless it explicitly ignores it."""

        window = self._desired_window(room)
        return desired_window_allows(
            self._room_settings(room).ignore_desired_window,
            _local(now),
            window.start,
            window.end,
        )

    def _unresolved_allowed(self, room: DiscoveredRoom, now: datetime) -> bool:
        """Permit unresolved occupancy only inside the desired cleaning window."""

        window = self._desired_window(room)
        return unresolved_occupancy_allowed(
            self._room_data(room.area_id).occupancy,
            _local(now),
            window.start,
            window.end,
        )

    def _startup_state_settle_reason(self, now: datetime) -> str | None:
        """Block new physical work until Home Assistant has restored live state."""

        if startup_dispatch_allowed(now, self._startup_state_settle_until):
            return None
        return "awaiting Home Assistant state restoration"

    def _manual_candidate(
        self,
        room: DiscoveredRoom,
        now: datetime,
        mode: str,
        context_id: str | None,
        user_id: str | None,
    ) -> ScheduleCandidate:
        """Build an explicit user override without scheduler eligibility gates."""

        settings = self._room_settings(room)
        return ScheduleCandidate(
            room_id=room.area_id,
            floor_id=room.floor_id,
            operation=CleaningOperation.VACUUM,
            due_at=now,
            confidence=1.0,
            reason="manual override",
            duration_minutes=settings.expected_minutes,
            duration_sample_count=0,
            passes=1,
            occurrence=None,
            evaluated_at=now,
            unresolved_window_allowed=True,
            bypass_forecast=True,
            manual_override=True,
            source=OccurrenceSource.MANUAL_DASHBOARD,
            manual_mode=mode,
            manual_context_id=context_id,
            manual_user_id=user_id,
            bypass_desired_window=True,
        )

    def _recheck_candidate(
        self,
        room: DiscoveredRoom,
        now: datetime,
    ) -> ScheduleCandidate:
        """Build a non-dispatching candidate for an explicit Repair recheck."""

        occurrence = self.state.occurrences.get(room.area_id)
        operation = CleaningOperation.VACUUM
        passes = 1
        if occurrence and occurrence.current_stage < len(occurrence.stages):
            stage = occurrence.stages[occurrence.current_stage]
            operation = stage.operation
            passes = stage.passes
        settings = self._room_settings(room)
        return ScheduleCandidate(
            room_id=room.area_id,
            floor_id=room.floor_id,
            operation=operation,
            due_at=now,
            confidence=1.0,
            reason="repair recheck",
            duration_minutes=settings.expected_minutes,
            duration_sample_count=0,
            passes=passes,
            occurrence=occurrence,
            evaluated_at=now,
            unresolved_window_allowed=True,
            bypass_forecast=True,
            manual_override=bool(occurrence and occurrence.manual_override),
            source=(occurrence.source if occurrence else OccurrenceSource.SCHEDULER),
        )

    def _room_candidate(
        self,
        room: DiscoveredRoom,
        now: datetime,
    ) -> tuple[ScheduleCandidate | None, str]:
        """Return a due room or a persisted occurrence awaiting its next stage."""

        settings = self._room_settings(room)
        detail = self._room_data(room.area_id)
        if room.area_id in self.state.room_faults:
            return None, "room dispatch blocked pending Repair"
        if room.area_id in self.state.room_recoveries:
            return None, "room recovery blocked pending Repair"
        occurrence = self.state.occurrences.get(room.area_id)
        manual_override = bool(occurrence and occurrence.manual_override)
        if not settings.enabled and not manual_override:
            self.state.water_notification_episodes.pop(room.area_id, None)
            return None, "room disabled"
        if settle_reason := self._startup_state_settle_reason(now):
            return None, settle_reason
        if occurrence:
            if occurrence.source == "manual_dashboard":
                due = occurrence.scheduled_at
            else:
                cleaning_deferral = detail.deferrals.get("cleaning")
                due = max(
                    (
                        value
                        for value in (
                            occurrence.scheduled_at,
                            cleaning_deferral.until if cleaning_deferral else None,
                        )
                        if value is not None
                    ),
                    default=now,
                )
        else:
            due = self._room_due(room, "cleaning", now)
        if due > now:
            return None, "not due"
        if occurrence:
            confirmation = self.state.water_confirmations.get(occurrence.occurrence_id)
            if confirmation and confirmation.status == "pending":
                return None, "waiting for water confirmation"
        if not manual_override and detail.occupancy == "occupied":
            return None, (f"occupancy {detail.occupancy} ({detail.occupancy_source})")
        bypass_desired_window = bool(occurrence and occurrence.bypass_desired_window)
        if (
            not manual_override
            and not self._desired_window_allows(room, now)
            and not bypass_desired_window
        ):
            return None, "waiting for desired cleaning window"
        unresolved_window_allowed = self._unresolved_allowed(room, now)
        if manual_override:
            unresolved_window_allowed = True
        if (
            not manual_override
            and detail.occupancy != "unoccupied"
            and not unresolved_window_allowed
        ):
            if detail.occupancy == "unresolved":
                return None, "unresolved occupancy; waiting for desired cleaning window"
            return None, (f"occupancy {detail.occupancy} ({detail.occupancy_source})")
        operation = CleaningOperation.VACUUM
        passes = 1
        if occurrence:
            stage_index = occurrence.current_stage
            stages = occurrence.stages
            if stage_index >= len(stages):
                return None, "occurrence is complete"
            operation = stages[stage_index].operation
            passes = stages[stage_index].passes
        duration_minutes, duration_sample_count = self._effective_duration(
            room, operation, passes
        )
        forecast = (
            Forecast(True, 0.0, "unresolved occupancy desired-window policy")
            if unresolved_window_allowed
            else Forecast(True, 0.0, "awaiting robot-specific vacancy forecast")
        )
        return ScheduleCandidate(
            room_id=room.area_id,
            floor_id=room.floor_id,
            operation=operation,
            due_at=due,
            confidence=forecast.confidence,
            reason=forecast.reason,
            duration_minutes=duration_minutes,
            duration_sample_count=duration_sample_count,
            passes=passes,
            occurrence=occurrence,
            evaluated_at=now,
            unresolved_window_allowed=unresolved_window_allowed,
            bypass_forecast=manual_override,
            manual_override=manual_override,
            source=(occurrence.source if occurrence else OccurrenceSource.SCHEDULER),
            manual_mode=occurrence.manual_mode if occurrence else None,
            manual_context_id=(occurrence.manual_context_id if occurrence else None),
            manual_user_id=occurrence.manual_user_id if occurrence else None,
            bypass_desired_window=bypass_desired_window,
        ), "ready"

    def _resolve_candidate_for_robot(
        self,
        candidate: ScheduleCandidate,
        robot: DiscoveredRobot,
    ) -> tuple[ScheduleCandidate | None, str]:
        """Resolve one robot candidate and retain a safe rejection reason."""

        room = self.discovery.rooms.get(candidate.room_id)
        if room is None:
            return None, "room is no longer discovered"
        occurrence = candidate.occurrence
        if occurrence:
            if (
                not candidate.manual_override
                and occurrence.robot_registry_id != robot.registry_id
            ):
                return None, "occurrence assigned to another robot"
            stage_index = occurrence.current_stage
            occurrence_stages = occurrence.stages
            if stage_index >= len(occurrence_stages):
                return None, "occurrence is complete"
            stage = occurrence_stages[stage_index]
            operation = stage.operation
            passes = stage.passes
            if not robot.adapter_capabilities.supports(operation, passes):
                return None, "robot does not support the scheduled stage"
            resolved_profile = stage.cleaning_profile
            if resolved_profile is None:
                resolved = resolve_cleaning_profile(
                    operation,
                    self._room_settings(room),
                    self._robot_settings(robot),
                    robot.adapter_capabilities,
                )
                if resolved is None:
                    return None, "cleaning profile is not compatible"
                resolved_profile = resolved
            elif not cleaning_profile_is_supported(
                resolved_profile,
                robot.adapter_capabilities,
            ):
                return None, "stored cleaning profile is not compatible"
            duration, count = self._effective_duration(
                room, operation, passes, robot.registry_id
            )
            forecast = (
                Forecast(True, 1.0, "manual override")
                if candidate.bypass_forecast
                else Forecast(True, 0.0, "unresolved occupancy desired-window policy")
                if candidate.unresolved_window_allowed
                else self._forecast(
                    room,
                    candidate.evaluated_at,
                    duration,
                )
            )
            if not forecast.allowed:
                return None, forecast.reason
            return replace(
                candidate,
                operation=operation,
                passes=passes,
                duration_minutes=duration,
                duration_sample_count=count,
                confidence=forecast.confidence,
                reason=forecast.reason,
                occurrence_id=occurrence.occurrence_id,
                stage_index=stage_index,
                program=occurrence.program,
                resolved_profile=resolved_profile,
                requested_profile=stage.requested_profile,
                profile_sources=stage.profile_sources,
                source=occurrence.source,
                manual_mode=occurrence.manual_mode,
                manual_context_id=occurrence.manual_context_id,
                manual_user_id=occurrence.manual_user_id,
                vacancy_diagnostic=self._vacancy_diagnostic(
                    room,
                    candidate.evaluated_at,
                    duration,
                ),
            ), "eligible"

        room_settings = self._room_settings(room)
        robot_settings = self._robot_settings(robot)
        manual_mode = candidate.manual_mode
        program = (
            {
                "vacuum_only": CleaningProgram.VACUUM_ONLY,
                "mop_only": CleaningProgram.MOP_ONLY,
            }.get(str(manual_mode))
            if manual_mode and manual_mode != "configured"
            else effective_cleaning_program(
                room_settings.cleaning_program,
                robot_settings.cleaning_program,
            )
        )
        operations = expand_cleaning_program(program or "")
        if not operations:
            return None, "cleaning program is not configured"
        planned_stages: list[CleaningStage] = []
        for operation in operations:
            planned_passes = stage_pass_count(
                operation,
                room_settings.vacuum_pass_count,
                room_settings.mop_pass_count,
                robot_settings.double_pass,
                robot_settings.mop_double_pass,
                robot.adapter_capabilities,
            )
            if planned_passes is None or not robot.adapter_capabilities.supports(
                operation, planned_passes
            ):
                return None, "robot does not support the requested passes"
            resolved_profile = resolve_cleaning_profile(
                operation,
                room_settings,
                robot_settings,
                robot.adapter_capabilities,
            )
            if resolved_profile is None:
                return None, "cleaning profile is not compatible"
            planned_stages.append(
                CleaningStage(
                    operation=CleaningOperation(operation),
                    passes=planned_passes,
                    cleaning_profile=resolved_profile,
                    requested_profile=requested_cleaning_profile(
                        room_settings,
                        robot_settings,
                    ),
                    profile_sources=tuple(cleaning_profile_sources(room_settings)),
                )
            )
        operation = planned_stages[0].operation
        passes = planned_stages[0].passes
        duration, count = self._effective_duration(
            room, operation, passes, robot.registry_id
        )
        forecast = (
            Forecast(True, 1.0, "manual override")
            if candidate.bypass_forecast
            else Forecast(True, 0.0, "unresolved occupancy desired-window policy")
            if candidate.unresolved_window_allowed
            else self._forecast(
                room,
                candidate.evaluated_at,
                duration,
            )
        )
        if not forecast.allowed:
            return None, forecast.reason
        return replace(
            candidate,
            operation=operation,
            passes=passes,
            duration_minutes=duration,
            duration_sample_count=count,
            confidence=forecast.confidence,
            reason=forecast.reason,
            program=program,
            new_stages=tuple(planned_stages),
            stage_index=0,
            resolved_profile=planned_stages[0].cleaning_profile,
            requested_profile=planned_stages[0].requested_profile,
            profile_sources=planned_stages[0].profile_sources,
            source=candidate.source,
            manual_mode=manual_mode,
            manual_context_id=candidate.manual_context_id,
            vacancy_diagnostic=self._vacancy_diagnostic(
                room,
                candidate.evaluated_at,
                duration,
            ),
        ), "eligible"

    def _candidate_for_robot(
        self,
        candidate: ScheduleCandidate,
        robot: DiscoveredRobot,
    ) -> ScheduleCandidate | None:
        """Return the resolved candidate for callers that do not need a reason."""

        resolved, _reason = self._resolve_candidate_for_robot(candidate, robot)
        return resolved

    def _candidate_robot_diagnostics(
        self,
        candidate: ScheduleCandidate,
        readiness: Mapping[str, tuple[bool, str]] | None = None,
    ) -> tuple[CandidateRobotDecision, ...]:
        """Return safe per-robot eligibility for preview and room diagnostics."""

        room = self.discovery.rooms.get(candidate.room_id)
        if room is None:
            return ()
        diagnostics: list[CandidateRobotDecision] = []
        for robot in self.discovery.robots.values():
            if robot.floor_id != room.floor_id:
                continue
            ready, ready_reason = (
                readiness.get(robot.entity_id, self._robot_ready(robot))
                if readiness is not None
                else self._robot_ready(robot)
            )
            if candidate.manual_override:
                ready, ready_reason = self._manual_robot_ready(robot)
            if not ready:
                diagnostics.append(
                    CandidateRobotDecision(
                        RobotEligibility(
                            robot_id=robot.entity_id,
                            robot_name=robot.name,
                            eligible=False,
                            reason=ready_reason,
                        )
                    )
                )
                continue
            resolved, reason = self._resolve_candidate_for_robot(candidate, robot)
            diagnostics.append(
                CandidateRobotDecision(
                    RobotEligibility(
                        robot_id=robot.entity_id,
                        robot_name=robot.name,
                        eligible=resolved is not None,
                        reason=reason,
                    ),
                    resolved,
                )
            )
        return tuple(diagnostics)
