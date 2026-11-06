"""Passive Roborock map observations, resolved through HA's area registry.

No refresh, polling, robot command, native room-name match, or persisted map
identifier is used here. Unsupported native contracts simply provide no source.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import entity_registry as er

from ..models import ROBOT_LOCATION_MAX_AGE, RobotLocation

if TYPE_CHECKING:
    from ..discovery import DiscoveredRobot


class RoborockLocationSource:
    """Read already-cached numeric map/room evidence from the owning vacuum."""

    def __init__(
        self,
        robot: DiscoveredRobot,
        entity: Any,
        mapping: Mapping[str, object],
    ) -> None:
        self.robot = robot
        self.entity = entity
        self.coordinator = entity.coordinator
        self.api = self.coordinator.properties_api
        self.mapping_key = repr(mapping)
        reverse: dict[str, set[str]] = {}
        for area, segments in mapping.items():
            if isinstance(segments, list):
                for segment in segments:
                    if isinstance(segment, str):
                        reverse.setdefault(segment, set()).add(area)
        self.reverse = {
            segment: next(iter(areas))
            for segment, areas in reverse.items()
            if len(areas) == 1
        }
        self.areas = frozenset(self.reverse.values())
        self._frame_at: datetime | None = None
        self._frame_map: int | None = None
        self._map_data: object | None = None
        self._last: RobotLocation | None = None
        self._cancels: list[Callable[[], None]] = []

    def read(self, now: datetime, raw_state: str | None) -> RobotLocation:
        """Only a newly observed parsed map may refresh moving-location age."""

        if raw_state == "docked":
            return RobotLocation(
                self.robot.registry_id, self.robot.dock_area_id, now, False
            )
        moving = raw_state in {"cleaning", "returning"}
        content = getattr(self.api.map_content, "map_data", None)
        home = getattr(self.api.home, "current_map_data", None)
        map_flag = getattr(self.api.maps, "current_map", None)
        segment = getattr(content, "vacuum_room", None)
        fresh = (
            self._frame_at is not None
            and timedelta(0) <= now - self._frame_at < ROBOT_LOCATION_MAX_AGE
        )
        valid = bool(
            moving
            and fresh
            and type(map_flag) is int
            and type(segment) is int
            and self._frame_map == map_flag
            and segment is not None
            and home is not None
            and getattr(home, "map_flag", None) == map_flag
            and any(
                getattr(room, "segment_id", None) == segment
                for room in getattr(home, "rooms", ())
            )
        )
        area = self.reverse.get(f"{map_flag}_{segment}") if valid else None
        return RobotLocation(
            self.robot.registry_id,
            area,
            self._frame_at or now,
            moving,
            valid=valid and area is not None,
        )

    def start(
        self,
        clock: Callable[[], datetime],
        state: Callable[[], str | None],
        receive: Callable[[RobotLocation], None],
    ) -> None:
        # HomeTrait, not MapContentTrait, owns the native update listener.
        # Its subscription can immediately replay cached home data. Remember
        # that frame first, so a replay/status heartbeat cannot make it fresh.
        self._map_data = getattr(self.api.map_content, "map_data", None)

        def capture() -> None:
            now = clock()
            map_data = getattr(self.api.map_content, "map_data", None)
            if map_data is not self._map_data:
                self._map_data = map_data
                map_flag = getattr(self.api.maps, "current_map", None)
                if self._frame_map is not None and map_flag != self._frame_map:
                    receive(
                        RobotLocation(self.robot.registry_id, None, now, False, False)
                    )
                self._frame_map = map_flag if map_data is not None else None
                self._frame_at = now if map_data is not None else None
            location = self.read(now, state())
            # Dock/unchanged native status heartbeats are not new observations.
            if (
                self._last
                and not location.moving
                and (location.area_id, location.valid, location.moving)
                == (self._last.area_id, self._last.valid, self._last.moving)
            ):
                return
            if location != self._last:
                self._last = location
                receive(location)

        self._cancels = [
            self.api.home.add_update_listener(capture),
            self.coordinator.async_add_listener(capture),
        ]

    def close(self) -> None:
        for cancel in self._cancels:
            cancel()
        self._cancels.clear()


def location_source(hass: Any, robot: DiscoveredRobot) -> RoborockLocationSource | None:
    """Bind only an explicitly supported same-entity native runtime contract."""

    if robot.platform != "roborock":
        return None
    component = getattr(hass, "data", {}).get("vacuum")
    entity = component.get_entity(robot.entity_id) if component else None
    coordinator = getattr(entity, "coordinator", None)
    api = getattr(coordinator, "properties_api", None)
    if (
        api is None
        or not hasattr(api, "map_content")
        or not callable(
            getattr(getattr(api, "home", None), "add_update_listener", None)
        )
        or not hasattr(api, "maps")
        or not callable(getattr(coordinator, "async_add_listener", None))
    ):
        return None
    entry = er.async_get(hass).async_get(robot.entity_id)
    if entry is None or entry.device_id != robot.device_id:
        return None
    options: object = entry.options.get("vacuum")
    mapping = options.get("area_mapping") if isinstance(options, Mapping) else None
    if not isinstance(mapping, Mapping):
        return None
    return RoborockLocationSource(robot, entity, mapping)
