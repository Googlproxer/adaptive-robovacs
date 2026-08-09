"""Entity and dashboard projections derived from scheduler state."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from .const import DEFAULT_UNRESOLVED_START
from .models import next_window_start

if TYPE_CHECKING:
    from .coordinator import AdaptiveRoboVacCoordinator


def _now() -> datetime:
    return dt_util.utcnow()


def _as_datetime(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_util.UTC)


def decommission_inventory(coordinator: AdaptiveRoboVacCoordinator) -> dict[str, Any]:
    """Report legacy-owned objects; this function never removes them."""

    legacy_entities = sorted(
        state.entity_id
        for state in coordinator.hass.states.async_all()
        if state.entity_id.startswith("pyscript.robovac_")
        or state.entity_id.startswith("input_boolean.robovac_")
        or state.entity_id.startswith("input_number.robovac_")
        or state.entity_id.startswith("input_select.robovac_")
        or state.entity_id.startswith("input_datetime.robovac_")
    )
    references = []
    for state in coordinator.hass.states.async_all("automation") + coordinator.hass.states.async_all("script"):
        if "robovac_scheduler" in str(state.attributes):
            references.append(state.entity_id)
    return {
        "legacy_entities": legacy_entities,
        "external_references": sorted(references),
        "safe_to_remove": False,
        "message": "Inventory only. Legacy removal requires explicit user sign-off.",
    }


def room_state(coordinator: AdaptiveRoboVacCoordinator, area_id: str) -> dict[str, Any]:
    """Return the established card-friendly state for a discovered area."""

    room = coordinator.discovery.rooms[area_id]
    detail = coordinator._room_data(area_id)
    settings = coordinator._room_settings(room)
    now = _now()
    desired_window_start = next_window_start(
        dt_util.as_local(now), str(coordinator.data.get("unresolved_start", DEFAULT_UNRESOLVED_START))
    )
    vacuum_due = coordinator._room_due(room, "vacuum", now)
    mop_due = None if settings.get("carpet", False) else coordinator._room_due(room, "mop", now)
    capable = [
        robot
        for robot in coordinator.discovery.robots.values()
        if robot.floor_id == room.floor_id and robot.supports_area_clean
    ]
    can_mop = any(coordinator._mop_ready(robot) for robot in capable)
    next_due = min(vacuum_due, mop_due) if mop_due and can_mop else vacuum_due
    candidate, reason = coordinator._room_candidate(room, now)
    active_robot_id, active = next(
        (
            (robot_id, job)
            for robot_id, job in coordinator.data["active"].items()
            if job and area_id in coordinator._active_rooms(job)
        ),
        (None, None),
    )
    active_robot_state = (
        coordinator.hass.states.get(active_robot_id).state
        if active_robot_id and coordinator.hass.states.get(active_robot_id)
        else None
    )
    duration_operation = active["operation"] if active else candidate["operation"] if candidate else "vacuum"
    duration_passes = int(active.get("passes", 1)) if active else candidate["passes"] if candidate else 1
    duration_minutes, duration_sample_count = coordinator._effective_duration(
        room, duration_operation, duration_passes, active_robot_id
    )
    last_cleaned = max(
        filter(
            None,
            [
                _as_datetime(detail.get("vacuum")),
                _as_datetime(detail.get("mop")),
            ],
        ),
        default=None,
    )
    return {
        "name": room.name,
        "area_id": room.area_id,
        "floor_id": room.floor_id,
        "bedroom": room.is_bedroom,
        "bedroom_transit": room.is_bedroom_transit,
        "radars": room.radar_entity_ids,
        "fallbacks": room.fallback_entity_ids,
        "enabled": settings["enabled"],
        "vacuum_interval": settings["vacuum_interval"],
        "mop_interval": settings["mop_interval"],
        "expected_minutes": settings["expected_minutes"],
        "carpet": settings["carpet"],
        "ignore_desired_window": settings["ignore_desired_window"],
        "occupancy": detail["occupancy"],
        "occupancy_source": detail["source"],
        "unavailable_radars": detail["unavailable_radars"],
        "last_cleaned": last_cleaned,
        "last_vacuum": _as_datetime(detail.get("vacuum")),
        "last_mop": _as_datetime(detail.get("mop")),
        "vacuum_due": vacuum_due,
        "mop_due": mop_due,
        "next_due": next_due,
        "desired_window_start": desired_window_start,
        "unresolved_window_start": desired_window_start,
        "next_candidate": candidate,
        "active": active,
        "active_robot": active_robot_id,
        "active_robot_state": active_robot_state,
        "effective_duration_minutes": duration_minutes,
        "duration_sample_count": duration_sample_count,
        "block_reason": reason,
        "map_status": detail.get("map_status", "unknown"),
        "map_error": detail.get("map_error"),
    }


def robot_state(coordinator: AdaptiveRoboVacCoordinator, entity_id: str) -> dict[str, Any]:
    """Return the established card-friendly state for a discovered vacuum."""

    robot = coordinator.discovery.robots[entity_id]
    state = coordinator.hass.states.get(entity_id)
    ready, reason = coordinator._robot_ready(robot)
    active = coordinator.data["active"].get(entity_id)
    hold = coordinator.data["robot_holds"].get(entity_id)
    active_rooms = [
        coordinator.discovery.rooms[area_id].name
        for area_id in coordinator._active_rooms(active)
        if area_id in coordinator.discovery.rooms
    ] if active else []
    return {
        "name": robot.name,
        "entity_id": entity_id,
        "floor_id": robot.floor_id,
        "state": state.state if state else "unavailable",
        "battery": coordinator._robot_battery(robot),
        "ready": ready,
        "reason": reason,
        "active": active,
        "scheduler_hold": hold,
        "active_room": ", ".join(active_rooms) if active_rooms else None,
        "active_rooms": active_rooms,
        "profile": robot.profile,
        "settings": coordinator._robot_settings(robot),
    }
