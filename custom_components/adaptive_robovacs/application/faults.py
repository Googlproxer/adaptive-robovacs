"""Scoped scheduler-fault and Repair orchestration.

Fault acknowledgement is deliberately observation-only.  This component may
clear a durable fault after fresh discovery and preflight, but it never starts
a robot or bypasses the application's FIFO command transaction.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from ..commands import EvaluateCommand, SchedulerCommand, SchedulerCommandResult
from ..const import READY_CONFIRMATION_DELAY, START_CONFIRMATION_TIMEOUT
from ..discovery import DiscoveredRobot, DiscoveredRoom, DiscoverySnapshot
from ..dispatch import DispatchPipeline
from ..jobs import should_assume_native_app_clean
from ..models import (
    EvaluationCause,
    EvaluationMode,
    FaultCode,
    JobPhase,
    SchedulerHaltRecheckResult,
    StageStatus,
    detailed_status_is_dispatchable,
    map_recovery_hold_is_manual,
    ready_confirmation_elapsed,
    robot_hold_recheck_result,
    scheduler_halt_recheck_result,
)
from ..observations import HomeAssistantObserver
from ..repair_service import RepairService
from ..repairs_manager import fault_summary
from ..state import RoomHistory, RoomSettings, SchedulerFault, SchedulerState

_LOGGER = logging.getLogger(__name__)


def _application_now() -> datetime:
    """Use the application clock, including its deterministic test override."""

    from . import core

    return core._now()


def _track_deadline(
    hass: HomeAssistant,
    action: Callable[[datetime], None],
    deadline: datetime,
) -> Callable[[], None]:
    """Use the application's Home Assistant timer seam."""

    from . import core

    return core._track_point(hass, action, deadline)


class ApplicationFaultMixin:
    """Manage scoped safe faults and their Repair acknowledgement flows."""

    hass: HomeAssistant
    entry: ConfigEntry
    state: SchedulerState
    discovery: DiscoverySnapshot
    dispatch: DispatchPipeline
    repairs: RepairService
    _lock: asyncio.Lock
    _start_confirmation_timers: dict[str, Callable[[], None]]
    _startup_state_settle_until: datetime | None

    if TYPE_CHECKING:

        def _async_create_task(
            self, coro: Coroutine[Any, Any, Any], *, name: str | None = None
        ) -> asyncio.Task[Any] | None: ...

        async def async_execute(
            self, command: SchedulerCommand
        ) -> SchedulerCommandResult: ...

        async def _async_save(self) -> None: ...

        async def async_refresh_discovery(self, *, notify: bool = True) -> None: ...

        def _notify_listeners(self) -> None: ...

        def _reset_ready_confirmation(self, robot_id: str) -> None: ...

        def _reset_room_recovery_dock(self, registry_id: str) -> None: ...

        def _cancel_recovery_timer(self, robot_id: str) -> None: ...

        def _cancel_job(
            self, robot_id: str, active: Any, cancelled_at: datetime, reason: str
        ) -> None: ...

        def _resume_held_job(
            self, robot_id: str, active: Any, state: Any, now: datetime
        ) -> None: ...

        async def async_acknowledge_room_recovery(
            self, area_id: str, recovery_id: str
        ) -> dict[str, object]: ...

        async def async_acknowledge_retired_map(
            self, registry_id: str, held_at: str
        ) -> dict[str, object]: ...

        def _sync_room_recovery_issues(self) -> None: ...

        def _sync_retired_map_issues(self) -> None: ...

        async def async_evaluate(
            self, dry_run: bool = False, reason: str = "manual"
        ) -> dict[str, Any]: ...

        def _room_data(self, area_id: str) -> RoomHistory: ...

        def _room_settings(self, room: DiscoveredRoom) -> RoomSettings: ...

        def _recheck_candidate(self, room: DiscoveredRoom, now: datetime) -> Any: ...

        def _candidate_for_robot(
            self, candidate: Any, robot: DiscoveredRobot
        ) -> Any: ...

    def robot_for_registry_id(self, registry_id: str) -> DiscoveredRobot | None:
        """Resolve a stable registry ID through current discovery."""

        return next(
            (
                robot
                for robot in self.discovery.robots.values()
                if robot.registry_id == registry_id
            ),
            None,
        )

    def _sync_dispatch_fault_issues(self) -> None:
        """Synchronize Repairs from immutable typed inputs."""

        self.repairs.sync_dispatch_faults(
            self.state.robot_faults,
            self.state.room_faults,
            self.discovery.robots.values(),
            self.discovery.rooms,
        )

    def _sync_robot_hold_issues(self) -> None:
        """Keep every unresolved non-map hold visible as a scoped Repair."""

        observed_states = {
            robot.registry_id: (
                state.state
                if (state := self.hass.states.get(robot.entity_id)) is not None
                else None
            )
            for robot in self.discovery.robots.values()
        }
        self.repairs.sync_robot_holds(
            self.state.robot_holds,
            self.discovery.robots.values(),
            observed_states,
        )

    def _sync_two_pass_issues(self) -> None:
        """Synchronize two-pass capability Repairs."""

        self.repairs.sync_two_pass_issues(
            self.state.room_settings,
            self.discovery.rooms,
            self.discovery.robots.values(),
        )

    def _sync_cleaning_program_issues(self) -> None:
        """Synchronize room-program compatibility Repairs."""

        self.repairs.sync_cleaning_program_issues(
            self.state.room_settings,
            self.state.robot_settings,
            self.state.occurrences,
            self.discovery.rooms,
            self.discovery.robots.values(),
        )

    def scheduler_fault_view(self) -> dict[str, Any] | None:
        """Preserve the legacy singular view when exactly one fault exists."""

        faults = [*self.state.robot_faults.values(), *self.state.room_faults.values()]
        if len(faults) != 1:
            return None
        return self._fault_view(faults[0])

    def _fault_view(self, fault: SchedulerFault) -> dict[str, Any]:
        """Project one durable fault without exposing adapter errors."""

        robot = self.robot_for_registry_id(fault.robot_registry_id)
        room = self.discovery.rooms.get(fault.room_area_id)
        reason_code = fault.reason_code
        return {
            "failure_code": reason_code,
            "failure_summary": fault_summary(reason_code),
            "failure_since": fault.occurred_at.isoformat(),
            "failure_phase": fault.phase,
            "repair_active": True,
            "robot": robot.name if robot else None,
            "room": room.name if room else None,
        }

    def _fault_views(self, section: str) -> list[dict[str, Any]]:
        faults = (
            self.state.robot_faults
            if section == "robot_faults"
            else self.state.room_faults
        )
        return [self._fault_view(fault) for _, fault in sorted(faults.items())]

    def fault_affects_robot(self, robot: DiscoveredRobot) -> bool:
        return robot.registry_id in self.state.robot_faults

    def fault_affects_room(self, room: DiscoveredRoom) -> bool:
        return room.area_id in self.state.room_faults

    def robot_fault_view(self, robot: DiscoveredRobot) -> dict[str, Any] | None:
        fault = self.state.robot_faults.get(robot.registry_id)
        return self._fault_view(fault) if fault else None

    def room_fault_view(self, room: DiscoveredRoom) -> dict[str, Any] | None:
        fault = self.state.room_faults.get(room.area_id)
        return self._fault_view(fault) if fault else None

    async def _async_latch_scheduler_fault(
        self,
        robot: DiscoveredRobot,
        room: DiscoveredRoom,
        reason_code: str,
        phase: str,
        *,
        native_command_may_have_started: bool,
        outcome_uncertain: bool,
    ) -> None:
        """Persist a robot- or room-scoped fault before later dispatches."""

        room_scoped_codes = {
            "area_mapping_missing",
            "area_mapping_stale",
            "area_mapping_ambiguous",
            "area_mapping_recheck_required",
            "two_pass_no_longer_supported",
            "adapter_request_unsupported",
            "adapter_preflight_failed",
            "profile_validation_failed",
            "profile_option_unsupported",
            "profile_control_unavailable",
        }
        section = "room_faults" if reason_code in room_scoped_codes else "robot_faults"
        scope_key = room.area_id if section == "room_faults" else robot.registry_id
        faults = (
            self.state.room_faults
            if section == "room_faults"
            else self.state.robot_faults
        )
        if scope_key in faults:
            return
        occurred_at = _application_now()
        try:
            fault_code = FaultCode(reason_code)
        except ValueError:
            _LOGGER.error(
                "Adaptive RoboVacs received an unknown adapter fault code: "
                "robot=%s room=%s phase=%s code=%r",
                robot.entity_id,
                room.area_id,
                phase,
                reason_code,
            )
            fault_code = FaultCode.UNRECOGNIZED_ADAPTER_FAILURE
        faults[scope_key] = SchedulerFault(
            reason_code=fault_code,
            robot_registry_id=robot.registry_id,
            room_area_id=room.area_id,
            occurred_at=occurred_at,
            phase=phase,
            native_command_may_have_started=native_command_may_have_started,
            outcome_uncertain=outcome_uncertain,
        )
        active = self.state.active_jobs.get(robot.registry_id)
        if outcome_uncertain and active:
            active.phase = JobPhase.START_OUTCOME_UNCERTAIN
            active.last_observed_at = occurred_at
        else:
            if active and active.occurrence_id:
                occurrence = self.state.occurrences.get(room.area_id)
                stage_index = active.stage_index
                if (
                    occurrence
                    and isinstance(stage_index, int)
                    and stage_index < len(occurrence.stages)
                ):
                    occurrence.stages[stage_index].status = StageStatus.PENDING
                    occurrence.stages[stage_index].started_at = None
            self.state.active_jobs[robot.registry_id] = None
        if section == "room_faults":
            detail = self._room_data(room.area_id)
            detail.map_status = "error"
            detail.map_error = fault_summary(reason_code)
        self._cancel_start_confirmation(robot.entity_id)
        self._reset_ready_confirmation(robot.entity_id)
        await self._async_save()
        self._sync_dispatch_fault_issues()
        self._notify_listeners()

    async def async_recheck_and_resume(
        self, robot_registry_id: str | None = None
    ) -> SchedulerHaltRecheckResult:
        """Recheck scoped or global durable blockers without dispatching work."""

        if robot_registry_id is not None:
            results: list[SchedulerHaltRecheckResult] = []
            if robot_registry_id in self.state.robot_faults:
                results.append(await self._async_recheck_robot_fault(robot_registry_id))
            hold = self.state.robot_holds.get(robot_registry_id)
            if hold is not None:
                if map_recovery_hold_is_manual(hold.reason):
                    response = await self.async_acknowledge_retired_map(
                        robot_registry_id,
                        hold.held_at.isoformat() if hold.held_at else "",
                    )
                    results.append(
                        SchedulerHaltRecheckResult(
                            bool(response.get("cleared")),
                            str(response.get("reason", "holds_remaining")),  # type: ignore[arg-type]
                        )
                    )
                else:
                    results.append(
                        await self._async_recheck_robot_hold(robot_registry_id)
                    )
            if not results:
                return SchedulerHaltRecheckResult(False, "no_scheduler_halt")
            await self.async_evaluate(dry_run=True, reason="recheck-resume")
            unresolved = [result for result in results if not result.cleared]
            if unresolved:
                return unresolved[0]
            if len(results) == 1:
                return results[0]
            return SchedulerHaltRecheckResult(
                True,
                "all_holds_cleared",
                attempted=len(results),
                cleared_count=len(results),
            )

        robot_faults = tuple(sorted(self.state.robot_faults))
        robot_holds = tuple(sorted(self.state.robot_holds))
        room_faults = tuple(sorted(self.state.room_faults))
        room_recoveries = tuple(
            sorted(
                (area_id, recovery.recovery_id)
                for area_id, recovery in self.state.room_recoveries.items()
            )
        )
        attempted = (
            len(robot_faults)
            + len(robot_holds)
            + len(room_faults)
            + len(room_recoveries)
        )
        cleared_count = 0

        for registry_id in robot_faults:
            if (await self._async_recheck_robot_fault(registry_id)).cleared:
                cleared_count += 1
        for registry_id in robot_holds:
            hold = self.state.robot_holds.get(registry_id)
            if hold is None:
                cleared_count += 1
                continue
            if map_recovery_hold_is_manual(hold.reason):
                response = await self.async_acknowledge_retired_map(
                    registry_id,
                    hold.held_at.isoformat() if hold.held_at else "",
                )
                if response.get("cleared"):
                    cleared_count += 1
            elif (await self._async_recheck_robot_hold(registry_id)).cleared:
                cleared_count += 1
        for area_id in room_faults:
            if await self.async_recheck_room_fault(area_id):
                cleared_count += 1
        for area_id, recovery_id in room_recoveries:
            response = await self.async_acknowledge_room_recovery(area_id, recovery_id)
            if response.get("cleared"):
                cleared_count += 1

        self._sync_dispatch_fault_issues()
        self._sync_robot_hold_issues()
        self._sync_room_recovery_issues()
        self._sync_retired_map_issues()
        self._sync_two_pass_issues()
        self._sync_cleaning_program_issues()
        await self.async_evaluate(dry_run=True, reason="recheck-resume")
        remaining = self._durable_scheduler_blockers()
        return SchedulerHaltRecheckResult(
            not remaining,
            "all_holds_cleared" if not remaining else "holds_remaining",
            attempted=attempted,
            cleared_count=cleared_count,
            remaining=remaining,
        )

    def _durable_scheduler_blockers(self) -> tuple[str, ...]:
        """Return stable blocker identities for reset diagnostics."""

        return tuple(
            [f"robot_fault:{key}" for key in sorted(self.state.robot_faults)]
            + [f"robot_hold:{key}" for key in sorted(self.state.robot_holds)]
            + [f"room_fault:{key}" for key in sorted(self.state.room_faults)]
            + [f"room_recovery:{key}" for key in sorted(self.state.room_recoveries)]
        )

    async def _async_recheck_robot_fault(
        self, robot_registry_id: str
    ) -> SchedulerHaltRecheckResult:
        """Recheck one dispatch fault without changing unrelated blockers."""

        async with self._lock:
            faults = self.state.robot_faults
            fault = faults.get(robot_registry_id)
            if not fault:
                return SchedulerHaltRecheckResult(True, "all_holds_cleared")
            await self.async_refresh_discovery()
            robot = self.robot_for_registry_id(fault.robot_registry_id)
            room = self.discovery.rooms.get(fault.room_area_id)
            if robot is None or room is None:
                return SchedulerHaltRecheckResult(False, "recovery_target_unavailable")
            state = self.hass.states.get(robot.entity_id)
            state_text = state.state if state else None
            result = scheduler_halt_recheck_result(state_text)
            if not result.cleared:
                return result
            if state_text == "cleaning":
                if should_assume_native_app_clean(
                    state_text,
                    fault,
                    robot.registry_id,
                    self.state.active_jobs.get(robot.registry_id),
                ):
                    self._discard_unconfirmed_scheduler_job(robot, room)
                await self._async_clear_robot_fault(robot, room)
                return result
            self._discard_unconfirmed_scheduler_job(robot, room)
            await self._async_clear_robot_fault(robot, room)
            return result

    async def _async_recheck_robot_hold(
        self, robot_registry_id: str
    ) -> SchedulerHaltRecheckResult:
        """Release one hold only from fresh authoritative physical evidence."""

        async with self._lock:
            hold = self.state.robot_holds.get(robot_registry_id)
            if hold is None:
                return SchedulerHaltRecheckResult(True, "all_holds_cleared")
            if map_recovery_hold_is_manual(hold.reason):
                return SchedulerHaltRecheckResult(False, "holds_remaining")
            await self.async_refresh_discovery(notify=False)
            robot = self.robot_for_registry_id(robot_registry_id)
            if robot is None:
                self._sync_robot_hold_issues()
                return SchedulerHaltRecheckResult(False, "recovery_target_unavailable")
            state = self.hass.states.get(robot.entity_id)
            state_text = state.state if state else None
            observed = HomeAssistantObserver(self.hass).robot(robot)
            capabilities = robot.adapter_capabilities
            status_id = (
                capabilities.completion_status_entity_id
                or capabilities.readiness_entity_id
            )
            ready_states = (
                capabilities.terminal_completion_states
                if capabilities.completion_status_entity_id
                else capabilities.readiness_states
            )
            status = self.hass.states.get(status_id) if status_id else None
            now = _application_now()
            result = robot_hold_recheck_result(
                state_text,
                observed.error,
                terminal_ready=detailed_status_is_dispatchable(
                    status.state if status else None,
                    required=bool(status_id),
                    ready_states=ready_states,
                ),
                dock_stable=bool(
                    state
                    and state.last_changed
                    and ready_confirmation_elapsed(
                        state.last_changed, now, READY_CONFIRMATION_DELAY
                    )
                ),
                startup_settling=bool(
                    self._startup_state_settle_until
                    and now < self._startup_state_settle_until
                ),
            )
            if not result.cleared:
                self._sync_robot_hold_issues()
                self._notify_listeners()
                return result

            active = self.state.active_jobs.get(robot_registry_id)
            if state_text == "cleaning":
                self.state.robot_holds.pop(robot_registry_id, None)
                if active is not None:
                    self._resume_held_job(robot.entity_id, active, state, now)
            else:
                if active is not None:
                    self._cancel_job(
                        robot.entity_id,
                        active,
                        now,
                        "explicit_recheck_reset",
                    )
                self.state.robot_holds.pop(robot_registry_id, None)

            if active is not None:
                recovery = self.state.room_recoveries.get(active.room_id)
                if (
                    recovery
                    and recovery.robot_registry_id == robot_registry_id
                    and recovery.occurrence_id == active.occurrence_id
                ):
                    self.state.room_recoveries.pop(active.room_id, None)
            self._reset_room_recovery_dock(robot_registry_id)
            self._cancel_recovery_timer(robot.entity_id)
            self._cancel_start_confirmation(robot.entity_id)
            self._reset_ready_confirmation(robot.entity_id)
            await self._async_save()
            self.repairs.delete_robot_error_recovery(robot_registry_id)
            self._sync_robot_hold_issues()
            self._sync_room_recovery_issues()
            self._notify_listeners()
            return result

    def _discard_unconfirmed_scheduler_job(
        self, robot: DiscoveredRobot, room: DiscoveredRoom
    ) -> None:
        """Forget a scheduler room that never had a confirmed clean start."""

        active = self.state.active_jobs.get(robot.registry_id)
        if (
            not active
            or active.source not in {"scheduler", "manual_dashboard"}
            or active.seen_cleaning
        ):
            return
        occurrence = self.state.occurrences.get(room.area_id)
        stage_index = active.stage_index
        if (
            occurrence
            and isinstance(stage_index, int)
            and stage_index < len(occurrence.stages)
        ):
            occurrence.stages[stage_index].status = StageStatus.PENDING
            occurrence.stages[stage_index].started_at = None
        self.state.active_jobs[robot.registry_id] = None
        self._cancel_start_confirmation(robot.entity_id)

    async def _async_clear_robot_fault(
        self, robot: DiscoveredRobot, room: DiscoveredRoom
    ) -> None:
        """Clear one robot fault without changing a physical clean."""

        self.state.robot_faults.pop(robot.registry_id, None)
        self._reset_ready_confirmation(robot.entity_id)
        await self._async_save()
        self.repairs.delete_robot_dispatch_fault(robot.registry_id)
        self._notify_listeners()

    async def async_recheck_room_fault(self, area_id: str) -> bool:
        """Recheck one room configuration fault without starting a clean."""

        async with self._lock:
            fault = self.state.room_faults.get(area_id)
            if not fault:
                return True
            await self.async_refresh_discovery()
            room = self.discovery.rooms.get(area_id)
            if room is None:
                return False
            base = self._recheck_candidate(room, _application_now())
            for robot in self.discovery.robots.values():
                if robot.floor_id != room.floor_id:
                    continue
                candidate = self._candidate_for_robot(base, robot)
                if candidate is None:
                    continue
                try:
                    preflight = await self.dispatch.async_preflight(robot, candidate)
                    profile = await self.dispatch.async_validate_profile(
                        robot, candidate
                    )
                except Exception:
                    _LOGGER.exception(
                        "Adaptive RoboVacs room fault recheck failed: room=%s robot=%s",
                        room.name,
                        robot.entity_id,
                    )
                    continue
                if preflight.ready and profile.ready:
                    self.state.room_faults.pop(area_id, None)
                    detail = self._room_data(area_id)
                    detail.map_status = "mapped"
                    detail.map_error = None
                    await self._async_save()
                    self.repairs.delete_room_dispatch_fault(area_id)
                    self._notify_listeners()
                    return True
            return False

    async def async_recheck_room_compatibility(self, area_id: str) -> bool:
        """Recheck a saved two-pass room without sending a clean."""

        async with self._lock:
            await self.async_refresh_discovery()
            room = self.discovery.rooms.get(area_id)
            if room is None:
                return False
            if self._room_settings(room).vacuum_pass_count != 2:
                return True
            compatible = any(
                robot.floor_id == room.floor_id
                and robot.supports_area_clean
                and 2 in robot.adapter_capabilities.supported_pass_counts
                for robot in self.discovery.robots.values()
            )
            if compatible:
                self._sync_two_pass_issues()
            return compatible

    async def async_recheck_cleaning_program_compatibility(self, area_id: str) -> bool:
        """Verify that one current same-floor robot can execute a room program."""

        async with self._lock:
            await self.async_refresh_discovery()
            room = self.discovery.rooms.get(area_id)
            if room is None:
                return False
            base = self._recheck_candidate(room, _application_now())
            compatible = any(
                robot.floor_id == room.floor_id
                and robot.supports_area_clean
                and self._candidate_for_robot(base, robot) is not None
                for robot in self.discovery.robots.values()
            )
            if compatible:
                self._sync_cleaning_program_issues()
            return compatible

    def _cancel_start_confirmation(self, robot_id: str) -> None:
        unsubscribe = self._start_confirmation_timers.pop(robot_id, None)
        if unsubscribe:
            unsubscribe()

    def _schedule_start_confirmation(self, robot_id: str) -> None:
        """Schedule a bounded accepted-command confirmation check."""

        self._cancel_start_confirmation(robot_id)
        deadline = _application_now() + START_CONFIRMATION_TIMEOUT

        @callback
        def check_start(_timestamp: datetime) -> None:
            self._start_confirmation_timers.pop(robot_id, None)
            self._async_create_task(
                self.async_execute(
                    EvaluateCommand(
                        mode=EvaluationMode.PREVIEW,
                        cause=EvaluationCause.START_CONFIRMATION,
                        detail=f"start-confirmation:{robot_id}",
                    )
                )
            )

        self._start_confirmation_timers[robot_id] = _track_deadline(
            self.hass, check_start, deadline
        )
