"""User-requested cleaning, cancellation, and entity read actions."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .discovery import DiscoveredRobot, DiscoverySnapshot
from .gateway import VacuumGateway
from .models import CleaningOperation, can_request_return_to_dock
from .planner import ScheduleCandidate
from .projections import ProjectionSource, robot_view, room_view
from .snapshots import RobotView, RoomView
from .state import ActiveJob, ManualAuditRecord, RobotHold, SchedulerState


def _now() -> datetime:
    from . import application

    return application._now()


class ApplicationActionsMixin:
    """Handle explicit user actions without bypassing physical safety gates."""

    hass: HomeAssistant
    entry: ConfigEntry
    state: SchedulerState
    discovery: DiscoverySnapshot
    gateway: VacuumGateway
    _closing: bool
    _lock: asyncio.Lock

    if TYPE_CHECKING:

        async def _async_save(self) -> None: ...

        def _notify_listeners(self) -> None: ...

        def _shutdown_started(self) -> bool: ...

        async def async_refresh_discovery(self, *, notify: bool = True) -> None: ...

        def _observe_occupancy(self, now: datetime) -> None: ...

        async def _async_reconcile_jobs(self, now: datetime) -> None: ...

        def _refresh_robot_readiness(self, now: datetime) -> None: ...

        def _apply_manual_deferral(
            self,
            robot_entity_id: str,
            area_ids: list[str],
            operations: list[CleaningOperation],
            completed_at: datetime,
        ) -> list[str]: ...

        def _record_manual_event(self, event: ManualAuditRecord) -> None: ...

        def robot_registry_id(self, entity_id: str) -> str: ...

        def _set_held_job_phase(
            self,
            robot_id: str,
            active: ActiveJob,
            action: str,
            now: datetime,
        ) -> None: ...

        def _cancel_start_confirmation(self, robot_id: str) -> None: ...

        def _startup_state_settle_reason(self, now: datetime) -> str | None: ...

        def _manual_candidate(
            self,
            room: Any,
            now: datetime,
            mode: str,
            context_id: str | None,
            user_id: str | None,
        ) -> ScheduleCandidate: ...

        def _manual_robot_ready(self, robot: DiscoveredRobot) -> tuple[bool, str]: ...

        def _candidate_for_robot(
            self, candidate: ScheduleCandidate, robot: DiscoveredRobot
        ) -> ScheduleCandidate | None: ...

        def _robot_battery(self, robot: DiscoveredRobot) -> float | None: ...

        async def _async_prepare_occurrence(
            self,
            robot: DiscoveredRobot,
            candidate: ScheduleCandidate,
            now: datetime,
        ) -> tuple[ScheduleCandidate | None, str | None]: ...

        async def _async_refresh_pending_profile_if_needed(
            self, robot: DiscoveredRobot, candidate: ScheduleCandidate
        ) -> ScheduleCandidate: ...

        async def _async_dispatch(
            self,
            robot: DiscoveredRobot,
            candidate: ScheduleCandidate,
            now: datetime,
        ) -> tuple[bool, str]: ...

        @property
        def observe_only(self) -> bool: ...

        @property
        def party_mode(self) -> bool: ...

    async def async_record_manual_clean(
        self, robot_entity_id: str, area_ids: list[str], operations: list[str]
    ) -> dict[str, Any]:
        """Apply one-day deferrals only to known rooms due within 24 hours."""

        now = _now()
        typed_operations = [CleaningOperation(operation) for operation in operations]
        changed = self._apply_manual_deferral(
            robot_entity_id, area_ids, typed_operations, now
        )
        self._record_manual_event(
            ManualAuditRecord(
                at=now,
                robot_registry_id=self.robot_registry_id(robot_entity_id),
                room_ids=tuple(area_ids),
                operations=tuple(typed_operations),
                changed=tuple(changed),
            )
        )
        await self._async_save()
        self._notify_listeners()
        return {"changed": changed}

    async def async_stop_and_return_to_dock(
        self, robot_entity_id: str, *, context: Any = None
    ) -> dict[str, Any]:
        """Stop one robot and retain its active clean as cancelled until it docks."""

        if self._closing:
            return {"accepted": False, "reason": "coordinator shutting down"}
        async with self._lock:
            robot = self.discovery.robots.get(robot_entity_id)
            if robot is None:
                raise ValueError("Robot is not discovered by this config entry")
            state = self.hass.states.get(robot.entity_id)
            state_text = state.state if state else None
            if state_text in {None, "unavailable", "unknown"}:
                return {"accepted": False, "reason": "robot is unavailable"}
            if not can_request_return_to_dock(state_text):
                return {"accepted": True, "reason": "robot is already docked"}

            await self.gateway.async_return_to_dock(robot.entity_id, context)

            active = self.state.active_jobs.get(robot.registry_id)
            if active:
                now = _now()
                hold = self.state.robot_holds.setdefault(
                    robot.registry_id,
                    RobotHold(
                        reason="user_requested_return",
                        phase="cancelling",
                        held_at=now,
                    ),
                )
                hold.reason = "user_requested_return"
                hold.phase = "cancelling"
                hold.returning_at = now
                hold.last_observed_at = now
                self._set_held_job_phase(robot.entity_id, active, "cancelling", now)
                self._cancel_start_confirmation(robot.entity_id)
                await self._async_save()
                self._notify_listeners()
            return {"accepted": True, "reason": "return to dock requested"}

    async def async_manual_clean_room(
        self,
        area_id: str,
        mode: str = "configured",
        *,
        context_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Start one explicit dashboard room-clean override immediately."""

        if mode not in {"configured", "vacuum_only", "mop_only"}:
            raise ValueError(f"Unknown manual cleaning mode: {mode}")
        if self._closing:
            return {
                "accepted": False,
                "status": "rejected",
                "reason": "coordinator shutting down",
            }
        async with self._lock:
            now = _now()
            await self.async_refresh_discovery()
            self._observe_occupancy(now)
            await self._async_reconcile_jobs(now)
            self._refresh_robot_readiness(now)
            room = self.discovery.rooms.get(area_id)
            event = ManualAuditRecord(
                at=now,
                room_ids=(area_id,),
                context_id=context_id,
                user_id=user_id,
                mode=mode,
                source="manual_dashboard",
            )

            async def reject(reason: str) -> dict[str, Any]:
                self._record_manual_event(
                    replace(event, outcome="rejected", reason=reason)
                )
                await self._async_save()
                self._notify_listeners()
                return {"accepted": False, "status": "rejected", "reason": reason}

            if room is None:
                return await reject("room is not discovered by this config entry")
            if room.area_id in self.state.room_faults:
                return await reject("room dispatch blocked pending Repair")
            if self.observe_only:
                return await reject("observe-only mode")
            if self.party_mode:
                return await reject("party mode")
            if settle_reason := self._startup_state_settle_reason(_now()):
                return await reject(settle_reason)
            base_candidate = self._manual_candidate(
                room, now, mode, context_id, user_id
            )
            resolved: list[tuple[DiscoveredRobot, ScheduleCandidate]] = []
            readiness: list[str] = []
            for robot in self.discovery.robots.values():
                if robot.floor_id != room.floor_id:
                    continue
                ready, reason = self._manual_robot_ready(robot)
                if not ready:
                    readiness.append(f"{robot.name}: {reason}")
                    continue
                candidate = self._candidate_for_robot(base_candidate, robot)
                if candidate is not None:
                    resolved.append((robot, candidate))
            if not resolved:
                reason = (
                    "; ".join(readiness)
                    if readiness
                    else "no ready robot has a compatible cleaning profile"
                )
                return await reject(reason)
            resolved.sort(
                key=lambda item: (
                    self._robot_battery(item[0]) or 0,
                    item[0].entity_id,
                ),
                reverse=True,
            )
            robot, candidate = resolved[0]
            prior_occurrence = self.state.occurrences.pop(area_id, None)
            if prior_occurrence:
                self.state.water_confirmations.pop(prior_occurrence.occurrence_id, None)
            event = replace(
                event,
                robot_registry_id=robot.registry_id,
                operations=tuple(stage.operation for stage in candidate.new_stages),
            )
            self._record_manual_event(replace(event, outcome="requested"))

            prepared, message = await self._async_prepare_occurrence(
                robot, candidate, now
            )
            if prepared is None:
                occurrence = self.state.occurrences.get(area_id)
                if occurrence:
                    self._record_manual_event(
                        replace(
                            event,
                            outcome="awaiting_confirmation"
                            if self.state.water_confirmations.get(
                                occurrence.occurrence_id
                            )
                            else "accepted",
                            reason=message,
                        )
                    )
                    await self._async_save()
                    self._notify_listeners()
                    return {
                        "accepted": True,
                        "status": "pending",
                        "reason": message,
                        "robot_entity_id": robot.entity_id,
                    }
                self._record_manual_event(
                    replace(event, outcome="rejected", reason=message)
                )
                await self._async_save()
                self._notify_listeners()
                return {"accepted": False, "status": "rejected", "reason": message}

            dispatch_now = _now()
            self._observe_occupancy(dispatch_now)
            fresh = replace(prepared, evaluated_at=dispatch_now)
            robot_ready, robot_reason = self._manual_robot_ready(robot)
            fresh_resolved = (
                self._candidate_for_robot(fresh, robot) if robot_ready else None
            )
            if fresh_resolved is None:
                occurrence = self.state.occurrences.pop(area_id, None)
                if occurrence:
                    self.state.water_confirmations.pop(occurrence.occurrence_id, None)
                return await reject(
                    robot_reason
                    if not robot_ready
                    else "cleaning profile is no longer compatible"
                )
            fresh_resolved = await self._async_refresh_pending_profile_if_needed(
                robot, fresh_resolved
            )
            if prepared.water_confirmed:
                fresh_resolved = replace(fresh_resolved, water_confirmed=True)
            changed_global_gate = (
                "coordinator shutting down"
                if self._shutdown_started()
                else "observe-only mode"
                if self.observe_only
                else "party mode"
                if self.party_mode
                else None
            )
            if changed_global_gate:
                failed_occurrence = self.state.occurrences.pop(area_id, None)
                if failed_occurrence:
                    self.state.water_confirmations.pop(
                        failed_occurrence.occurrence_id, None
                    )
                return await reject(changed_global_gate)
            ok, dispatch_message = await self._async_dispatch(
                robot, fresh_resolved, dispatch_now
            )
            if not ok:
                self._record_manual_event(
                    replace(
                        event,
                        outcome="failed",
                        reason=dispatch_message,
                    )
                )
                if not self.state.active_jobs.get(robot.registry_id):
                    failed_occurrence = self.state.occurrences.pop(area_id, None)
                    if failed_occurrence:
                        self.state.water_confirmations.pop(
                            failed_occurrence.occurrence_id, None
                        )
            await self._async_save()
            self._notify_listeners()
            return {
                "accepted": ok,
                "status": "started" if ok else "failed",
                "reason": dispatch_message,
                "robot_entity_id": robot.entity_id,
            }

    def room_view(self, area_id: str) -> RoomView:
        """Build the current immutable presentation for one room."""

        return room_view(cast(ProjectionSource, self), area_id)

    def robot_view(self, entity_id: str) -> RobotView:
        """Build the current immutable presentation for one robot."""

        return robot_view(cast(ProjectionSource, self), entity_id)
