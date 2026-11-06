"""Typed Home Assistant state-observation boundary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.core import HomeAssistant

from .adapters.location import RoborockLocationSource, location_source
from .discovery import DiscoveredRobot, DiscoveredRoom, DiscoverySnapshot
from .models import (
    RobotErrorObservation,
    RobotLocation,
    RobotObservation,
    RoomObservation,
    normalize_robot_error,
    resolve_occupancy,
)
from .robot_presence import RobotPresenceTracker


@dataclass(frozen=True, slots=True)
class ObservedRoom:
    """Stable room identity paired with its current observation."""

    area_id: str
    observation: RoomObservation


@dataclass(frozen=True, slots=True)
class ObservedRobot:
    """Stable robot identity paired with its current observation."""

    registry_id: str
    entity_id: str
    observation: RobotObservation


@dataclass(frozen=True, slots=True)
class HouseObservation:
    """One deterministic point-in-time view of the discovered house."""

    rooms: tuple[ObservedRoom, ...]
    robots: tuple[ObservedRobot, ...]


class HomeAssistantObserver:
    """Translate HA states into domain observations without mutating state."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self.locations = RobotPresenceTracker()
        self.location_sources: dict[str, RoborockLocationSource] = {}
        self.attribution_areas: frozenset[str] = frozenset()
        self.occupancy_entities: dict[str, tuple[DiscoveredRoom, ...]] = {}

    def bind_locations(
        self,
        discovery: DiscoverySnapshot,
        receive: Callable[[RobotLocation], None],
        clock: Callable[[], datetime],
    ) -> None:
        sources: dict[str, RoborockLocationSource] = {}
        areas: set[str] = set()
        for robot in discovery.robots.values():
            source = location_source(self._hass, robot)
            if source is None:
                continue
            previous = self.location_sources.get(robot.registry_id)
            if (
                previous
                and previous.mapping_key == source.mapping_key
                and previous.entity is source.entity
                and previous.robot.floor_id == robot.floor_id
            ):
                source = previous
                source.robot = robot
            else:
                self.locations.retain(set(self.location_sources) - {robot.registry_id})

                def robot_state(entity_id: str = robot.entity_id) -> str | None:
                    return self._state(entity_id)

                source.start(
                    clock,
                    robot_state,
                    receive,
                )
            sources[robot.registry_id] = source
            areas.update(
                area
                for area in source.areas
                if area in discovery.rooms
                and discovery.rooms[area].floor_id == robot.floor_id
            )
        for registry_id, source in self.location_sources.items():
            if sources.get(registry_id) is not source:
                source.close()
        self.location_sources = sources
        self.locations.retain(set(sources))
        self.attribution_areas = frozenset(areas)
        entities: dict[str, list[DiscoveredRoom]] = {}
        for area in areas:
            room = discovery.rooms[area]
            for entity in (*room.radar_entity_ids, *room.fallback_entity_ids):
                entities.setdefault(entity, []).append(room)
        self.occupancy_entities = {
            entity: tuple(rooms) for entity, rooms in entities.items()
        }

    def close_locations(self) -> None:
        for source in self.location_sources.values():
            source.close()
        self.location_sources.clear()
        self.locations.retain(set())

    def set_location_topology(
        self, discovery: DiscoverySnapshot, edges: set[tuple[str, str]]
    ) -> None:
        self.locations.set_edges(
            tuple(
                sorted(
                    (first, second)
                    for first, second in edges
                    if first in discovery.rooms
                    and second in discovery.rooms
                    and discovery.rooms[first].floor_id
                    == discovery.rooms[second].floor_id
                )
            )
        )

    def capture_occupancy(
        self, entity_id: str, state: str | None
    ) -> tuple[tuple[str, RoomObservation], ...]:
        return tuple(
            (room.area_id, self.room(room, override=(entity_id, state)))
            for room in self.occupancy_entities.get(entity_id, ())
        )

    def room(
        self, room: DiscoveredRoom, *, override: tuple[str, str | None] | None = None
    ) -> RoomObservation:
        """Observe occupancy inputs for one room."""

        def state(entity_id: str) -> str | None:
            return (
                override[1]
                if override and override[0] == entity_id
                else self._state(entity_id)
            )

        radars = tuple(state(entity_id) for entity_id in room.radar_entity_ids)
        fallbacks = tuple(state(entity_id) for entity_id in room.fallback_entity_ids)
        resolved = resolve_occupancy(radars, fallbacks)
        return RoomObservation(
            occupancy=resolved.state,
            source=resolved.source,
            unavailable_radars=resolved.unavailable_radars,
        )

    def robot(self, robot: DiscoveredRobot) -> RobotObservation:
        """Observe state, battery, and native timer for one robot."""

        state = self._state(robot.entity_id)
        battery = self._number(self._state(robot.profile.battery_entity_id))
        cleaning_timer = self._number(
            self._state(robot.profile.cleaning_time_entity_id)
        )
        error_ids = robot.adapter_capabilities.error_entity_ids
        error = RobotErrorObservation()
        if len(error_ids) == 1:
            error = normalize_robot_error(self._state(error_ids[0]))
        elif error_ids:
            error = RobotErrorObservation("unknown")
        return RobotObservation(
            state=state,
            battery=battery,
            cleaning_timer_minutes=cleaning_timer,
            error=error,
        )

    def house(self, discovery: DiscoverySnapshot) -> HouseObservation:
        """Observe every discovered room and robot in stable identity order."""

        return HouseObservation(
            rooms=tuple(
                ObservedRoom(room.area_id, self.room(room))
                for room in sorted(
                    discovery.rooms.values(), key=lambda item: item.area_id
                )
            ),
            robots=tuple(
                ObservedRobot(
                    robot.registry_id,
                    robot.entity_id,
                    self.robot(robot),
                )
                for robot in sorted(
                    discovery.robots.values(), key=lambda item: item.registry_id
                )
            ),
        )

    def _state(self, entity_id: str | None) -> str | None:
        state = self._hass.states.get(entity_id) if entity_id else None
        return state.state if state else None

    @staticmethod
    def _number(value: str | None) -> float | None:
        try:
            return float(value) if value is not None else None
        except TypeError, ValueError:
            return None
