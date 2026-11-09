"""Apply robot-aware occupancy checkpoints without rescanning learned history."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant

from ..discovery import DiscoveredRobot, DiscoveredRoom, DiscoverySnapshot
from ..lifecycle import SchedulerRuntime
from ..models import (
    ROBOT_LOCATION_MAX_AGE,
    JobPhase,
    OccupancyAttribution,
    RobotLocation,
    RoomObservation,
    advance_cleaning_occupancy,
    attribute_cleaning_occupancy,
)
from ..observations import HomeAssistantObserver
from ..state import OccupancySample, OccupancyTransition, SchedulerState


class ApplicationOccupancyMixin:
    state: SchedulerState
    hass: HomeAssistant
    observer: HomeAssistantObserver
    discovery: DiscoverySnapshot
    lifecycle: SchedulerRuntime

    if TYPE_CHECKING:
        _occupancy_deadlines: dict[str, datetime]

    def _cleaning_occupancy_robots(
        self, area_id: str, now: datetime
    ) -> tuple[str, ...]:
        return tuple(
            sorted(
                robot_id
                for robot_id, session in self.state.cleaning_occupancy_sessions.items()
                if session.covers(area_id, now)
            )
        )

    def _cleaning_occupancy_block(self, area_id: str, now: datetime) -> str | None:
        for session in self.state.cleaning_occupancy_sessions.values():
            if session.covers(area_id, now):
                return (
                    "dock room occupancy cooldown"
                    if session.dock_area_id == area_id
                    and session.cooldown_until
                    and now < session.cooldown_until
                    else "robot cleaning session in room"
                )
        return None

    def _uses_occupancy_attribution(self, area_id: str) -> bool:
        history = self.state.room_history.get(area_id)
        return (
            area_id in self.observer.attribution_areas
            or bool(history and history.occupancy_learning_version == 3)
            or any(
                area_id in session.areas or area_id == session.dock_area_id
                for session in self.state.cleaning_occupancy_sessions.values()
            )
        )

    def _enable_cleaning_occupancy(self, area_id: str) -> None:
        history = self.state.room_history[area_id]
        if history.occupancy_learning_version < 3:
            # Older samples cannot distinguish long cleans from human arrivals.
            history.occupancy_samples.clear()
            history.occupancy_learning_version = 3

    def _expire_cleaning_occupancy(self, now: datetime) -> bool:
        expired = sorted(
            (session.deadline or now, robot_id, session)
            for robot_id, session in self.state.cleaning_occupancy_sessions.items()
            if session.deadline is not None and session.deadline <= now
        )
        for boundary, robot_id, session in expired:
            cooldown_ended = session.cooldown_until == boundary
            if cooldown_ended and session.docked_at is None:
                self.state.cleaning_occupancy_sessions[robot_id] = replace(
                    session, cooldown_until=None
                )
            elif not cooldown_ended:
                self.state.cleaning_occupancy_sessions[robot_id] = replace(
                    session, areas=(), source="unknown", location_expires_at=None
                )
            else:
                del self.state.cleaning_occupancy_sessions[robot_id]
            for area in {*session.areas, session.dock_area_id}:
                if area is not None and area in self.discovery.rooms:
                    room = self.discovery.rooms[area]
                    detail = self.state.room_history[area]
                    # Reconcile the old signal at its deadline before a later
                    # queued sensor transition is applied.
                    raw = (
                        detail.occupancy_attribution.raw
                        if detail.occupancy_attribution
                        else detail.occupancy
                    )
                    observed = RoomObservation(
                        raw, detail.occupancy_source, detail.unavailable_radars
                    )
                    self._observe_attributed_room(
                        room,
                        observed,
                        boundary,
                        boundary=boundary,
                        continuous=cooldown_ended,
                    )
        return bool(expired)

    def _observe_robot_cleaning_occupancy(
        self,
        robot: DiscoveredRobot,
        state: str | None,
        now: datetime,
        *,
        areas: tuple[str, ...] | None = None,
        observations: dict[str, RoomObservation] | None = None,
    ) -> bool:
        previous = self.state.cleaning_occupancy_sessions.get(robot.registry_id)
        if areas is None:
            job = self.state.active_jobs.get(robot.registry_id)
            areas = (
                tuple(job.room_ids)
                if job
                and job.phase
                in {
                    JobPhase.DISPATCHING,
                    JobPhase.ACCEPTED,
                    JobPhase.MOP_WASHING,
                    JobPhase.CLEANING,
                }
                else ()
            )
        areas = tuple(
            area
            for area in areas
            if area is not None
            and area in self.discovery.rooms
            and self.discovery.rooms[area].floor_id == robot.floor_id
        )
        dock = self.discovery.rooms.get(robot.dock_area_id or "")
        dock_area = dock.area_id if dock and dock.floor_id == robot.floor_id else None
        location = self.observer.locations.locations.get(robot.registry_id)
        fresh = bool(
            location
            and location.valid
            and location.moving
            and now - location.observed_at < ROBOT_LOCATION_MAX_AGE
        )
        next_session = advance_cleaning_occupancy(
            previous,
            state,
            now,
            areas=areas,
            dock_area_id=dock_area,
            location_area=location.area_id if fresh and location else None,
            location_expires_at=location.observed_at + ROBOT_LOCATION_MAX_AGE
            if fresh and location
            else None,
        )
        if (
            previous
            and previous.source == "location"
            and state in {"cleaning", "returning"}
            and not fresh
            and not areas
        ):
            next_session = replace(
                previous, areas=(), source="unknown", location_expires_at=None
            )
        if previous == next_session:
            return False
        if next_session is None:
            self.state.cleaning_occupancy_sessions.pop(robot.registry_id, None)
        else:
            self.state.cleaning_occupancy_sessions[robot.registry_id] = next_session
        if (
            previous
            and next_session
            and replace(next_session, location_expires_at=previous.location_expires_at)
            == previous
        ):
            # Passive map heartbeats extend freshness without replan/save/publish.
            self._sync_occupancy_deadline()
            return False
        affected = {
            area
            for session in (previous, next_session)
            if session
            for area in (*session.areas, session.dock_area_id)
            if area is not None and area in self.discovery.rooms
        }
        for area in affected:
            self._enable_cleaning_occupancy(area)
            room = self.discovery.rooms[area]
            self._observe_attributed_room(
                room,
                (observations or {}).get(area) or self.observer.room(room),
                now,
                boundary=now,
                continuous=state == "docked"
                or (
                    state in {"cleaning", "returning"}
                    and next_session is not None
                    and (next_session.source != "unknown" or previous is None)
                ),
            )
        self._sync_occupancy_deadline()
        return True

    def _sync_cleaning_occupancy(self, now: datetime) -> None:
        self._expire_cleaning_occupancy(now)
        for robot in self.discovery.robots.values():
            observed = self.hass.states.get(robot.entity_id)
            self._observe_robot_cleaning_occupancy(
                robot, observed.state if observed else None, now
            )

    def _restore_cleaning_occupancy(self, now: datetime) -> None:
        # Offline docking has no trustworthy timestamp. Only a previously
        # checkpointed dock cooldown survives; no vacancy anchor crosses a gap.
        self.state.cleaning_occupancy_sessions = {
            key: session
            for key, session in self.state.cleaning_occupancy_sessions.items()
            if session.docked_at is not None
            and session.cooldown_until is not None
            and session.cooldown_until > now
        }
        for history in self.state.room_history.values():
            history.unoccupied_since = None
            history.occupancy = "unresolved"
            history.occupancy_attribution = None

    def _prune_cleaning_occupancy(self) -> None:
        robots = {robot.registry_id: robot for robot in self.discovery.robots.values()}
        for robot_id, session in tuple(self.state.cleaning_occupancy_sessions.items()):
            robot = robots.get(robot_id)
            dock = self.discovery.rooms.get(robot.dock_area_id or "") if robot else None
            valid = bool(
                robot
                and session.dock_area_id
                == (dock.area_id if dock and dock.floor_id == robot.floor_id else None)
                and all(
                    area in self.discovery.rooms
                    and self.discovery.rooms[area].floor_id == robot.floor_id
                    for area in session.areas
                )
                and (
                    session.source != "location"
                    or robot_id in self.observer.location_sources
                )
            )
            if valid:
                continue
            del self.state.cleaning_occupancy_sessions[robot_id]
            for area in {*session.areas, session.dock_area_id}:
                history = self.state.room_history.get(area or "")
                if (
                    history
                    and history.occupancy_attribution
                    and history.occupancy_attribution.classification
                    in {"cleaning", "dock_cooldown"}
                ):
                    history.occupancy_attribution = None
                    history.occupancy = "unresolved"
                    history.unoccupied_since = None
                    self._occupancy_deadlines.pop(area or "", None)

    def _observe_attributed_room(
        self,
        room: DiscoveredRoom,
        observed: RoomObservation,
        now: datetime,
        *,
        boundary: datetime | None = None,
        continuous: bool = True,
    ) -> bool:
        detail = self.state.room_history[room.area_id]
        previous = detail.occupancy_attribution or OccupancyAttribution(
            raw=detail.occupancy,
            vacancy_since=detail.unoccupied_since,
            classification="human" if detail.occupancy == "occupied" else "clear",
            started_at=now if detail.occupancy == "occupied" else None,
        )
        if detail.occupancy_attribution and detail.occupancy_source != observed.source:
            previous = OccupancyAttribution(raw=observed.occupancy)
            continuous = False
        robots = self._cleaning_occupancy_robots(room.area_id, now)
        cooldowns = [
            session.cooldown_until
            for robot_id, session in self.state.cleaning_occupancy_sessions.items()
            if robot_id in robots
            and room.area_id == session.dock_area_id
            and session.cooldown_until is not None
            and session.cooldown_until > now
        ]
        result = attribute_cleaning_occupancy(
            previous,
            observed.occupancy,
            now,
            self.observer.locations.for_room(room.area_id, now),
            robots=robots,
            cooldown_until=max(cooldowns, default=None),
            boundary=boundary,
            continuous=continuous,
        )
        checkpoint = result.checkpoint
        changed = (
            detail.occupancy != result.occupancy
            or detail.unoccupied_since != result.clear_since
            or detail.occupancy_attribution != checkpoint
        )
        masked = {"cleaning", "dock_cooldown"}
        if (
            previous.raw != observed.occupancy
            and previous.classification not in masked
            and checkpoint.classification not in masked
        ):
            detail.occupancy_transitions.append(OccupancyTransition(now))
            detail.occupancy_transitions = detail.occupancy_transitions[-4096:]
        if result.sample is not None:
            started, ended = result.sample
            minutes = int((ended - started).total_seconds() / 60)
            if minutes > 0:
                detail.occupancy_samples.append(OccupancySample(started, minutes))
            if previous.classification in masked:
                detail.occupancy_transitions.append(
                    OccupancyTransition(ended, (ended - started).total_seconds())
                )
                detail.occupancy_transitions = detail.occupancy_transitions[-4096:]
        detail.occupancy_attribution = checkpoint
        detail.occupancy = result.occupancy
        detail.unoccupied_since = result.clear_since
        detail.occupancy_source = observed.source
        detail.unavailable_radars = observed.unavailable_radars
        deadlines = getattr(self, "_occupancy_deadlines", None)
        if deadlines is None:
            self._occupancy_deadlines = deadlines = {}
        if result.deadline is not None:
            deadlines[room.area_id] = result.deadline
        else:
            deadlines.pop(room.area_id, None)
        return changed

    def _sync_occupancy_deadline(self) -> None:
        from .core import _now

        now = _now()
        deadlines = getattr(self, "_occupancy_deadlines", {})
        traffic_deadlines = (
            location.observed_at + ROBOT_LOCATION_MAX_AGE
            for location in self.observer.locations.locations.values()
            if location.valid
            and location.moving
            and location.observed_at + ROBOT_LOCATION_MAX_AGE > now
        )
        self.lifecycle.update_occupancy_deadline(
            min(
                (
                    *deadlines.values(),
                    *traffic_deadlines,
                    *(
                        session.deadline
                        for session in self.state.cleaning_occupancy_sessions.values()
                        if session.deadline is not None and session.deadline > now
                    ),
                ),
                default=None,
            )
        )

    def _receive_robot_location(self, location: RobotLocation, now: datetime) -> bool:
        if location.robot_id not in self.observer.location_sources:
            return False
        robot = next(
            robot
            for robot in self.discovery.robots.values()
            if robot.registry_id == location.robot_id
        )
        room = self.discovery.rooms.get(location.area_id or "")
        if location.moving and (
            location.area_id not in self.observer.attribution_areas
            or room is None
            or room.floor_id != robot.floor_id
        ):
            location = replace(location, area_id=None, valid=False)
        previous = self.observer.locations.locations.get(location.robot_id)
        expired = self._expire_cleaning_occupancy(now)
        changed = self.observer.locations.observe(location) or expired
        observed = self.hass.states.get(robot.entity_id)
        if self._observe_robot_cleaning_occupancy(
            robot, observed.state if observed else None, now
        ):
            changed = True
        affected = {location.area_id, previous.area_id if previous else None}
        if changed:
            # Location changes are rare. Include skipped intermediate areas and
            # pending episodes; ordinary map heartbeats only touch their room.
            affected.update(self.observer.attribution_areas)
        for area in affected:
            if area in self.observer.attribution_areas:
                room = self.discovery.rooms[area]
                detail = self.state.room_history[area]
                if (
                    detail.occupancy_attribution
                    and detail.occupancy_attribution.started_at
                ):
                    changed = (
                        self._observe_attributed_room(
                            room, self.observer.room(room), now
                        )
                        or changed
                    )
        self._sync_occupancy_deadline()
        return changed
