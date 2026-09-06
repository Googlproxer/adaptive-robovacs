"""Typed Home Assistant state-observation boundary."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.core import HomeAssistant

from .discovery import DiscoveredRobot, DiscoveredRoom, DiscoverySnapshot
from .models import RobotObservation, RoomObservation, resolve_occupancy


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

    def room(self, room: DiscoveredRoom) -> RoomObservation:
        """Observe occupancy inputs for one room."""

        radars = tuple(self._state(entity_id) for entity_id in room.radar_entity_ids)
        fallbacks = tuple(
            self._state(entity_id) for entity_id in room.fallback_entity_ids
        )
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
        return RobotObservation(
            state=state,
            battery=battery,
            cleaning_timer_minutes=cleaning_timer,
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
