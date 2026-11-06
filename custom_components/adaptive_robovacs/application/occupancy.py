"""Apply robot-aware occupancy checkpoints without rescanning learned history."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant

from ..discovery import DiscoveredRoom, DiscoverySnapshot
from ..lifecycle import SchedulerRuntime
from ..models import (
    ROBOT_LOCATION_MAX_AGE,
    OccupancyAttribution,
    RobotLocation,
    RoomObservation,
    attribute_occupancy,
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

    def _observe_attributed_room(
        self, room: DiscoveredRoom, observed: RoomObservation, now: datetime
    ) -> bool:
        detail = self.state.room_history[room.area_id]
        previous = detail.occupancy_attribution or OccupancyAttribution(
            raw=detail.occupancy, vacancy_since=detail.unoccupied_since
        )
        if detail.occupancy_attribution and detail.occupancy_source != observed.source:
            previous = OccupancyAttribution(raw=observed.occupancy)
        result = attribute_occupancy(
            previous,
            observed.occupancy,
            now,
            self.observer.locations.for_room(room.area_id, now),
        )
        checkpoint = result.checkpoint
        changed = (
            detail.occupancy != result.occupancy
            or detail.unoccupied_since != result.clear_since
            or detail.occupancy_attribution != checkpoint
        )
        if previous.raw != observed.occupancy:
            detail.occupancy_transitions.append(OccupancyTransition(now))
            detail.occupancy_transitions = detail.occupancy_transitions[-4096:]
        if result.sample is not None:
            started, ended = result.sample
            minutes = int((ended - started).total_seconds() / 60)
            if minutes > 0:
                detail.occupancy_samples.append(OccupancySample(started, minutes))
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
            min((*deadlines.values(), *traffic_deadlines), default=None)
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
        changed = self.observer.locations.observe(location)
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
