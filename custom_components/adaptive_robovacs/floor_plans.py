"""Pure floor-plan commands and reducers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .models import normalize_floor_plan_edge
from .state import FloorPlanRectangle, FloorPlanSensorMarker, FloorPlanState


@dataclass(frozen=True, slots=True)
class FloorPlanWrite:
    """A fully decoded, optimistic floor-plan replacement request."""

    floor_id: str
    revision: int
    rooms: tuple[tuple[str, FloorPlanRectangle], ...]
    edges: tuple[tuple[str, str], ...]
    sensors: tuple[tuple[str, FloorPlanSensorMarker], ...]
    forget_area_ids: tuple[str, ...] = ()
    forget_sensor_registry_ids: tuple[str, ...] = ()


def decode_floor_plan_write(
    floor_id: str,
    revision: int,
    rooms: Mapping[str, Mapping[str, object]],
    edges: tuple[tuple[str, str], ...],
    sensors: Mapping[str, Mapping[str, object]],
    forget_area_ids: tuple[str, ...] = (),
    forget_sensor_registry_ids: tuple[str, ...] = (),
) -> FloorPlanWrite:
    """Decode the raw Home Assistant service payload at its boundary."""

    if not floor_id:
        raise ValueError("floor_id must not be empty")
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise ValueError("revision must be an integer")
    rectangles = tuple(
        sorted(
            (
                area_id,
                FloorPlanRectangle.from_mapping({**dict(raw), "floor_id": floor_id}),
            )
            for area_id, raw in rooms.items()
        )
    )
    markers = tuple(
        sorted(
            (registry_id, FloorPlanSensorMarker.from_mapping(raw))
            for registry_id, raw in sensors.items()
        )
    )
    normalized_edges = tuple(
        sorted({normalize_floor_plan_edge(*edge) for edge in edges})
    )
    return FloorPlanWrite(
        floor_id=floor_id,
        revision=revision,
        rooms=rectangles,
        edges=normalized_edges,
        sensors=markers,
        forget_area_ids=tuple(sorted(set(forget_area_ids))),
        forget_sensor_registry_ids=tuple(sorted(set(forget_sensor_registry_ids))),
    )


def replace_floor_plan(
    current: FloorPlanState,
    request: FloorPlanWrite,
    *,
    room_floor_by_id: Mapping[str, str],
    sensor_owner_by_registry_id: Mapping[str, str],
) -> FloorPlanState:
    """Validate and apply one floor replacement without mutating its input."""

    if request.revision != current.revision:
        raise ValueError("floor plan changed; reload before saving")
    selected_ids = {
        area_id
        for area_id, floor_id in room_floor_by_id.items()
        if floor_id == request.floor_id
    }
    if not selected_ids:
        raise ValueError("unknown floor")

    room_rectangles = dict(request.rooms)
    if any(area_id not in selected_ids for area_id in room_rectangles):
        raise ValueError("room layouts must use discovered rooms on the selected floor")
    if any(
        rectangle.floor_id != request.floor_id for rectangle in room_rectangles.values()
    ):
        raise ValueError("room layouts must stay on the selected floor")

    sensor_markers = dict(request.sensors)
    for registry_id, marker in sensor_markers.items():
        owner_area_id = sensor_owner_by_registry_id.get(registry_id)
        if owner_area_id is None or owner_area_id not in selected_ids:
            raise ValueError(
                "sensor markers must use discovered sensors on the selected floor"
            )
        if marker.area_id != owner_area_id:
            raise ValueError("a sensor marker must remain in its discovered room")

    if any(
        left not in selected_ids or right not in selected_ids
        for left, right in request.edges
    ):
        raise ValueError("floor-plan links must use rooms on the selected floor")

    next_rooms = {
        area_id: rectangle
        for area_id, rectangle in current.rooms.items()
        if area_id not in selected_ids
    }
    next_rooms.update(room_rectangles)
    next_edges = {
        edge
        for edge in current.edges
        if not (edge[0] in selected_ids and edge[1] in selected_ids)
    }
    next_edges.update(request.edges)
    selected_sensor_ids = {
        registry_id
        for registry_id, owner in sensor_owner_by_registry_id.items()
        if owner in selected_ids
    }
    next_sensors = {
        registry_id: marker
        for registry_id, marker in current.sensors.items()
        if registry_id not in selected_sensor_ids
    }
    next_sensors.update(sensor_markers)

    live_room_ids = set(room_floor_by_id)
    for area_id in request.forget_area_ids:
        if area_id in live_room_ids:
            raise ValueError("only unavailable rooms can be forgotten")
        next_rooms.pop(area_id, None)
        next_edges = {edge for edge in next_edges if area_id not in edge}
    live_sensor_ids = set(sensor_owner_by_registry_id)
    for registry_id in request.forget_sensor_registry_ids:
        if registry_id in live_sensor_ids:
            raise ValueError("only unavailable sensors can be forgotten")
        next_sensors.pop(registry_id, None)

    return FloorPlanState(
        revision=current.revision + 1,
        rooms=next_rooms,
        edges=next_edges,
        sensors=next_sensors,
    )


def replace_room_adjacency(
    current: FloorPlanState,
    area_id: str,
    neighbor_area_ids: tuple[str, ...],
    *,
    room_floor_by_id: Mapping[str, str],
) -> FloorPlanState:
    """Replace one room's direct same-floor neighbors immutably."""

    floor_id = room_floor_by_id.get(area_id)
    if floor_id is None:
        raise ValueError("unknown room")
    edges = {edge for edge in current.edges if area_id not in edge}
    for neighbor_id in neighbor_area_ids:
        if room_floor_by_id.get(neighbor_id) != floor_id:
            raise ValueError("adjacent rooms must be discovered on the same floor")
        edges.add(normalize_floor_plan_edge(area_id, neighbor_id))
    return FloorPlanState(
        revision=current.revision + 1,
        rooms=dict(current.rooms),
        edges=edges,
        sensors=dict(current.sensors),
    )
