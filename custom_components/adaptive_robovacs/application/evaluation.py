"""Observe, plan, publish, revalidate, and dispatch one evaluation transaction."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ..const import EVENT_EVALUATION
from ..discovery import DiscoveredRobot, DiscoveredRoom, DiscoverySnapshot
from ..planner import (
    CandidateRobotDecision,
    PlanningInput,
    ScheduleCandidate,
    build_schedule_plan,
)
from ..state import FrozenJsonObject, RoomHistory, RoomSettings, SchedulerState

_LOGGER = logging.getLogger(__name__)


def _now() -> datetime:
    from . import core

    return core._now()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class ApplicationEvaluationMixin:
    """Run the serialized observe-to-dispatch evaluation transaction."""

    hass: HomeAssistant
    entry: ConfigEntry
    state: SchedulerState
    discovery: DiscoverySnapshot
    _closing: bool
    _lock: asyncio.Lock
    _startup_state_settle_until: datetime | None

    if TYPE_CHECKING:

        def _shutdown_started(self) -> bool: ...

        async def async_refresh_discovery(self, *, notify: bool = True) -> None: ...

        async def _async_save(self) -> None: ...

        def _notify_listeners(self) -> None: ...

        def _room_data(self, area_id: str) -> RoomHistory: ...

        def _room_settings(self, room: DiscoveredRoom) -> RoomSettings: ...

        def _expire_robot_cooldowns(self, now: datetime) -> None: ...

        def _observe_occupancy(self, now: datetime) -> None: ...

        async def _async_reconcile_jobs(self, now: datetime) -> None: ...

        def _refresh_robot_readiness(self, now: datetime) -> None: ...

        def _room_candidate(
            self, room: DiscoveredRoom, now: datetime
        ) -> tuple[ScheduleCandidate | None, str]: ...

        def _record_room_decision(
            self,
            room: DiscoveredRoom,
            reason: str,
            now: datetime,
            duration_minutes: float,
        ) -> None: ...

        def _robot_ready(
            self, robot: DiscoveredRobot, *, ignore_scheduler_fault: bool = False
        ) -> tuple[bool, str]: ...

        def _manual_robot_ready(self, robot: DiscoveredRobot) -> tuple[bool, str]: ...

        def _candidate_robot_diagnostics(
            self,
            candidate: ScheduleCandidate,
            readiness: Mapping[str, tuple[bool, str]] | None = None,
        ) -> tuple[CandidateRobotDecision, ...]: ...

        def _robot_battery(self, robot: DiscoveredRobot) -> float | None: ...

        def scheduler_fault_view(self) -> dict[str, Any] | None: ...

        def _candidate_for_robot(
            self, candidate: ScheduleCandidate, robot: DiscoveredRobot
        ) -> ScheduleCandidate | None: ...

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

        @property
        def scheduler_limited(self) -> bool: ...

    def legacy_deferral_report(self) -> list[dict[str, Any]]:
        """Return reviewable room deferrals whose source predates provenance."""

        report: list[dict[str, Any]] = []
        for room in self.discovery.rooms.values():
            detail = self._room_data(room.area_id)
            operations = [
                {
                    "operation": operation,
                    "until": _iso(deferral.until),
                }
                for operation, deferral in detail.deferrals.items()
                if deferral.source == "legacy_unknown"
            ]
            if operations:
                report.append(
                    {
                        "area_id": room.area_id,
                        "room": room.name,
                        "operations": operations,
                    }
                )
        return report

    async def async_clear_legacy_deferrals(self, area_ids: list[str]) -> dict[str, Any]:
        """Clear only user-selected legacy deferrals and never dispatch work."""

        cleared: list[str] = []
        async with self._lock:
            for area_id in area_ids:
                if area_id not in self.discovery.rooms:
                    continue
                detail = self._room_data(area_id)
                for operation, record in tuple(detail.deferrals.items()):
                    if record.source != "legacy_unknown":
                        continue
                    detail.deferrals.pop(operation, None)
                    cleared.append(f"{area_id}:{operation}")
            if cleared:
                await self._async_save()
                self._notify_listeners()
        return {"cleared": cleared, "dispatch_started": False}

    async def async_evaluate(
        self,
        dry_run: bool = False,
        reason: str = "manual",
    ) -> dict[str, Any]:
        """Refresh state, publish a safe preview, and optionally dispatch work."""

        if self._closing:
            return {
                **self.state.evaluation.last_preview.to_mapping(),
                "dispatches": ["coordinator shutting down"],
            }
        async with self._lock:
            if self._shutdown_started():
                return {
                    **self.state.evaluation.last_preview.to_mapping(),
                    "dispatches": ["coordinator shutting down"],
                }
            now = _now()
            await self.async_refresh_discovery(notify=False)
            self._expire_robot_cooldowns(now)
            self._observe_occupancy(now)
            await self._async_reconcile_jobs(now)
            self._refresh_robot_readiness(now)
            candidates: list[ScheduleCandidate] = []
            reasons: dict[str, str] = {}
            for room in self.discovery.rooms.values():
                candidate, block_reason = self._room_candidate(room, now)
                if candidate:
                    candidates.append(candidate)
                else:
                    reasons[room.area_id] = block_reason
                    if self._room_settings(room).enabled:
                        self._record_room_decision(
                            room,
                            block_reason,
                            now,
                            self._room_settings(room).expected_minutes,
                        )
            robot_ready = {
                robot.entity_id: self._robot_ready(robot)
                for robot in self.discovery.robots.values()
            }
            plan_inputs: list[PlanningInput] = []
            for ordinal, candidate in enumerate(candidates):
                diagnostics = self._candidate_robot_diagnostics(candidate, robot_ready)
                plan_inputs.append(
                    PlanningInput(
                        candidate=candidate,
                        decisions=diagnostics,
                        ordinal=ordinal,
                        battery_by_robot=tuple(
                            (
                                decision.eligibility.robot_id,
                                self._robot_battery(
                                    self.discovery.robots[decision.eligibility.robot_id]
                                ),
                            )
                            for decision in diagnostics
                        ),
                    )
                )

            plan = build_schedule_plan(tuple(plan_inputs))
            candidates = list(plan.candidates)
            assignments = [
                (self.discovery.robots[item.robot_id], item.candidate)
                for item in plan.assignments
            ]
            for _robot, candidate in assignments:
                room = self.discovery.rooms[candidate.room_id]
                self._record_room_decision(
                    room,
                    "assigned to compatible robot",
                    now,
                    candidate.duration_minutes,
                )
            for room_id, rejection in plan.blocks:
                candidate = next(item for item in candidates if item.room_id == room_id)
                reasons.setdefault(room_id, rejection)
                self._record_room_decision(
                    self.discovery.rooms[candidate.room_id],
                    rejection,
                    now,
                    candidate.duration_minutes,
                )

            preview = {
                "at": _iso(now),
                "reason": reason,
                "observe_only": self.observe_only,
                "party_mode": self.party_mode,
                "dispatch_halted": False,
                "dispatch_limited": self.scheduler_limited,
                "startup_state_settle_until": _iso(self._startup_state_settle_until),
                "scheduler_fault": self.scheduler_fault_view(),
                "candidates": [
                    {
                        "room": item.room_id,
                        "room_name": self.discovery.rooms[item.room_id].name,
                        "operation": (
                            item.operation if item.occurrence else "cleaning"
                        ),
                        "due_at": _iso(item.due_at),
                        "confidence": item.confidence,
                        "basis": item.reason,
                        "passes": item.passes,
                        "eligible": any(
                            decision.eligible for decision in item.robot_eligibility
                        ),
                        "robot_eligibility": [
                            {
                                "robot_entity_id": decision.robot_id,
                                "robot_name": decision.robot_name,
                                "eligible": decision.eligible,
                                "reason": decision.reason,
                            }
                            for decision in item.robot_eligibility
                        ],
                    }
                    for item in candidates
                ],
                "assignments": [
                    {
                        "robot": robot.entity_id,
                        "room": item.room_id,
                        "operation": item.operation,
                        "program": item.program,
                        "stage_index": item.stage_index,
                        "passes": item.passes,
                        "adapter_id": robot.adapter_id,
                        "cleaning_profile": (
                            item.resolved_profile.to_mapping()
                            if item.resolved_profile
                            else None
                        ),
                        "source": item.source,
                    }
                    for robot, item in assignments
                ],
                "blocks": reasons,
                "legacy_deferral_review": self.legacy_deferral_report(),
                "robots": {
                    robot_id: {"ready": ready, "reason": ready_reason}
                    for robot_id, (ready, ready_reason) in robot_ready.items()
                },
            }
            self.state.evaluation.last_evaluation_at = now
            self.state.evaluation.last_preview = FrozenJsonObject.from_mapping(preview)
            await self._async_save()

            dispatches: list[str] = []
            if (
                not dry_run
                and not self._shutdown_started()
                and not self.observe_only
                and not self.party_mode
            ):
                for robot, candidate in assignments:
                    if self._shutdown_started():
                        break
                    # Earlier assignments may have awaited service calls. Recheck
                    # every room and robot gate before creating an occurrence or
                    # sending a water-confirmation notification.
                    prepare_now = _now()
                    self._observe_occupancy(prepare_now)
                    prepared_room = self.discovery.rooms.get(candidate.room_id)
                    if prepared_room is None:
                        dispatches.append("waiting for unavailable room")
                        continue
                    prepare_candidate, prepare_reason = self._room_candidate(
                        prepared_room, prepare_now
                    )
                    robot_is_ready, robot_reason = (
                        self._manual_robot_ready(robot)
                        if candidate.manual_override
                        else self._robot_ready(robot)
                    )
                    prepare_resolved = (
                        self._candidate_for_robot(prepare_candidate, robot)
                        if prepare_candidate and robot_is_ready
                        else None
                    )
                    if prepare_resolved is None:
                        wait_reason = (
                            prepare_reason
                            if not prepare_candidate
                            else robot_reason
                            if not robot_is_ready
                            else (
                                "cleaning program or vacancy forecast is no "
                                "longer compatible"
                            )
                        )
                        dispatches.append(
                            f"waiting for {prepared_room.name}: {wait_reason}"
                        )
                        continue
                    candidate = prepare_resolved
                    (
                        prepared,
                        preparation_message,
                    ) = await self._async_prepare_occurrence(
                        robot, candidate, prepare_now
                    )
                    if prepared is None:
                        if preparation_message:
                            dispatches.append(preparation_message)
                        continue
                    # Service calls for an earlier assignment can take time. Refresh
                    # every physical safety gate immediately before this command so
                    # stage two never inherits stage one's eligibility.
                    dispatch_now = _now()
                    self._observe_occupancy(dispatch_now)
                    dispatch_room = self.discovery.rooms.get(candidate.room_id)
                    if dispatch_room is None:
                        dispatches.append("waiting for unavailable room")
                        continue
                    fresh_candidate, fresh_reason = self._room_candidate(
                        dispatch_room, dispatch_now
                    )
                    robot_is_ready, robot_reason = (
                        self._manual_robot_ready(robot)
                        if candidate.manual_override
                        else self._robot_ready(robot)
                    )
                    fresh_resolved = (
                        self._candidate_for_robot(fresh_candidate, robot)
                        if fresh_candidate and robot_is_ready
                        else None
                    )
                    if fresh_resolved is None:
                        wait_reason = (
                            fresh_reason
                            if not fresh_candidate
                            else robot_reason
                            if not robot_is_ready
                            else "cleaning program is no longer compatible"
                        )
                        dispatches.append(
                            f"waiting for {dispatch_room.name}: {wait_reason}"
                        )
                        continue
                    fresh_resolved = (
                        await self._async_refresh_pending_profile_if_needed(
                            robot, fresh_resolved
                        )
                    )
                    fresh_resolved = replace(
                        fresh_resolved,
                        water_confirmed=(
                            fresh_resolved.water_confirmed or prepared.water_confirmed
                        ),
                        ignore_water_readiness=(
                            fresh_resolved.ignore_water_readiness
                            or prepared.ignore_water_readiness
                        ),
                    )
                    ok, message = await self._async_dispatch(
                        robot, fresh_resolved, dispatch_now
                    )
                    dispatches.append(message)
                    if not ok:
                        _LOGGER.warning("Adaptive RoboVacs: %s", message)
                        continue
            elif self.scheduler_limited:
                dispatches.append("scheduler limited to unaffected robots and rooms")
            elif self.observe_only:
                dispatches.append("observe-only mode")
            elif self.party_mode:
                dispatches.append("party mode")
            self._notify_listeners()
            self.hass.bus.async_fire(
                EVENT_EVALUATION, {"entry_id": self.entry.entry_id, **preview}
            )
            return {**preview, "dispatches": dispatches}
