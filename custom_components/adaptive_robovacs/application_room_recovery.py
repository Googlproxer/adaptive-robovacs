"""Room interruption recovery, independent of dispatch/configuration faults.

Every release of physical ownership is saved before publishing the new state.
Repairs acknowledge retries; they never invoke the vacuum gateway.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from homeassistant.core import HomeAssistant, callback

from .commands import EvaluateCommand, SchedulerCommand, SchedulerCommandResult
from .const import READY_CONFIRMATION_DELAY
from .discovery import DiscoveredRobot, DiscoverySnapshot
from .jobs import active_rooms, interrupted_occurrence
from .models import (
    EvaluationCause,
    EvaluationMode,
    JobPhase,
    detailed_status_is_dispatchable,
    ready_confirmation_elapsed,
    room_recovery_dock_is_safe,
)
from .observations import HomeAssistantObserver
from .repair_service import RepairService
from .state import (
    ActiveJob,
    CleaningOccurrence,
    RecoveryAuditRecord,
    RobotHold,
    RoomRecovery,
    SchedulerState,
)
from .storage import SchedulerStore


def _now() -> datetime:
    from .application import _now as application_now

    return application_now()


def matching_occurrence(
    state: SchedulerState, registry_id: str, active: ActiveJob
) -> CleaningOccurrence | None:
    """Require exact single-room ownership before changing a saved stage."""

    occurrence = state.occurrences.get(active.room_id)
    index = active.stage_index
    if (
        active.source not in {"scheduler", "manual_dashboard"}
        or not active.seen_cleaning
        or active_rooms(active) != (active.room_id,)
        or occurrence is None
        or occurrence.room_id != active.room_id
        or occurrence.robot_registry_id != registry_id
        or occurrence.occurrence_id != active.occurrence_id
        or index is None
        or index != occurrence.current_stage
        or not 0 <= index < len(occurrence.stages)
        or occurrence.stages[index].operation != active.operation
        or occurrence.stages[index].status != "running"
    ):
        return None
    return occurrence


class ApplicationRoomRecoveryMixin:
    """Own room error recovery inside the application's serialized transaction."""

    hass: HomeAssistant
    state: SchedulerState
    discovery: DiscoverySnapshot
    storage: SchedulerStore
    repairs: RepairService
    _lock: asyncio.Lock
    _closing: bool
    _storage_safe_mode: bool
    _startup_state_settle_until: datetime | None
    _room_recovery_since: dict[str, datetime]
    _room_recovery_timers: dict[str, Callable[[], None]]

    if TYPE_CHECKING:

        def _async_create_task(
            self, coro: Coroutine[Any, Any, Any], *, name: str | None = None
        ) -> asyncio.Task[Any] | None: ...

        async def async_execute(
            self, command: SchedulerCommand
        ) -> SchedulerCommandResult: ...

        async def async_refresh_discovery(self, *, notify: bool = True) -> None: ...

        def _notify_listeners(self) -> None: ...

        def _cancel_recovery_timer(self, robot_id: str) -> None: ...

        def _cancel_start_confirmation(self, robot_id: str) -> None: ...

        def _reset_ready_confirmation(self, robot_id: str) -> None: ...

        def robot_for_registry_id(self, registry_id: str) -> DiscoveredRobot | None: ...

    def _reset_room_recovery_dock(self, registry_id: str) -> None:
        self._room_recovery_since.pop(registry_id, None)
        if unsubscribe := self._room_recovery_timers.pop(registry_id, None):
            unsubscribe()

    def _room_recovery_dock_confirmed(
        self, robot: DiscoveredRobot | None, now: datetime
    ) -> bool:
        if robot is None:
            return False
        observed = HomeAssistantObserver(self.hass).robot(robot)
        capabilities = robot.adapter_capabilities
        status_id = (
            capabilities.completion_status_entity_id or capabilities.readiness_entity_id
        )
        ready_states = (
            capabilities.terminal_completion_states
            if capabilities.completion_status_entity_id
            else capabilities.readiness_states
        )
        status = self.hass.states.get(status_id) if status_id else None
        terminal = detailed_status_is_dispatchable(
            status.state if status else None,
            required=bool(status_id),
            ready_states=ready_states,
        )
        if not room_recovery_dock_is_safe(
            observed.state,
            observed.error,
            terminal_ready=terminal,
            startup_settling=bool(
                self._startup_state_settle_until
                and now < self._startup_state_settle_until
            ),
        ):
            self._reset_room_recovery_dock(robot.registry_id)
            return False
        since = self._room_recovery_since.get(robot.registry_id)
        if since is None:
            self._room_recovery_since[robot.registry_id] = now

            @callback
            def check(_timestamp: datetime) -> None:
                self._room_recovery_timers.pop(robot.registry_id, None)
                self._async_create_task(
                    self.async_execute(
                        EvaluateCommand(
                            mode=EvaluationMode.PREVIEW,
                            cause=EvaluationCause.RECOVERY,
                            detail="room-error-dock-confirmation",
                        )
                    )
                )

            from .application import _track_point

            self._room_recovery_timers[robot.registry_id] = _track_point(
                self.hass, check, now + READY_CONFIRMATION_DELAY
            )
        return ready_confirmation_elapsed(since, now, READY_CONFIRMATION_DELAY)

    async def _async_commit_room_recovery(self, state: SchedulerState) -> None:
        # Do not expose an unpersisted release, including after a failed save.
        await self.storage.async_save(state)
        self.state = state

    def _sync_room_recovery_issues(self) -> None:
        self.repairs.sync_room_recoveries(
            self.state.room_recoveries,
            self.discovery.robots.values(),
            self.discovery.rooms,
        )

    async def _async_handle_room_error(
        self,
        registry_id: str,
        robot: DiscoveredRobot | None,
        active: ActiveJob | None,
        state_text: str,
        now: datetime,
    ) -> bool:
        """Intercept unresolved errors before legacy cancellation/completion."""

        if robot is None:
            self._reset_room_recovery_dock(registry_id)
        if active is None or active.source not in {"scheduler", "manual_dashboard"}:
            return False
        hold = self.state.robot_holds.get(registry_id)
        fault = self.state.robot_faults.get(registry_id)
        recovery = self.state.room_recoveries.get(active.room_id)
        if (
            self._storage_safe_mode
            or self._closing
            or active.cleaning_finished_at
            or active.completion_before_hold
            or active.phase == JobPhase.START_OUTCOME_UNCERTAIN
            or (fault and fault.outcome_uncertain)
            or (hold and hold.reason != "robot_error")
        ):
            self._reset_room_recovery_dock(registry_id)
            return False
        if not (
            active.hold_reason == "robot_error"
            or (hold and hold.reason == "robot_error")
            or (state_text == "error" and active.seen_cleaning)
            or (recovery and recovery.occurrence_id == active.occurrence_id)
        ):
            return False
        occurrence = matching_occurrence(self.state, registry_id, active)
        if state_text == "cleaning":
            self._reset_room_recovery_dock(registry_id)
            if (
                recovery
                and recovery.occurrence_id == active.occurrence_id
                and not recovery.detached_at
            ):
                recoveries = dict(self.state.room_recoveries)
                recoveries.pop(active.room_id)
                await self._async_commit_room_recovery(
                    replace(self.state, room_recoveries=recoveries)
                )
                self._sync_room_recovery_issues()
            self.repairs.delete_robot_error_recovery(registry_id)
            return False
        interrupted_at = active.held_at or (hold.held_at if hold else None) or now
        # Error holds are restored here even when an older version saved only
        # the active checkpoint. Never reinterpret an unresolved return as success.
        held_job = replace(
            active,
            phase=JobPhase.ERROR_WAITING,
            hold_reason="robot_error",
            held_at=interrupted_at,
            interruption_started_at=active.interruption_started_at or interrupted_at,
            interrupted=True,
            forecast_sample_eligible=False,
        )
        held = RobotHold(
            "robot_error", "held", held_at=interrupted_at, last_observed_at=now
        )
        if occurrence is None or (
            recovery
            and (
                recovery.occurrence_id != active.occurrence_id
                or recovery.robot_registry_id != registry_id
                or recovery.stage_index != active.stage_index
                or recovery.operation != active.operation
                or recovery.detached_at
            )
        ):
            if (
                active.phase != JobPhase.ERROR_WAITING
                or hold is None
                or hold.reason != "robot_error"
                or hold.held_at is None
                or active.held_at is None
            ):
                await self._async_commit_room_recovery(
                    replace(
                        self.state,
                        active_jobs={**self.state.active_jobs, registry_id: held_job},
                        robot_holds={**self.state.robot_holds, registry_id: held},
                    )
                )
            self._room_recovery_dock_confirmed(robot, now)
            self.repairs.set_robot_error_recovery(registry_id, interrupted_at, robot)
            return True
        if recovery is None:
            observed = HomeAssistantObserver(self.hass).robot(robot) if robot else None
            recovery = RoomRecovery(
                uuid4().hex,
                active.room_id,
                registry_id,
                occurrence.occurrence_id,
                occurrence.current_stage,
                active.operation,
                interrupted_at,
                observed.error.category if observed else "robot_error",
            )
            await self._async_commit_room_recovery(
                replace(
                    self.state,
                    active_jobs={**self.state.active_jobs, registry_id: held_job},
                    robot_holds={**self.state.robot_holds, registry_id: held},
                    room_recoveries={
                        **self.state.room_recoveries,
                        active.room_id: recovery,
                    },
                )
            )
        self.repairs.delete_robot_error_recovery(registry_id)
        if self._room_recovery_dock_confirmed(robot, now):
            holds = dict(self.state.robot_holds)
            holds.pop(registry_id, None)
            await self._async_commit_room_recovery(
                replace(
                    self.state,
                    robot_holds=holds,
                    active_jobs={**self.state.active_jobs, registry_id: None},
                    occurrences={
                        **self.state.occurrences,
                        active.room_id: interrupted_occurrence(
                            occurrence, recovery.stage_index
                        ),
                    },
                    room_recoveries={
                        **self.state.room_recoveries,
                        active.room_id: replace(recovery, detached_at=now),
                    },
                    audit=replace(
                        self.state.audit,
                        recovery_events=[
                            *self.state.audit.recovery_events,
                            RecoveryAuditRecord(
                                registry_id,
                                (active.room_id,),
                                now,
                                "room_error_detached",
                            ),
                        ][-20:],
                    ),
                )
            )
            self._reset_room_recovery_dock(registry_id)
            if robot:
                self._cancel_recovery_timer(robot.entity_id)
                self._cancel_start_confirmation(robot.entity_id)
                self._reset_ready_confirmation(robot.entity_id)
        self._sync_room_recovery_issues()
        return True

    async def async_acknowledge_room_recovery(
        self, area_id: str, recovery_id: str
    ) -> dict[str, object]:
        """Acknowledge only this episode; actual dispatch always happens later."""

        async with self._lock:
            if self._storage_safe_mode or self._closing:
                return {"cleared": False, "reason": "recovery_unavailable"}
            await self.async_refresh_discovery(notify=False)
            recovery = self.state.room_recoveries.get(area_id)
            if recovery is None:
                return {"cleared": True, "reason": "already_cleared"}
            if recovery.recovery_id != recovery_id:
                return {"cleared": False, "reason": "recovery_changed"}
            active = self.state.active_jobs.get(recovery.robot_registry_id)
            if recovery.detached_at is None or (
                active and active.occurrence_id == recovery.occurrence_id
            ):
                return {"cleared": False, "reason": "awaiting_safe_dock"}
            occurrence = self.state.occurrences.get(area_id)
            if (
                area_id not in self.discovery.rooms
                or self.robot_for_registry_id(recovery.robot_registry_id) is None
                or occurrence is None
                or occurrence.room_id != area_id
                or occurrence.occurrence_id != recovery.occurrence_id
                or occurrence.robot_registry_id != recovery.robot_registry_id
                or occurrence.current_stage != recovery.stage_index
                or not 0 <= recovery.stage_index < len(occurrence.stages)
                or occurrence.stages[recovery.stage_index].operation
                != recovery.operation
                or occurrence.stages[recovery.stage_index].status != "pending"
            ):
                return {"cleared": False, "reason": "recovery_target_unavailable"}
            recoveries = dict(self.state.room_recoveries)
            recoveries.pop(area_id)
            await self._async_commit_room_recovery(
                replace(
                    self.state,
                    room_recoveries=recoveries,
                    occurrences={
                        **self.state.occurrences,
                        area_id: replace(
                            occurrence,
                            manual_override=False,
                            bypass_desired_window=False,
                        ),
                    },
                )
            )
            self._sync_room_recovery_issues()
            self._notify_listeners()
            return {
                "cleared": True,
                "reason": "retry_allowed",
                "dispatch_started": False,
            }

    async def async_acknowledge_robot_error(
        self, registry_id: str, held_at: str
    ) -> dict[str, object]:
        """Abandon a legacy checkpoint only after explicit safe-dock confirmation."""

        async with self._lock:
            if self._storage_safe_mode or self._closing:
                return {"cleared": False, "reason": "recovery_unavailable"}
            await self.async_refresh_discovery(notify=False)
            hold = self.state.robot_holds.get(registry_id)
            active = self.state.active_jobs.get(registry_id)
            fault = self.state.robot_faults.get(registry_id)
            if hold is None:
                return {"cleared": True, "reason": "already_cleared"}
            if (
                hold.reason != "robot_error"
                or hold.held_at is None
                or hold.held_at.isoformat() != held_at
                or active is None
                or active.source not in {"scheduler", "manual_dashboard"}
                or active.phase == JobPhase.START_OUTCOME_UNCERTAIN
                or (fault and fault.outcome_uncertain)
                or matching_occurrence(self.state, registry_id, active) is not None
                or active.room_id in self.state.room_recoveries
            ):
                return {"cleared": False, "reason": "recovery_changed"}
            if not self._room_recovery_dock_confirmed(
                self.robot_for_registry_id(registry_id), _now()
            ):
                return {"cleared": False, "reason": "awaiting_safe_dock"}
            # A broken association cannot be credited or retried as a known
            # attempt. Remove only a positively identified owned occurrence;
            # normal room cadence may create a fresh one later.
            occurrences = dict(self.state.occurrences)
            occurrence = occurrences.get(active.room_id)
            if (
                occurrence
                and occurrence.occurrence_id == active.occurrence_id
                and occurrence.robot_registry_id == registry_id
            ):
                occurrences.pop(active.room_id)
            holds = dict(self.state.robot_holds)
            holds.pop(registry_id)
            await self._async_commit_room_recovery(
                replace(
                    self.state,
                    robot_holds=holds,
                    active_jobs={**self.state.active_jobs, registry_id: None},
                    occurrences=occurrences,
                    audit=replace(
                        self.state.audit,
                        recovery_events=[
                            *self.state.audit.recovery_events,
                            RecoveryAuditRecord(
                                registry_id,
                                active_rooms(active),
                                _now(),
                                "legacy_error_abandoned",
                            ),
                        ][-20:],
                    ),
                )
            )
            self._reset_room_recovery_dock(registry_id)
            robot = self.robot_for_registry_id(registry_id)
            if robot:
                self._cancel_recovery_timer(robot.entity_id)
                self._cancel_start_confirmation(robot.entity_id)
                self._reset_ready_confirmation(robot.entity_id)
            self.repairs.delete_robot_error_recovery(registry_id)
            self._notify_listeners()
            return {"cleared": True, "dispatch_started": False}
