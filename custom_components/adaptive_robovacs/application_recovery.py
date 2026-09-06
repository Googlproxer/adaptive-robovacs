"""Restart recovery and recovery-timer orchestration for durable jobs."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .commands import EvaluateCommand, SchedulerCommand, SchedulerCommandResult
from .const import DEFAULT_EXPECTED_MINUTES
from .discovery import DiscoveredRobot, DiscoveredRoom, DiscoverySnapshot
from .jobs import active_rooms, cancellation_cooldown
from .models import (
    EvaluationCause,
    EvaluationMode,
    JobPhase,
    held_job_transition,
    map_recovery_hold_is_manual,
    offline_held_recovery_outcome,
    pending_completion_is_docked,
)
from .state import (
    ActiveJob,
    RecoveryAuditRecord,
    RobotHold,
    RoomHistory,
    RoomSettings,
    SchedulerState,
)

_LOGGER = logging.getLogger(__name__)


def _now() -> datetime:
    from . import application

    return application._now()


def async_track_point_in_utc_time(
    hass: HomeAssistant,
    action: Callable[[datetime], None],
    deadline: datetime,
) -> Callable[[], None]:
    from . import application

    return application._track_point(hass, action, deadline)


class ApplicationRecoveryMixin:
    """Recover persisted jobs exclusively from fresh physical observations."""

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

        async def _async_complete_job(
            self,
            robot_id: str,
            active: ActiveJob,
            completion: datetime,
            confidence: str,
        ) -> bool: ...

        def _cancel_job(
            self,
            robot_id: str,
            active: ActiveJob,
            now: datetime,
            reason: str,
        ) -> None: ...

        def _mark_observed_completion(
            self,
            robot_id: str,
            active: ActiveJob,
            completion: datetime,
            confidence: str = "observed",
            allow_sample: bool = True,
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

        def _terminal_completion_is_observed(
            self, robot: DiscoveredRobot | None
        ) -> bool: ...

        def _set_dock_completion_pending(
            self, robot_id: str, active: ActiveJob, docked_at: datetime
        ) -> None: ...

        def _mop_washing_is_observed(
            self, robot: DiscoveredRobot | None, active: ActiveJob | None
        ) -> bool: ...

        def _mark_mop_washing_started(
            self, robot: DiscoveredRobot, active: ActiveJob, now: datetime
        ) -> bool: ...

        def _cleaning_timer_minutes(self, robot_id: str) -> float | None: ...

    async def _async_recover_active_jobs(self) -> None:
        """Recover a persisted command checkpoint after a Home Assistant restart."""

        now = _now()
        robot_ids = (
            {robot.registry_id for robot in self.discovery.robots.values()}
            | set(self.state.active_jobs)
            | set(self.state.robot_holds)
        )
        for registry_id in robot_ids:
            robot = self.robot_for_registry_id(registry_id)
            entity_id = robot.entity_id if robot else registry_id
            active = self.state.active_jobs.get(registry_id)
            tracked_expected_minutes = active.expected_minutes if active else None
            if active:
                self._normalise_active_job(active, now)
                # Home Assistant did not observe the complete lifecycle while
                # it was offline. Keep cadence authoritative, but never train
                # the verified elapsed-duration model from this job.
                active.forecast_sample_eligible = False
                active.recovery_crossed = True
            state = self.hass.states.get(entity_id)
            state_text = state.state if state else "unavailable"
            hold = self.state.robot_holds.get(registry_id)

            if active and self._mop_washing_is_observed(robot, active):
                if robot:
                    self._mark_mop_washing_started(robot, active, now)
                continue
            if (
                active
                and active.phase == "mop_washing"
                and state_text not in {"cleaning", "returning"}
            ):
                # A restarted coordinator must keep the accepted Mop stage
                # while Roborock performs its dock wash before room cleaning.
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
                    self._mark_observed_completion(entity_id, active, completion)
                await self._async_complete_job(
                    entity_id, active, completion, confidence
                )
                self.state.robot_holds.pop(registry_id, None)
                continue

            # Retain v1.0.9 holds written before their richer state was added.
            if not hold and active and active.phase in {"paused", "error_waiting"}:
                hold = RobotHold(
                    reason=active.hold_reason or "paused",
                    phase="held",
                    held_at=active.held_at or now,
                    last_observed_at=active.last_observed_at or now,
                )
                self.state.robot_holds[registry_id] = hold

            action = self._reconcile_robot_hold(
                registry_id,
                state_text,
                active,
                now,
            )
            if action is not None:
                hold = self.state.robot_holds.get(registry_id) or hold
                outcome = offline_held_recovery_outcome(
                    state_text,
                    hold.phase if hold else None,
                    (
                        active.last_observed_at
                        if active
                        else hold.last_observed_at
                        if hold
                        else None
                    ),
                    tracked_expected_minutes,
                    now,
                )
                if outcome == "cancelled":
                    if active:
                        self._cancel_job(
                            entity_id,
                            active,
                            now,
                            "recovered_physical_cancellation",
                        )
                    self._apply_robot_cancellation_deferral(entity_id, now)
                    self.state.robot_holds.pop(registry_id, None)
                    continue
                if action == "held":
                    if active:
                        self._hold_active_job(entity_id, active, state_text, now)
                    continue
                if action == "resumed":
                    if active:
                        self._resume_held_job(entity_id, active, state, now)
                    continue
                if action in {"cancelling", "completion_pending"}:
                    if active:
                        self._set_held_job_phase(entity_id, active, action, now)
                    continue
                if action == "cancelled":
                    current_hold = self.state.robot_holds.get(registry_id)
                    cancelled_at = (
                        current_hold.returning_at if current_hold else None
                    ) or now
                    if active:
                        self._cancel_job(
                            entity_id, active, cancelled_at, "physical_cancelled"
                        )
                    self._apply_robot_cancellation_deferral(entity_id, cancelled_at)
                    self.state.robot_holds.pop(registry_id, None)
                    continue
                if action == "complete":
                    if active:
                        completion = active.cleaning_finished_at or now
                        if not active.cleaning_finished_at:
                            self._mark_observed_completion(
                                entity_id, active, completion
                            )
                        await self._async_complete_job(
                            entity_id, active, completion, "observed"
                        )
                    self.state.robot_holds.pop(registry_id, None)
                    continue
            if not active:
                continue
            if (
                active.source in {"scheduler", "manual_dashboard"}
                and not active.seen_cleaning
                and active.phase
                in {"dispatching", "accepted", "start_outcome_uncertain"}
                and state_text not in {"cleaning", "returning"}
            ):
                room = self.discovery.rooms.get(active.room_id)
                if robot and room and robot.registry_id not in self.state.robot_faults:
                    await self._async_latch_scheduler_fault(
                        robot,
                        room,
                        "start_outcome_uncertain",
                        "restart_recovery",
                        native_command_may_have_started=True,
                        outcome_uncertain=True,
                    )
                continue
            if state and state.state in {"cleaning", "returning"}:
                active.recovered_at = now
                if state.state == "returning":
                    # Returning is reliable evidence that an accepted room command
                    # did run, even if Home Assistant missed the cleaning transition.
                    # It is not, however, proof that dock servicing is final.
                    active.seen_cleaning = True
                    active.phase = JobPhase.RETURNING
                    active.docked_at = None
                    self._cancel_recovery_timer(entity_id)
                else:
                    active.phase = JobPhase.CLEANING
                    self._cancel_recovery_timer(entity_id)
                continue
            if active.seen_cleaning and state and state.state == "docked":
                # A dock snapshot obtained after an outage cannot establish
                # when docking occurred. Start a fresh observed dwell instead.
                active.recovered_at = now
                if self._terminal_completion_is_observed(robot):
                    self._mark_observed_completion(
                        entity_id,
                        active,
                        now,
                        "recovered_terminal_status",
                        allow_sample=False,
                    )
                    await self._async_complete_job(
                        entity_id, active, now, "recovered_terminal_status"
                    )
                else:
                    self._set_dock_completion_pending(entity_id, active, now)
                continue
            if active.seen_cleaning and state and state.state == "idle":
                self._set_recovery_waiting(entity_id, active, now)
                continue
            if state is None or state.state in {"unavailable", "unknown"}:
                self._set_recovery_waiting(entity_id, active, now)
                continue
            self.state.active_jobs[registry_id] = None
            self._cancel_recovery_timer(entity_id)
            self.state.audit.recovery_events.append(
                RecoveryAuditRecord(
                    robot_registry_id=registry_id,
                    at=now,
                    reason="unconfirmed checkpoint",
                )
            )
        self.state.audit.recovery_events = self.state.audit.recovery_events[-20:]
        await self._async_save()

    def _normalise_active_job(self, active: ActiveJob, now: datetime) -> None:
        """Backfill lifecycle fields for checkpoints written by older releases."""

        area_ids = self._active_rooms(active)
        if area_ids:
            active.room_id = area_ids[0]
            if not active.room_ids:
                active.room_ids = area_ids
        rooms = [
            self.discovery.rooms[area_id]
            for area_id in area_ids
            if area_id in self.discovery.rooms
        ]
        if active.source == "manual_home_assistant":
            fallback = (
                sum(self._room_settings(room).expected_minutes for room in rooms)
                or DEFAULT_EXPECTED_MINUTES
            )
        else:
            fallback = (
                self._room_settings(rooms[0]).expected_minutes
                if rooms
                else DEFAULT_EXPECTED_MINUTES
            )
        if active.expected_minutes is None:
            active.expected_minutes = fallback
        if active.last_observed_at is None:
            active.last_observed_at = (
                active.observed_started_at
                or active.accepted_at
                or active.started_at
                or now
            )
        if active.expected_end is None:
            started = (
                active.observed_started_at
                or active.accepted_at
                or active.started_at
                or now
            )
            active.expected_end = started + timedelta(minutes=active.expected_minutes)

    def _reconcile_robot_hold(
        self,
        robot_id: str,
        state_text: str,
        active: ActiveJob | None,
        now: datetime,
    ) -> str | None:
        """Keep observed pauses/errors durable and classify physical follow-up only."""

        hold = self.state.robot_holds.get(robot_id)
        # Selecting a robot-retained map is a maintenance operation, not a
        # cleaning lifecycle.  It can only be released by the explicit
        # map-selection confirmation service after mapping has been rechecked.
        if hold and map_recovery_hold_is_manual(hold.reason):
            if state_text not in {"unavailable", "unknown"}:
                hold.last_observed_at = now
            return "held"
        if state_text in {"paused", "error"}:
            reason = (
                "robot_error"
                if state_text == "error" or (hold and hold.reason == "robot_error")
                else "paused"
            )
            if not hold:
                hold = RobotHold(reason=reason, phase="held", held_at=now)
                self.state.robot_holds[robot_id] = hold
                if reason == "robot_error":
                    _LOGGER.error(
                        "Adaptive RoboVacs scheduler held after robot error: "
                        "robot=%s state=%s. The robot must physically resume or "
                        "return to its dock before scheduling can continue.",
                        robot_id,
                        state_text,
                    )
                else:
                    _LOGGER.info(
                        "Adaptive RoboVacs scheduler held while robot is paused: "
                        "robot=%s",
                        robot_id,
                    )
            else:
                hold.reason = reason
            hold.last_observed_at = now
            return "held"
        if not hold:
            return None

        if state_text not in {"unavailable", "unknown"}:
            hold.last_observed_at = now
        if (
            hold.reason == "user_requested_return"
            and hold.phase == "cancelling"
            and state_text != "docked"
        ):
            return "cancelling"
        action = held_job_transition(
            state_text,
            hold.phase,
            bool(active and active.completion_before_hold),
        )
        if action == "resumed":
            self.state.robot_holds.pop(robot_id, None)
            _LOGGER.info(
                "Adaptive RoboVacs scheduler hold released by observed physical "
                "resume: robot=%s",
                robot_id,
            )
        elif action in {"cancelling", "completion_pending"}:
            hold.phase = action
            hold.returning_at = now
        return action

    def _hold_active_job(
        self,
        robot_id: str,
        active: ActiveJob,
        state_text: str,
        now: datetime,
    ) -> bool:
        """Persist an interrupted job so an automatic idle state cannot complete it."""

        hold = self.state.robot_holds.get(self.robot_registry_id(robot_id))
        is_error = bool(hold and hold.reason == "robot_error") or state_text == "error"
        phase = (
            JobPhase.COMPLETION_HELD
            if active.cleaning_finished_at
            else (JobPhase.ERROR_WAITING if is_error else JobPhase.PAUSED)
        )
        changed = active.phase != phase
        active.phase = phase
        active.hold_reason = "robot_error" if is_error else "paused"
        active.held_at = active.held_at or now
        active.interruption_started_at = active.interruption_started_at or now
        active.interrupted = True
        if active.cleaning_finished_at:
            active.completion_before_hold = True
        self._cancel_recovery_timer(robot_id)
        if changed:
            _LOGGER.info(
                "Adaptive RoboVacs active room job held: robot=%s rooms=%s reason=%s",
                robot_id,
                self._active_rooms(active),
                active.hold_reason,
            )
        return changed

    def _set_held_job_phase(
        self,
        robot_id: str,
        active: ActiveJob,
        action: str,
        now: datetime,
    ) -> None:
        """Expose a held job's physical-return state without releasing it."""

        if action == "cancelling":
            active.phase = JobPhase.CANCELLING
            active.interrupted = True
            active.hold_reason = "physical_cancellation"
            active.cancelling_at = now
        else:
            active.phase = JobPhase.COMPLETION_HELD
            active.hold_reason = "completion_before_fault"
        self._cancel_recovery_timer(robot_id)

    def _resume_held_job(
        self, robot_id: str, active: ActiveJob, state: Any, now: datetime
    ) -> None:
        """Continue a held job only after the robot itself resumes cleaning."""

        active.hold_reason = None
        active.held_at = None
        interruption_started = active.interruption_started_at
        active.interruption_started_at = None
        if interruption_started:
            active.interruption_minutes += max(
                0,
                (now - interruption_started).total_seconds() / 60,
            )
        active.docked_at = None
        active.last_observed_at = now
        if not active.seen_cleaning:
            observed_start = state.last_changed if state else now
            active.observed_started_at = observed_start
            active.expected_end = observed_start + timedelta(
                minutes=active.expected_minutes or 0
            )
            timer = self._cleaning_timer_minutes(robot_id)
            if timer is not None and timer <= 1:
                active.timer_start = timer
        active.seen_cleaning = True
        active.phase = JobPhase.CLEANING
        self._cancel_start_confirmation(robot_id)
        self._cancel_recovery_timer(robot_id)

    def _apply_robot_cancellation_deferral(
        self, robot_id: str, cancelled_at: datetime
    ) -> list[str]:
        cooldown = cancellation_cooldown(
            robot_id in self.discovery.robots,
            cancelled_at,
        )
        if cooldown is not None:
            self.state.robot_cooldowns[self.robot_registry_id(robot_id)] = cooldown
        return []

    def _active_rooms(self, active: ActiveJob) -> list[str]:
        return list(active_rooms(active))

    def _set_recovery_waiting(
        self, robot_id: str, active: ActiveJob, recovered_at: datetime
    ) -> None:
        """Keep an offline job pending until a later physical observation."""

        active.phase = JobPhase.RECOVERY_WAITING
        active.recovered_at = recovered_at
        if active.expected_end:
            self._schedule_recovery_completion(robot_id, active.expected_end)

    def _cancel_recovery_timer(self, robot_id: str) -> None:
        """Remove an exact expected-end callback when live state supersedes it."""

        unsubscribe = self._recovery_timers.pop(robot_id, None)
        if unsubscribe:
            unsubscribe()

    def _schedule_recovery_completion(
        self, robot_id: str, expected_end: datetime
    ) -> None:
        """Re-evaluate a job when a physical-completion deadline is reached."""

        self._cancel_recovery_timer(robot_id)
        if expected_end <= _now():
            self._async_create_task(
                self.async_execute(
                    EvaluateCommand(
                        mode=EvaluationMode.DISPATCH,
                        cause=EvaluationCause.RECOVERY,
                        detail=f"recovery-end:{robot_id}",
                    )
                )
            )
            return

        @callback
        def reconcile_at_expected_end(_when: datetime) -> None:
            self._recovery_timers.pop(robot_id, None)
            self._async_create_task(
                self.async_execute(
                    EvaluateCommand(
                        mode=EvaluationMode.DISPATCH,
                        cause=EvaluationCause.RECOVERY,
                        detail=f"recovery-end:{robot_id}",
                    )
                )
            )

        self._recovery_timers[robot_id] = async_track_point_in_utc_time(
            self.hass, reconcile_at_expected_end, expected_end
        )
