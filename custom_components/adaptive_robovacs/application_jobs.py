"""Observed-state job lifecycle orchestration.

Robot observations stay authoritative here while pure reducers in jobs.py
decide durable transitions and effects. Restart-only normalization and timers
live in application_recovery.py.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .commands import SchedulerCommand, SchedulerCommandResult
from .const import START_CONFIRMATION_TIMEOUT
from .discovery import DiscoveredRobot, DiscoveredRoom, DiscoverySnapshot
from .jobs import (
    DeferralCandidate,
    JobTransition,
    ManualAuditEffect,
    RecoveryAuditEffect,
    active_rooms,
    reduce_job_cancellation,
    reduce_job_completion,
    reduce_manual_deferrals,
    should_assume_native_app_clean,
)
from .models import (
    CleaningOperation,
    JobPhase,
    elapsed_total_duration_minutes,
    managed_clean_duration_failed,
    pending_completion_is_docked,
)
from .state import (
    ActiveJob,
    ManualAuditRecord,
    RecoveryAuditRecord,
    RoomHistory,
    RoomSettings,
    SchedulerState,
)

_LOGGER = logging.getLogger(__name__)


def _now() -> datetime:
    from . import application

    return application._now()


class ApplicationJobsMixin:
    """Reconcile durable jobs from fresh robot observations."""

    hass: HomeAssistant
    entry: ConfigEntry
    state: SchedulerState
    discovery: DiscoverySnapshot
    _recovery_timers: dict[str, Callable[[], None]]

    if TYPE_CHECKING:

        def _async_create_task(
            self, coro: Coroutine[Any, Any, Any], *, name: str | None = None
        ) -> asyncio.Task[Any] | None: ...

        async def async_execute(
            self, command: SchedulerCommand
        ) -> SchedulerCommandResult: ...

        async def _async_save(self) -> None: ...

        def _room_data(self, area_id: str) -> RoomHistory: ...

        def _room_settings(self, room: DiscoveredRoom) -> RoomSettings: ...

        def robot_for_registry_id(self, registry_id: str) -> DiscoveredRobot | None: ...

        def robot_registry_id(self, entity_id: str) -> str: ...

        def _cancel_start_confirmation(self, robot_id: str) -> None: ...

        def _discard_unconfirmed_scheduler_job(
            self, robot: DiscoveredRobot, room: DiscoveredRoom
        ) -> None: ...

        async def _async_latch_scheduler_fault(
            self,
            robot: DiscoveredRobot,
            room: DiscoveredRoom,
            reason_code: str,
            phase: str,
            *,
            native_command_may_have_started: bool,
            outcome_uncertain: bool,
        ) -> None: ...

        async def _async_downgrade_q10_max_plus(
            self, robot: DiscoveredRobot, room: DiscoveredRoom, candidate: Any
        ) -> None: ...

        def _mop_washing_is_observed(
            self, robot: DiscoveredRobot | None, active: ActiveJob | None
        ) -> bool: ...

        def _mark_mop_washing_started(
            self, robot: DiscoveredRobot, active: ActiveJob, now: datetime
        ) -> bool: ...

        def _terminal_completion_is_observed(
            self, robot: DiscoveredRobot | None
        ) -> bool: ...

        def _set_dock_completion_pending(
            self, robot_id: str, active: ActiveJob, docked_at: datetime
        ) -> None: ...

        def _dock_completion_deadline(
            self, active: ActiveJob, docked_at: datetime
        ) -> datetime: ...

        def _room_due(
            self, room: DiscoveredRoom, operation: str, now: datetime
        ) -> datetime: ...

        def _set_room_deferral(
            self,
            room: DiscoveredRoom,
            operation: str,
            deferred_until: datetime,
            source: str,
            created_at: datetime,
        ) -> None: ...

        def _reconcile_robot_hold(
            self,
            robot_id: str,
            state_text: str,
            active: ActiveJob | None,
            now: datetime,
        ) -> str | None: ...

        def _hold_active_job(
            self,
            robot_id: str,
            active: ActiveJob,
            state_text: str,
            now: datetime,
        ) -> bool: ...

        def _resume_held_job(
            self, robot_id: str, active: ActiveJob, state: Any, now: datetime
        ) -> None: ...

        def _set_held_job_phase(
            self,
            robot_id: str,
            active: ActiveJob,
            action: str,
            now: datetime,
        ) -> None: ...

        def _apply_robot_cancellation_deferral(
            self, robot_id: str, cancelled_at: datetime
        ) -> list[str]: ...

        def _cancel_recovery_timer(self, robot_id: str) -> None: ...

        def _active_rooms(self, active: ActiveJob) -> list[str]: ...

    async def _async_reconcile_jobs(self, now: datetime) -> None:
        """Persist completion only after an accepted command has actually cleaned."""

        changed = False
        robot_registry_ids = (
            {robot.registry_id for robot in self.discovery.robots.values()}
            | set(self.state.active_jobs)
            | set(self.state.robot_holds)
        )
        for registry_id in robot_registry_ids:
            robot = self.robot_for_registry_id(registry_id)
            robot_id = robot.entity_id if robot else registry_id
            active = self.state.active_jobs.get(registry_id)
            state = self.hass.states.get(robot.entity_id) if robot else None
            state_text = state.state if state else "unavailable"
            if active and state_text not in {"unavailable", "unknown"}:
                active.last_observed_at = now
                changed = True

            if active and self._mop_washing_is_observed(robot, active):
                if robot and self._mark_mop_washing_started(robot, active, now):
                    changed = True
                continue

            if (
                active
                and active.source in {"scheduler", "manual_dashboard"}
                and active.phase == "accepted"
                and not active.seen_cleaning
            ):
                accepted_at = active.accepted_at or active.started_at
                immediate_failure = state_text in {
                    "paused",
                    "error",
                    "returning",
                    "unavailable",
                    "unknown",
                }
                confirmation_expired = bool(
                    state_text != "cleaning"
                    and accepted_at
                    and now - accepted_at >= START_CONFIRMATION_TIMEOUT
                )
                if immediate_failure or confirmation_expired:
                    room = self.discovery.rooms.get(active.room_id)
                    if robot and room:
                        if active.q10_max_plus_fallback:
                            await self._async_downgrade_q10_max_plus(
                                robot, room, active
                            )
                        uncertain = state_text not in {"docked", "idle"}
                        await self._async_latch_scheduler_fault(
                            robot,
                            room,
                            (
                                "start_outcome_uncertain"
                                if uncertain
                                else "start_confirmation_failed"
                            ),
                            "start_confirmation",
                            native_command_may_have_started=uncertain,
                            outcome_uncertain=uncertain,
                        )
                        changed = True
                        continue

            if active and pending_completion_is_docked(state_text, active.phase):
                completion = active.cleaning_finished_at or (
                    state.last_changed if state else now
                )
                confidence = (
                    active.completion_confidence or "observed"
                    if active.cleaning_finished_at
                    else "observed_pending_completion"
                )
                if not active.cleaning_finished_at:
                    self._mark_observed_completion(robot_id, active, completion)
                await self._async_complete_job(robot_id, active, completion, confidence)
                self.state.robot_holds.pop(registry_id, None)
                changed = True
                continue

            hold_action = self._reconcile_robot_hold(
                registry_id, state_text, active, now
            )
            if hold_action == "held":
                if active:
                    changed = (
                        self._hold_active_job(robot_id, active, state_text, now)
                        or changed
                    )
                else:
                    changed = True
                continue
            if hold_action == "resumed":
                if active:
                    self._resume_held_job(robot_id, active, state, now)
                changed = True
                continue
            if hold_action in {"cancelling", "completion_pending"}:
                if active:
                    self._set_held_job_phase(robot_id, active, hold_action, now)
                changed = True
                continue
            if hold_action == "cancelled":
                hold = self.state.robot_holds.get(registry_id)
                cancelled_at = hold.returning_at if hold and hold.returning_at else now
                if active:
                    self._cancel_job(
                        robot_id, active, cancelled_at, "physical_cancelled"
                    )
                self._apply_robot_cancellation_deferral(robot_id, cancelled_at)
                self.state.robot_holds.pop(registry_id, None)
                changed = True
                continue
            if hold_action == "complete":
                if active:
                    completion = active.cleaning_finished_at or now
                    if not active.cleaning_finished_at:
                        self._mark_observed_completion(robot_id, active, completion)
                    await self._async_complete_job(
                        robot_id, active, completion, "observed"
                    )
                self.state.robot_holds.pop(registry_id, None)
                changed = True
                continue
            if not active:
                continue

            fault = self.state.robot_faults.get(registry_id) if robot else None
            fault_room = self.discovery.rooms.get(fault.room_area_id) if fault else None
            if (
                robot
                and fault_room
                and should_assume_native_app_clean(
                    state_text,
                    fault,
                    robot.registry_id,
                    active,
                )
            ):
                # The physical state confirms a clean, not its room. Preserve
                # the scheduled stage for a later retry and classify the live
                # clean as native-app activity.
                self._discard_unconfirmed_scheduler_job(robot, fault_room)
                changed = True
                continue

            if state_text == "cleaning":
                self._resume_held_job(robot_id, active, state, now)
                changed = True
                continue
            if active.seen_cleaning and state_text == "returning":
                active.phase = JobPhase.RETURNING
                active.docked_at = None
                self._cancel_recovery_timer(robot_id)
                changed = True
                continue
            if active.seen_cleaning and state_text == "docked":
                docked_at = active.docked_at or (state.last_changed if state else now)
                if self._terminal_completion_is_observed(robot):
                    confidence = (
                        "recovered_terminal_status"
                        if active.recovery_crossed
                        else "observed"
                    )
                    self._mark_observed_completion(
                        robot_id,
                        active,
                        docked_at,
                        confidence,
                        allow_sample=confidence == "observed",
                    )
                    await self._async_complete_job(
                        robot_id, active, docked_at, confidence
                    )
                    changed = True
                else:
                    deadline = self._dock_completion_deadline(active, docked_at)
                    if now >= deadline:
                        confidence = (
                            "recovered_dock_dwell"
                            if active.recovery_crossed
                            else "inferred_dock_dwell"
                        )
                        self._mark_observed_completion(
                            robot_id, active, docked_at, confidence, allow_sample=False
                        )
                        await self._async_complete_job(
                            robot_id, active, docked_at, confidence
                        )
                        changed = True
                    else:
                        self._set_dock_completion_pending(robot_id, active, docked_at)
                        changed = True
                continue
            started = active.started_at
            if (
                not active.seen_cleaning
                and started
                and now - started > timedelta(minutes=10)
            ):
                if active.source == "manual_home_assistant":
                    self._record_manual_event(
                        ManualAuditRecord(
                            at=now,
                            robot_registry_id=self.robot_registry_id(robot_id),
                            room_ids=tuple(self._active_rooms(active)),
                            context_id=active.manual_context_id,
                            outcome="not_started_or_cancelled",
                        )
                    )
                else:
                    # Scheduler-owned jobs are handled by the bounded confirmation
                    # branch above. This legacy fallback is retained for malformed
                    # checkpoints that predate accepted_at.
                    continue
                self.state.active_jobs[registry_id] = None
                self._cancel_recovery_timer(robot_id)
                changed = True
        if changed:
            await self._async_save()

    def _mark_observed_completion(
        self,
        robot_id: str,
        active: ActiveJob,
        completion: datetime,
        confidence: str = "observed",
        allow_sample: bool = True,
    ) -> None:
        """Record a completion from a native state transition and learn from it."""

        active.cleaning_finished_at = completion
        active.native_timer_elapsed = self._native_timer_elapsed_minutes(
            robot_id, active
        )
        if (
            allow_sample
            and active.forecast_sample_eligible
            and not active.recovery_crossed
        ):
            active.measured_minutes = self._measured_duration_minutes(active)
            active.duration_source = "elapsed_total_v2"
        active.completion_confidence = confidence

    def _cleaning_timer_minutes(self, robot_id: str) -> float | None:
        robot = self.discovery.robots.get(robot_id)
        entity_id = robot.profile.cleaning_time_entity_id if robot else None
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None:
            return None
        try:
            value = float(state.state)
        except TypeError, ValueError:
            return None
        unit = str(state.attributes.get("unit_of_measurement", "min")).lower()
        if unit in {"h", "hour", "hours"}:
            return value * 60
        if unit in {"s", "second", "seconds"}:
            return value / 60
        return value

    def _native_timer_elapsed_minutes(
        self, robot_id: str, active: ActiveJob
    ) -> float | None:
        timer = self._cleaning_timer_minutes(robot_id)
        timer_start = active.timer_start
        if (
            timer is not None
            and timer_start is not None
            and timer >= float(timer_start)
        ):
            return timer - float(timer_start)
        return None

    def _measured_duration_minutes(self, active: ActiveJob) -> float | None:
        """Measure complete elapsed work, excluding observed interruptions."""

        started = active.observed_started_at
        finished = active.cleaning_finished_at
        return elapsed_total_duration_minutes(
            started,
            finished,
            active.interruption_minutes,
        )

    def _cancel_job(
        self, robot_id: str, active: ActiveJob, cancelled_at: datetime, reason: str
    ) -> None:
        transition = reduce_job_cancellation(
            robot_id,
            self.robot_registry_id(robot_id),
            active,
            cancelled_at,
            reason,
            self.state.occurrences.get(active.room_id),
        )
        self._apply_job_transition(transition)

    def _complete_job(
        self,
        robot_id: str,
        active: ActiveJob,
        completion: datetime,
        confidence: str,
    ) -> None:
        deferred: tuple[str, ...] = ()
        if active.source == "manual_home_assistant":
            deferred = tuple(
                self._apply_manual_deferral(
                    robot_id,
                    list(active_rooms(active)),
                    list(active.requested_operations or [CleaningOperation.VACUUM]),
                    completion,
                )
            )
        transition = reduce_job_completion(
            robot_id,
            self.robot_registry_id(robot_id),
            active,
            completion,
            confidence,
            self.state.occurrences.get(active.room_id),
            deferred,
        )
        self._apply_job_transition(transition)

    def _apply_job_transition(self, transition: JobTransition) -> None:
        """Apply one pure job transition inside the application transaction."""

        history = self.state.room_history.setdefault(
            transition.room_id,
            RoomHistory(),
        )
        completion = transition.completed_at
        if completion is not None and transition.set_room_operation_completion:
            if transition.operation == "vacuum":
                history.vacuum_completed_at = completion
            elif transition.operation == "mop":
                history.mop_completed_at = completion
        if completion is not None and transition.set_room_cleaning_completion:
            history.cleaning_completed_at = completion
        if completion is not None and transition.stage_completed:
            history.last_stage_outcome = "completed"
            history.last_stage_reason = transition.recovery_audit.reason
            history.last_stage_at = completion
            history.last_stage_summary = f"{transition.operation} completed"

        if transition.remove_occurrence:
            self.state.occurrences.pop(transition.room_id, None)
        elif transition.updated_occurrence is not None:
            self.state.occurrences[transition.room_id] = transition.updated_occurrence
        if transition.remove_water_confirmation and transition.occurrence_id:
            self.state.water_confirmations.pop(transition.occurrence_id, None)
        if transition.clear_water_notification_episode:
            self.state.water_notification_episodes.pop(transition.room_id, None)
        if transition.duration_sample is not None:
            history.duration_samples.append(transition.duration_sample)
            history.duration_samples[:] = history.duration_samples[-50:]
        if transition.manual_audit is not None:
            self._record_manual_effect(transition.manual_audit)
        self._record_recovery_effect(transition.recovery_audit)

        self.state.active_jobs[transition.robot_registry_id] = None
        self._cancel_recovery_timer(transition.robot_entity_id)
        self._cancel_start_confirmation(transition.robot_entity_id)

    def _record_manual_effect(self, effect: ManualAuditEffect) -> None:
        self._record_manual_event(
            ManualAuditRecord(
                at=effect.at,
                robot_registry_id=effect.robot_registry_id,
                room_ids=effect.room_ids,
                operations=effect.operations,
                context_id=effect.context_id,
                mode=effect.mode,
                reason=effect.reason,
                confidence=effect.confidence,
                source=effect.source,
                outcome=effect.outcome,
                deferred=(
                    effect.deferred
                    if effect.source is None and effect.outcome == "completed"
                    else ()
                ),
            )
        )

    def _record_recovery_effect(self, effect: RecoveryAuditEffect) -> None:
        events = self.state.audit.recovery_events
        events.append(
            RecoveryAuditRecord(
                robot_registry_id=effect.robot_registry_id,
                room_ids=effect.room_ids,
                at=effect.at,
                reason=effect.reason,
            )
        )
        events[:] = events[-20:]

    async def _async_complete_job(
        self,
        robot_id: str,
        active: ActiveJob,
        completion: datetime,
        confidence: str,
    ) -> bool:
        """Complete a job unless the vacuum reports that no cleaning occurred."""

        if managed_clean_duration_failed(
            active.source,
            active.duration_source,
            active.measured_minutes,
            active.native_timer_elapsed,
        ):
            robot = self.discovery.robots.get(robot_id)
            room = self.discovery.rooms.get(active.room_id)
            if robot and room:
                detail = self._room_data(room.area_id)
                detail.last_stage_outcome = "failed"
                detail.last_stage_reason = "native_cleaning_zero_duration"
                detail.last_stage_at = completion
                _LOGGER.warning(
                    "Adaptive RoboVacs rejected zero-duration native clean: "
                    "robot=%s room=%s duration_source=%s measured_minutes=%s",
                    robot.entity_id,
                    room.name,
                    active.duration_source,
                    active.measured_minutes,
                )
                if robot.registry_id in self.state.robot_faults:
                    self._cancel_job(
                        robot_id, active, completion, "native_cleaning_zero_duration"
                    )
                else:
                    await self._async_latch_scheduler_fault(
                        robot,
                        room,
                        "native_cleaning_zero_duration",
                        "completion",
                        native_command_may_have_started=True,
                        outcome_uncertain=False,
                    )
                return False
        self._complete_job(robot_id, active, completion, confidence)
        return True

    def _apply_manual_deferral(
        self,
        robot_entity_id: str,
        area_ids: list[str],
        operations: list[CleaningOperation],
        completed_at: datetime,
    ) -> list[str]:
        if robot_entity_id not in self.discovery.robots:
            return []
        candidates = tuple(
            DeferralCandidate(
                area_id,
                operation,
                self._room_due(room, operation, completed_at),
            )
            for area_id in area_ids
            if (room := self.discovery.rooms.get(area_id)) is not None
            for operation in operations
        )
        effects = reduce_manual_deferrals(completed_at, candidates)
        for effect in effects:
            room = self.discovery.rooms[effect.room_id]
            self._set_room_deferral(
                room,
                effect.operation,
                effect.until,
                "manual_clean",
                completed_at,
            )
            self._set_room_deferral(
                room,
                "cleaning",
                effect.until,
                "manual_clean",
                completed_at,
            )
        return [f"{effect.room_id}:{effect.operation}" for effect in effects]

    def _record_manual_event(self, event: ManualAuditRecord) -> None:
        events = self.state.audit.manual_events
        events.append(event)
        events[:] = events[-50:]
