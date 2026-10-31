"""Sparse cached water snapshots and checkpointed dock-wash verification."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from ..adapters.roborock import read_dock_water
from ..commands import (
    CheckDockWaterCommand,
    EvaluateCommand,
    SchedulerCommand,
    SchedulerCommandResult,
)
from ..discovery import DiscoveredRobot, DiscoverySnapshot
from ..dock_water_state import DockWaterVerification, RobotWaterRecord
from ..gateway import VacuumGateway
from ..models import (
    EvaluationCause,
    EvaluationMode,
    StageStatus,
    water_notification_faults,
)
from ..planner import ScheduleCandidate
from ..state import SchedulerState

_LOGGER = logging.getLogger(__name__)
DOCK_WATER_DELAY = timedelta(minutes=5)
DOCK_WATER_INTERVAL = timedelta(hours=1)
WASH_START_TIMEOUT = timedelta(minutes=2)
WASH_COMPLETION_TIMEOUT = timedelta(minutes=10)
WASH_STATES = frozenset({"washing_the_mop", "washing_the_mop_2"})


def _now() -> datetime:
    from .core import _now as application_now

    return application_now()


@dataclass(slots=True)
class DockWaterRuntime:
    """Transient deadlines; missed periodic snapshots are never replayed."""

    due: dict[str, datetime] = field(default_factory=dict)
    docked: set[str] = field(default_factory=set)
    deadline: datetime | None = None
    cancel: Callable[[], None] | None = None
    generation: int = 0


class ApplicationDockWaterMixin:
    hass: HomeAssistant
    entry: ConfigEntry
    state: SchedulerState
    discovery: DiscoverySnapshot
    gateway: VacuumGateway
    _lock: asyncio.Lock
    _dock_water_runtime: DockWaterRuntime

    if TYPE_CHECKING:

        async def _async_save(self, *, force: bool = False) -> None: ...
        async def async_execute(
            self, command: SchedulerCommand
        ) -> SchedulerCommandResult: ...
        def _async_create_task(
            self, coro: Coroutine[Any, Any, Any], *, name: str | None = None
        ) -> asyncio.Task[Any] | None: ...
        def _notify_listeners(self) -> None: ...
        def _shutdown_started(self) -> bool: ...
        def _robot_ready(
            self, robot: DiscoveredRobot, *, ignore_scheduler_fault: bool = False
        ) -> tuple[bool, str]: ...
        def _robot_technically_ready(
            self, robot: DiscoveredRobot, *, ignore_scheduler_fault: bool = False
        ) -> tuple[bool, str]: ...
        def _dispatch_adjacency_block_reason(
            self, candidate: ScheduleCandidate, now: datetime
        ) -> str | None: ...
        def _skip_occurrence_stage(
            self,
            area_id: str,
            stage_index: int,
            outcome: StageStatus,
            reason: str,
            when: datetime,
        ) -> bool: ...
        async def _async_send_mobile_notification(
            self, payload: dict[str, Any]
        ) -> tuple[int, int]: ...
        async def _async_clear_mobile_notification(self, tag: str) -> None: ...
        @property
        def observe_only(self) -> bool: ...
        @property
        def party_mode(self) -> bool: ...

    def _water_runtime(self) -> DockWaterRuntime:
        if not hasattr(self, "_dock_water_runtime"):
            self._dock_water_runtime = DockWaterRuntime()
        return self._dock_water_runtime

    def _water_robot_status(self, robot: DiscoveredRobot) -> str | None:
        entity_id = robot.adapter_capabilities.readiness_entity_id
        state = self.hass.states.get(entity_id or robot.entity_id)
        return state.state if state else None

    def _water_robot_docked(self, robot: DiscoveredRobot) -> bool:
        state = self.hass.states.get(robot.entity_id)
        if state is None or state.state != "docked":
            return False
        capabilities = robot.adapter_capabilities
        return (
            not capabilities.readiness_entity_id
            or self._water_robot_status(robot)
            in capabilities.terminal_completion_states
        )

    def _water_verification_pending(self, registry_id: str) -> bool:
        record = self.state.robot_water.get(registry_id)
        return bool(record and record.verification and record.verification.pending)

    def _sync_dock_water_checks(
        self, now: datetime, *, arm_timer: bool = True, due_only: bool = False
    ) -> bool:
        """Reuse existing robot observations, without reading any tank sensor."""

        runtime = self._water_runtime()
        live = set()
        changed = False
        for robot in self.discovery.robots.values():
            sources = robot.adapter_capabilities.dock_water
            if not sources.supported and not sources.onboard:
                continue
            key = robot.registry_id
            live.add(key)
            record = self.state.robot_water.get(key)
            verification = record.verification if record else None
            if (
                due_only
                and runtime.due.get(key, now + DOCK_WATER_INTERVAL) > now
                and not (
                    verification
                    and verification.pending
                    and verification.deadline <= now
                )
            ):
                continue
            docked = self._water_robot_docked(robot)
            if verification and verification.pending:
                prior = (verification.phase, verification.deadline)
                status = self.hass.states.get(
                    robot.adapter_capabilities.readiness_entity_id or robot.entity_id
                )
                observed_at = status.last_changed if status else None
                timeout = (
                    WASH_START_TIMEOUT
                    if verification.phase == "requested"
                    else WASH_COMPLETION_TIMEOUT
                )
                transition = (
                    verification.phase == "requested"
                    and self._water_robot_status(robot) in WASH_STATES
                ) or (verification.phase == "washing" and docked)
                if transition and (
                    observed_at is None
                    or observed_at < verification.requested_at
                    or observed_at > verification.requested_at + timeout
                ):
                    verification.phase = "failed"
                    verification.reason = "dock_water_check_timed_out"
                    self._skip_dock_water_mops(verification, now)
                elif self._water_robot_status(robot) in WASH_STATES:
                    if verification.phase != "washing":
                        verification.phase = "washing"
                        verification.deadline = (
                            verification.requested_at + WASH_COMPLETION_TIMEOUT
                        )
                elif docked and verification.phase == "washing":
                    verification.phase = "settling"
                    verification.deadline = now + DOCK_WATER_DELAY
                elif self._water_robot_status(robot) in {
                    "cleaning",
                    "segment_cleaning",
                    "zoned_cleaning",
                    "segment_mopping",
                }:
                    verification.phase = "failed"
                    verification.reason = "dock_water_check_interrupted"
                    self._skip_dock_water_mops(verification, now)
                changed = changed or prior != (
                    verification.phase,
                    verification.deadline,
                )
            if docked:
                if key not in runtime.docked:
                    runtime.docked.add(key)
                    runtime.due[key] = now + DOCK_WATER_DELAY
                    if verification and verification.phase == "settling":
                        verification.deadline = now + DOCK_WATER_DELAY
            else:
                runtime.docked.discard(key)
                runtime.due.pop(key, None)
        runtime.docked.intersection_update(live)
        for key in set(runtime.due) - live:
            runtime.due.pop(key)
        for key, record in self.state.robot_water.items():
            verification = record.verification
            if key not in live and verification and verification.pending:
                verification.phase = "failed"
                verification.reason = "dock_water_telemetry_unavailable"
                self._skip_dock_water_mops(verification, now)
                changed = True
        if arm_timer:
            self._arm_dock_water_timer()
        return changed

    def _arm_dock_water_timer(self) -> None:
        from .core import _track_point

        runtime = self._water_runtime()
        deadlines = list(runtime.due.values())
        deadlines.extend(
            record.verification.deadline
            for record in self.state.robot_water.values()
            if record.verification and record.verification.pending
        )
        deadline = min(deadlines, default=None)
        if deadline == runtime.deadline:
            return
        self._stop_dock_water_timer()
        if deadline is None or self._shutdown_started():
            return
        runtime.deadline = deadline
        generation = runtime.generation

        @callback
        def reached(_timestamp: datetime) -> None:
            if (
                self._shutdown_started()
                or runtime.deadline != deadline
                or runtime.generation != generation
            ):
                return
            runtime.generation += 1
            runtime.cancel = None
            runtime.deadline = None
            self._async_create_task(self.async_execute(CheckDockWaterCommand()))

        runtime.cancel = _track_point(self.hass, reached, deadline)

    def _stop_dock_water_timer(self) -> None:
        runtime = self._water_runtime()
        runtime.generation += 1
        if runtime.cancel:
            runtime.cancel()
        runtime.cancel = None
        runtime.deadline = None

    def _water_notification_tag(self, robot: DiscoveredRobot) -> str:
        return f"adaptive_robovacs_water_{self.entry.entry_id}_{robot.registry_id}"

    def _water_fault_is_mature(
        self, robot: DiscoveredRobot, fault: str, now: datetime
    ) -> bool:
        sources = robot.adapter_capabilities.dock_water
        entity_id = getattr(sources, fault)
        state = self.hass.states.get(entity_id) if entity_id else None
        if fault in {"clean", "dirty"} and (state is None or state.state != "on"):
            state = self.hass.states.get(sources.error) if sources.error else None
        return bool(state and now - state.last_changed >= timedelta(seconds=60))

    async def _async_sample_dock_water(
        self, robot: DiscoveredRobot, now: datetime
    ) -> None:
        sources = robot.adapter_capabilities.dock_water
        observed = read_dock_water(self.hass, sources)
        record = self.state.robot_water.setdefault(
            robot.registry_id, RobotWaterRecord()
        )
        record.status = observed
        record.last_snapshot_at = now
        faults = water_notification_faults(observed, record.notified_faults)
        if faults == record.notified_faults:
            return
        if faults and any(
            fault not in record.notified_faults
            and not self._water_fault_is_mature(robot, fault, now)
            for fault in faults
        ):
            self._water_runtime().due[robot.registry_id] = now + DOCK_WATER_DELAY
            return
        if not faults:
            await self._async_clear_mobile_notification(
                self._water_notification_tag(robot)
            )
            record.notified_faults = ()
            return
        actions = {
            "clean": "Refill and refit the clean-water tank.",
            "dirty": "Empty and refit the dirty-water tank.",
            "onboard": "Onboard water shortage reported; check the water supply.",
        }
        message = " ".join(actions[key] for key in faults)
        delivered, _ = await self._async_send_mobile_notification(
            {
                "title": f"{robot.name} water needs attention",
                "message": (
                    f"{message} Last reported conditions; tank status may stay "
                    "cached until washing starts."
                ),
                "data": {
                    "tag": self._water_notification_tag(robot),
                    "channel": "Adaptive RoboVacs - Water",
                },
            }
        )
        if delivered:
            record.notified_faults = faults

    def _skip_dock_water_mops(
        self, verification: DockWaterVerification, now: datetime
    ) -> None:
        for room_id, occurrence in tuple(self.state.occurrences.items()):
            if occurrence.occurrence_id not in verification.occurrence_ids:
                continue
            index = occurrence.current_stage
            if (
                index < len(occurrence.stages)
                and occurrence.stages[index].operation == "mop"
            ):
                self._skip_occurrence_stage(
                    room_id,
                    index,
                    StageStatus.SKIPPED_NO_WATER,
                    verification.reason or "dock_water_unavailable",
                    now,
                )

    def _queue_water_evaluation(self) -> None:
        self._async_create_task(
            self.async_execute(
                EvaluateCommand(
                    mode=EvaluationMode.DISPATCH,
                    cause=EvaluationCause.STAGE_TRANSITION,
                    detail="dock-water-verification-finished",
                    coalesce=True,
                )
            )
        )

    async def _async_check_dock_water(self) -> None:
        """One sparse timer command; routine snapshots never evaluate or wash."""

        async with self._lock:
            if self._shutdown_started():
                return
            now = _now()
            observations_changed = self._sync_dock_water_checks(
                now, arm_timer=False, due_only=True
            )
            runtime = self._water_runtime()
            changed = observations_changed
            resume = False
            for robot in self.discovery.robots.values():
                key = robot.registry_id
                record = self.state.robot_water.get(key)
                verification = record.verification if record else None
                if (
                    verification
                    and verification.pending
                    and verification.deadline <= now
                ):
                    succeeded = (
                        verification.phase == "settling"
                        and self._water_robot_docked(robot)
                        and robot.adapter_capabilities.dock_water.supported
                        and read_dock_water(
                            self.hass, robot.adapter_capabilities.dock_water
                        ).dock_ready
                    )
                    verification.phase = "succeeded" if succeeded else "failed"
                    verification.reason = (
                        None if succeeded else "dock_water_check_failed"
                    )
                    if not succeeded:
                        self._skip_dock_water_mops(verification, now)
                    changed = resume = True
                    await self._async_sample_dock_water(robot, now)
                    if self._water_robot_docked(robot):
                        runtime.due[key] = now + DOCK_WATER_INTERVAL
                if runtime.due.get(key, now + DOCK_WATER_INTERVAL) > now:
                    continue
                runtime.due[key] = now + DOCK_WATER_INTERVAL
                await self._async_sample_dock_water(robot, now)
                changed = True
            if changed:
                await self._async_save()
                self._notify_listeners()
            self._arm_dock_water_timer()
            if resume:
                self._queue_water_evaluation()

    async def _async_prepare_dock_water(
        self, robot: DiscoveredRobot, candidate: ScheduleCandidate, now: datetime
    ) -> bool:
        """Begin at most one native verification wash per scheduled occurrence/run."""

        sources = robot.adapter_capabilities.dock_water
        if not sources.supported:
            return True
        record = self.state.robot_water.get(robot.registry_id)
        if record and record.verification and record.verification.pending:
            return False
        observed = read_dock_water(self.hass, sources)
        if observed.dock_ready:
            return True
        occurrence = self.state.occurrences[candidate.room_id]
        run = self.state.cleaning_runs.get(robot.registry_id)
        attempt_key = f"run:{run.run_id}" if run else occurrence.occurrence_id
        if (
            record
            and record.verification
            and record.verification.attempt_key == attempt_key
        ):
            self._skip_occurrence_stage(
                candidate.room_id,
                candidate.stage_index,
                StageStatus.SKIPPED_NO_WATER,
                "dock_water_unavailable",
                now,
            )
            await self._async_save()
            self._queue_water_evaluation()
            return False
        refs = tuple(run.occurrences.values()) if run else (occurrence.occurrence_id,)
        record = self.state.robot_water.setdefault(
            robot.registry_id, RobotWaterRecord()
        )
        verification = DockWaterVerification(
            attempt_key, refs, now, now + WASH_START_TIMEOUT
        )
        record.verification = verification
        if (
            candidate.source != "scheduler"
            or not sources.washing
            or not sources.error
            or not observed.revalidation_eligible
        ):
            verification.phase = "failed"
            verification.reason = "dock_water_unavailable"
        elif (
            self._shutdown_started()
            or self.observe_only
            or self.party_mode
            or not self._robot_technically_ready(robot)[0]
            or self._dispatch_adjacency_block_reason(candidate, now)
        ):
            record.verification = None
            return False
        await self._async_save()
        if verification.pending:
            try:
                if (
                    self._shutdown_started()
                    or self.observe_only
                    or self.party_mode
                    or not self._robot_technically_ready(robot)[0]
                    or self._dispatch_adjacency_block_reason(candidate, _now())
                ):
                    verification.phase = "failed"
                    verification.reason = "dock_water_check_cancelled"
                else:
                    async with asyncio.timeout(35):
                        await self.gateway.async_start_dock_wash(robot)
            except Exception:
                _LOGGER.exception(
                    "Adaptive RoboVacs dock wash verification failed: "
                    "robot=%s attempt=%s",
                    robot.entity_id,
                    attempt_key,
                )
                verification.phase = "failed"
                verification.reason = "dock_water_check_rejected"
        if not verification.pending:
            self._skip_dock_water_mops(verification, now)
            self._queue_water_evaluation()
        self._sync_dock_water_checks(_now())
        await self._async_save()
        return False
