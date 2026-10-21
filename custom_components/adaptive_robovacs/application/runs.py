"""Managed run admission, durable reservations, and battery observations."""

from __future__ import annotations

import math
import secrets
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..discovery import DiscoveredRobot, DiscoveredRoom, DiscoverySnapshot
from ..models import (
    RUN_TURNAROUND_MINUTES,
    AdjacencyDecision,
    CleaningProgram,
    JobPhase,
    OccurrenceSource,
    ResolvedDailyWindow,
    learned_battery_rate,
    resolve_cleaning_profile,
    run_battery_fits,
)
from ..planner import (
    PlannedCandidateAssignment,
    PlanningInput,
    ScheduleCandidate,
    WholeSchedulePlan,
)
from ..run_planner import (
    PlannedCleaningRun,
    RunRoomOption,
    RunVacancy,
    build_run_queue,
    run_queue_block,
)
from ..run_state import BatteryUsageSample, CleaningRun, profile_key
from ..state import (
    ActiveJob,
    CleaningOccurrence,
    RobotSettings,
    RoomHistory,
    RoomSettings,
    SchedulerState,
)


class ApplicationRunsMixin:
    """Keep queue ownership separate from the single physical-job lifecycle."""

    state: SchedulerState
    discovery: DiscoverySnapshot
    hass: HomeAssistant

    if TYPE_CHECKING:

        async def _async_save(self, *, force: bool = False) -> None: ...
        def _room_data(self, area_id: str) -> RoomHistory: ...
        def _room_settings(self, room: DiscoveredRoom) -> RoomSettings: ...
        def _robot_settings(self, robot: DiscoveredRobot) -> RobotSettings: ...
        def _robot_battery(self, robot: DiscoveredRobot) -> float | None: ...
        def _effective_duration(
            self,
            room: DiscoveredRoom,
            operation: str,
            passes: int,
            robot_id: str | None = None,
        ) -> tuple[float, int]: ...
        def _adjacency_decision(
            self,
            room: DiscoveredRoom,
            now: datetime,
            duration_minutes: float | None = None,
        ) -> AdjacencyDecision: ...
        def _room_candidate(
            self,
            room: DiscoveredRoom,
            now: datetime,
            *,
            ignore_run_reservation: bool = False,
        ) -> tuple[ScheduleCandidate | None, str]: ...
        def _desired_window(self, room: DiscoveredRoom) -> ResolvedDailyWindow: ...
        def _candidate_for_robot(
            self, candidate: ScheduleCandidate, robot: DiscoveredRobot
        ) -> ScheduleCandidate | None: ...
        def _observe_occupancy(self, now: datetime) -> None: ...
        @property
        def observe_only(self) -> bool: ...
        @property
        def party_mode(self) -> bool: ...

    def _run_for_room(self, area_id: str) -> tuple[str, CleaningRun] | None:
        return next(
            (
                (key, run)
                for key, run in self.state.cleaning_runs.items()
                if area_id in run.rooms
            ),
            None,
        )

    def _run_occupancy_exempt(self, area_id: str) -> bool:
        reservation = self._run_for_room(area_id)
        occurrence = self.state.occurrences.get(area_id)
        return bool(
            reservation
            and occurrence
            and reservation[1].started_room_id == area_id
            and reservation[1].occurrences.get(area_id) == occurrence.occurrence_id
        )

    def _run_continuation(self, robot: DiscoveredRobot) -> bool:
        run = self.state.cleaning_runs.get(robot.registry_id)
        return bool(
            run
            and run.started
            and run.rooms
            and run_battery_fits(self._robot_battery(robot), run.battery_percent)
        )

    def _run_vacancy(self, room: DiscoveredRoom) -> RunVacancy:
        detail = self._room_data(room.area_id)
        settings = self._room_settings(room)
        return RunVacancy(
            detail.occupancy,
            detail.occupancy_source == "no_sensor",
            dt_util.as_local(detail.unoccupied_since)
            if detail.unoccupied_since
            else None,
            tuple(
                (dt_util.as_local(sample.started_at), sample.minutes)
                for sample in detail.occupancy_samples
            ),
            settings.occupancy_sensitivity_minutes,
            self.state.global_settings.forecast_confidence,
            settings.vacancy_fallback_enabled,
            settings.vacancy_fallback_minutes,
        )

    def _run_room_option(
        self, robot: DiscoveredRobot, candidate: ScheduleCandidate
    ) -> RunRoomOption | None:
        room = self.discovery.rooms.get(candidate.room_id)
        if room is None or candidate.source != OccurrenceSource.SCHEDULER:
            return None
        occurrence = self.state.occurrences.get(room.area_id)
        stages = (
            tuple(occurrence.stages[occurrence.current_stage :])
            if occurrence
            else candidate.new_stages
        )
        if not stages:
            return None
        durations: list[float] = []
        energy = 0.0
        learned = True
        counts: list[int] = []
        for index, stage in enumerate(stages):
            if not robot.adapter_capabilities.supports(stage.operation, stage.passes):
                return None
            profile = stage.cleaning_profile or resolve_cleaning_profile(
                stage.operation,
                self._room_settings(room),
                self._robot_settings(robot),
                robot.adapter_capabilities,
            )
            if profile is None:
                return None
            duration, _ = self._effective_duration(
                room, stage.operation, stage.passes, robot.registry_id
            )
            active = self.state.active_jobs.get(robot.registry_id)
            if (
                index == 0
                and active
                and active.room_id == room.area_id
                and active.seen_cleaning
                and not active.recovery_crossed
                and active.observed_started_at is not None
            ):
                elapsed = max(
                    0,
                    (
                        candidate.evaluated_at - active.observed_started_at
                    ).total_seconds()
                    / 60,
                )
                duration = max(5, duration - elapsed)
            durations.append(duration)
            rate, count = learned_battery_rate(
                sample.percent_per_minute
                for sample in self.state.battery_samples.get(robot.registry_id, [])
                if sample.profile == profile_key(profile)
            )
            counts.append(count)
            if rate is None:
                learned = False
            else:
                energy += rate * (duration + RUN_TURNAROUND_MINUTES)
        window = self._desired_window(room)
        settings = self._room_settings(room)
        global_settings = self.state.global_settings
        neighbors = self._adjacency_decision(
            room, candidate.evaluated_at
        ).neighbor_area_ids
        return RunRoomOption(
            candidate,
            tuple(durations),
            energy if learned else None,
            min(counts),
            self._run_vacancy(room),
            tuple(self._run_vacancy(self.discovery.rooms[key]) for key in neighbors),
            settings.adjacency_mode,
            global_settings.adjacency_night_start,
            global_settings.adjacency_night_end,
            window.start,
            window.end,
            settings.ignore_desired_window or candidate.bypass_desired_window,
        )

    def _plan_cleaning_runs(
        self, plan: WholeSchedulePlan, inputs: tuple[PlanningInput, ...], now: datetime
    ) -> tuple[WholeSchedulePlan, tuple[PlannedCleaningRun, ...]]:
        """Extend established robot allocation, preserving other robots' seeds."""

        assigned = {item.candidate.room_id for item in plan.assignments}
        runs: list[PlannedCleaningRun] = []
        assignments: list[PlannedCandidateAssignment] = []
        blocks = dict(plan.blocks)
        robot_blocks: list[tuple[str, str, str]] = []
        by_room = {item.candidate.room_id: item for item in inputs}
        for assignment in plan.assignments:
            robot = self.discovery.robots[assignment.robot_id]
            if (
                robot.registry_id in self.state.cleaning_runs
                or assignment.candidate.manual_override
            ):
                assignments.append(assignment)
                continue
            seed = self._run_room_option(robot, assignment.candidate)
            if seed is None or seed.battery_percent is None:
                assignments.append(assignment)
                continue
            options = [seed]
            for base in plan.candidates:
                if base.room_id in assigned or self._run_for_room(base.room_id):
                    continue
                resolved = next(
                    (
                        decision.candidate
                        for decision in by_room[base.room_id].decisions
                        if decision.eligibility.robot_id == robot.entity_id
                        and decision.eligibility.eligible
                    ),
                    None,
                )
                option = self._run_room_option(robot, resolved) if resolved else None
                if option is not None and option.battery_percent is not None:
                    options.append(option)
            queue, rejected = build_run_queue(
                tuple(options), dt_util.as_local(now), self._robot_battery(robot)
            )
            blocks.update(rejected)
            robot_blocks.extend(
                (area_id, robot.entity_id, reason) for area_id, reason in rejected
            )
            if not queue:
                continue
            for option in queue:
                assigned.add(option.candidate.room_id)
                blocks.pop(option.candidate.room_id, None)
            runs.append(PlannedCleaningRun(robot.entity_id, queue))
            assignments.append(
                PlannedCandidateAssignment(robot.entity_id, queue[0].candidate)
            )
        return replace(
            plan,
            assignments=tuple(assignments),
            blocks=tuple(blocks.items()),
            robot_blocks=tuple(robot_blocks),
        ), tuple(runs)

    async def _async_start_cleaning_run(
        self, planned: PlannedCleaningRun, now: datetime
    ) -> ScheduleCandidate | None:
        """Revalidate and persist every occurrence before the first start command."""

        robot = self.discovery.robots[planned.robot_id]
        self._observe_occupancy(now)
        fresh: list[RunRoomOption] = []
        for item in planned.rooms:
            room = self.discovery.rooms.get(item.candidate.room_id)
            candidate, _ = (
                self._room_candidate(room, now) if room else (None, "room unavailable")
            )
            resolved = (
                self._candidate_for_robot(candidate, robot) if candidate else None
            )
            option = self._run_room_option(robot, resolved) if resolved else None
            if option:
                fresh.append(option)
        queue, _ = build_run_queue(
            tuple(fresh), dt_util.as_local(now), self._robot_battery(robot)
        )
        if not queue:
            return None
        refs: dict[str, str] = {}
        for option in queue:
            candidate = option.candidate
            occurrence = self.state.occurrences.get(candidate.room_id)
            if occurrence is None:
                occurrence = CleaningOccurrence(
                    secrets.token_hex(12),
                    candidate.room_id,
                    robot.registry_id,
                    None,
                    candidate.program or CleaningProgram.VACUUM_ONLY,
                    list(candidate.new_stages),
                    candidate.due_at,
                    now,
                    robot.adapter_id,
                    robot.adapter_schema_version,
                )
                self.state.occurrences[candidate.room_id] = occurrence
            refs[candidate.room_id] = occurrence.occurrence_id
        run = CleaningRun(
            secrets.token_hex(12),
            now,
            list(refs),
            refs,
            expected_minutes=sum(item.duration_minutes for item in queue),
            battery_percent=sum(item.battery_percent or 0 for item in queue),
            estimated_end_at=now
            + timedelta(minutes=sum(item.duration_minutes for item in queue)),
        )
        self.state.cleaning_runs[robot.registry_id] = run
        await self._async_save(force=True)
        return queue[0].candidate

    def _reconcile_cleaning_runs(self, now: datetime) -> None:
        """Only trim reservations; observed jobs retain ownership of completion."""

        for registry_id, run in tuple(self.state.cleaning_runs.items()):
            robot = next(
                (
                    item
                    for item in self.discovery.robots.values()
                    if item.registry_id == registry_id
                ),
                None,
            )
            if (
                not robot
                or self.observe_only
                or self.party_mode
                or registry_id in self.state.robot_holds
                or registry_id in self.state.robot_faults
            ):
                self.state.cleaning_runs.pop(registry_id, None)
                continue
            active = self.state.active_jobs.get(registry_id)
            if active and (active.source != "scheduler" or active.interrupted):
                self.state.cleaning_runs.pop(registry_id, None)
                continue
            options: list[RunRoomOption] = []
            for area_id in tuple(run.rooms):
                candidate: ScheduleCandidate | None
                occurrence = self.state.occurrences.get(area_id)
                if (
                    occurrence is None
                    or occurrence.occurrence_id != run.occurrences[area_id]
                    or occurrence.current_stage >= len(occurrence.stages)
                ):
                    run.remove(area_id, "program completed or replaced")
                    continue
                if area_id == run.started_room_id or (
                    active and active.room_id == area_id
                ):
                    # Running stages cannot use the pending-candidate gate.
                    candidate = ScheduleCandidate(
                        area_id,
                        robot.floor_id,
                        occurrence.stages[occurrence.current_stage].operation,
                        occurrence.scheduled_at,
                        1,
                        "current room",
                        0,
                        0,
                        1,
                        occurrence,
                        now,
                        False,
                        True,
                        False,
                        OccurrenceSource.SCHEDULER,
                        stage_index=occurrence.current_stage,
                    )
                else:
                    room = self.discovery.rooms.get(area_id)
                    # Bypass only the reservation, retaining normal room gates.
                    candidate = self._queued_candidate(room, now) if room else None
                option = self._run_room_option(robot, candidate) if candidate else None
                if option is None or (
                    area_id != run.started_room_id
                    and option.vacancy.occupancy != "unoccupied"
                ):
                    if not active or active.room_id != area_id:
                        run.remove(
                            area_id, "pending room is no longer clear or compatible"
                        )
                    continue
                options.append(option)
            while options:
                reason = run_queue_block(
                    tuple(options),
                    dt_util.as_local(now),
                    self._robot_battery(robot),
                    current_room_id=run.started_room_id,
                )
                if reason is None:
                    break
                tail = options[-1]
                if active and active.room_id == tail.candidate.room_id:
                    # Finish the physical stage, then replan after recharge.
                    self.state.cleaning_runs.pop(registry_id, None)
                    break
                run.remove(tail.candidate.room_id, reason)
                options.pop()
            run.expected_minutes = sum(item.duration_minutes for item in options)
            run.estimated_end_at = now + timedelta(minutes=run.expected_minutes)
            run.battery_percent = sum(item.battery_percent or 0 for item in options)
            if not run.rooms:
                self.state.cleaning_runs.pop(registry_id, None)

    def _queued_candidate(
        self, room: DiscoveredRoom, now: datetime
    ) -> ScheduleCandidate | None:
        """Inspect a reservation using normal room policy without changing state."""

        return self._room_candidate(room, now, ignore_run_reservation=True)[0]

    def _run_dispatch_block(
        self, candidate: ScheduleCandidate, now: datetime
    ) -> str | None:
        reservation = self._run_for_room(candidate.room_id)
        if reservation is None:
            return None
        registry_id, run = reservation
        self._reconcile_cleaning_runs(now)
        if self.state.cleaning_runs.get(registry_id) is not run:
            return "cleaning run requires fresh planning before dispatch"
        if not run.rooms or run.rooms[0] != candidate.room_id:
            return "room is pending in another cleaning run"
        robot = next(
            (
                item
                for item in self.discovery.robots.values()
                if item.registry_id == registry_id
            ),
            None,
        )
        option = self._run_room_option(robot, candidate) if robot else None
        if option is None or not robot:
            return "cleaning run is no longer compatible"
        return run_queue_block(
            (option,),
            dt_util.as_local(now),
            self._robot_battery(robot),
            current_room_id=run.started_room_id,
        )

    def _observe_run_battery(
        self, robot: DiscoveredRobot, active: ActiveJob, state_text: str
    ) -> None:
        if active.source not in {"scheduler", "manual_dashboard"}:
            return
        if state_text == "cleaning":
            run = self.state.cleaning_runs.get(robot.registry_id)
            if run and active.run_id == run.run_id and active.room_id == run.rooms[0]:
                run.started_room_id = active.room_id
                run.started = True
        battery = self._robot_battery(robot)
        valid = battery is not None and math.isfinite(battery) and 0 <= battery <= 100
        if (
            not valid
            or active.recovery_crossed
            or state_text in {"unknown", "unavailable", "paused", "error"}
        ):
            active.battery_sample_valid = False
            return
        assert battery is not None
        profile = active.cleaning_profile
        if state_text == "cleaning" and profile:
            vacuum_state = self.hass.states.get(robot.entity_id)
            attributes = getattr(vacuum_state, "attributes", {})
            if profile.fan_speed and attributes.get("fan_speed") != profile.fan_speed:
                active.battery_sample_valid = False
            for entity_id, expected in (
                (robot.profile.mode_select_entity_id, profile.mode),
                (robot.profile.mop_mode_select_entity_id, profile.mop_mode),
                (robot.profile.mop_intensity_select_entity_id, profile.mop_intensity),
            ):
                if entity_id and expected:
                    observed = self.hass.states.get(entity_id)
                    if observed is None or observed.state != expected:
                        active.battery_sample_valid = False
        if state_text == "cleaning" and not active.seen_cleaning:
            active.battery_start = battery
            active.battery_low = battery
            active.battery_last = battery

        elif active.seen_cleaning:
            if (
                active.battery_last is not None
                and battery > active.battery_last
                and state_text != "docked"
            ):
                active.battery_sample_valid = False
            active.battery_low = (
                min(active.battery_low, battery)
                if active.battery_low is not None
                else battery
            )
            active.battery_last = battery

    def _run_recharge_observed(
        self, robot: DiscoveredRobot, active: ActiveJob, state_text: str
    ) -> bool:
        """A robot's recharge/resume episode is never stage completion."""

        status_id = robot.adapter_capabilities.readiness_entity_id
        status = self.hass.states.get(status_id) if status_id else None
        if active.seen_cleaning and status and status.state == "waiting_to_charge":
            active.recharge_pending = True
            active.battery_sample_valid = False
            active.interrupted = True
            self.state.cleaning_runs.pop(robot.registry_id, None)
        if active.recharge_pending:
            if state_text == "cleaning" and (
                status is None or status.state != "waiting_to_charge"
            ):
                active.recharge_pending = False
            elif state_text in {"returning", "docked"}:
                active.phase = JobPhase.RECHARGING
                return True
        return False

    def _learn_run_battery(
        self, registry_id: str, active: ActiveJob, completion: datetime, confidence: str
    ) -> None:
        if (
            confidence != "observed"
            or not active.battery_sample_valid
            or active.recovery_crossed
            or active.interrupted
            or active.interruption_minutes
            or active.cleaning_profile is None
            or active.battery_start is None
            or active.battery_low is None
            or active.observed_started_at is None
        ):
            return
        minutes = (completion - active.observed_started_at).total_seconds() / 60
        used = active.battery_start - active.battery_low
        if minutes <= 0 or used <= 0:
            return
        samples = self.state.battery_samples.setdefault(registry_id, [])
        key = profile_key(active.cleaning_profile)
        samples.append(BatteryUsageSample(key, used / minutes, completion))
        # Bound each profile independently; profiles never train one another.
        matching = [item for item in samples if item.profile == key]
        if len(matching) > 50:
            samples.remove(matching[0])
