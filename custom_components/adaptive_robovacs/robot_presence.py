"""Bounded per-robot location tracking; no history scans or native map I/O."""

from __future__ import annotations

from datetime import datetime

from .models import (
    ROBOT_LOCATION_MAX_AGE,
    ROBOT_OCCUPANCY_MATCH,
    RobotLocation,
    RobotVisit,
    mandatory_transit_areas,
)


class RobotPresenceTracker:
    """Keep only recent visits and cached unavoidable transit-area sets."""

    def __init__(self) -> None:
        self.locations: dict[str, RobotLocation] = {}
        self.visits: dict[str, tuple[RobotVisit, ...]] = {}
        self.edges: tuple[tuple[str, str], ...] = ()
        self._routes: dict[tuple[str, str], tuple[str, ...]] = {}

    def set_edges(self, edges: tuple[tuple[str, str], ...]) -> None:
        if edges != self.edges:
            self.edges = edges
            self._routes.clear()

    def observe(self, current: RobotLocation) -> bool:
        previous = self.locations.get(current.robot_id)
        if previous and current.observed_at < previous.observed_at:
            return False
        changed = previous is None or (
            previous.area_id,
            previous.moving,
            previous.valid,
        ) != (current.area_id, current.moving, current.valid)
        self.locations[current.robot_id] = current
        visits = [
            visit
            for visit in self.visits.get(current.robot_id, ())
            if visit.expires_at + ROBOT_OCCUPANCY_MATCH > current.observed_at
            and (visit.left_at is not None or current.valid)
        ]
        if previous and changed:
            visits = [
                RobotVisit(
                    visit.robot_id,
                    visit.area_id,
                    visit.entered_at,
                    current.observed_at,
                    current.observed_at,
                )
                if visit.left_at is None
                else visit
                for visit in visits
            ]
            if (
                previous.valid
                and previous.moving
                and current.valid
                and previous.area_id
                and current.area_id
                and previous.area_id != current.area_id
                and current.observed_at - previous.observed_at < ROBOT_LOCATION_MAX_AGE
            ):
                pair = (previous.area_id, current.area_id)
                if pair not in self._routes:
                    self._routes[pair] = mandatory_transit_areas(*pair, self.edges)
                visits.extend(
                    RobotVisit(
                        current.robot_id,
                        area,
                        previous.observed_at,
                        current.observed_at,
                        current.observed_at,
                        True,
                    )
                    for area in self._routes[pair]
                )
        if current.valid and current.moving and current.area_id:
            active = next((visit for visit in visits if visit.left_at is None), None)
            if active:
                visits[visits.index(active)] = RobotVisit(
                    active.robot_id,
                    active.area_id,
                    active.entered_at,
                    current.observed_at,
                )
            else:
                visits.append(
                    RobotVisit(
                        current.robot_id,
                        current.area_id,
                        current.observed_at,
                        current.observed_at,
                    )
                )
        self.visits[current.robot_id] = tuple(visits[-128:])
        return changed

    def for_room(self, area_id: str, now: datetime) -> tuple[RobotVisit, ...]:
        return tuple(
            visit
            for visits in self.visits.values()
            for visit in visits
            if visit.area_id == area_id
            and visit.expires_at + ROBOT_OCCUPANCY_MATCH > now
        )

    def traffic(self, area_id: str, now: datetime) -> bool:
        return any(
            location.valid
            and location.moving
            and location.area_id == area_id
            and now - location.observed_at < ROBOT_LOCATION_MAX_AGE
            for location in self.locations.values()
        )

    def retain(self, robots: set[str]) -> None:
        self.locations = {
            key: value for key, value in self.locations.items() if key in robots
        }
        self.visits = {
            key: value for key, value in self.visits.items() if key in robots
        }
