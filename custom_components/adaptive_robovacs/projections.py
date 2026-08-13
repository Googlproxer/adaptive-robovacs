"""Entity and dashboard projections derived from scheduler state."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from .models import next_usable_window_start, next_window_start

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


def room_state(coordinator: AdaptiveRoboVacCoordinator, area_id: str) -> dict[str, Any]:
    """Return the established card-friendly state for a discovered area."""

    room = coordinator.discovery.rooms[area_id]
    detail = coordinator._room_data(area_id)
    settings = coordinator._room_settings(room)
    now = _now()
    desired_window = coordinator._desired_window(room)
    local_now = dt_util.as_local(now)
    desired_window_start = (
        next_usable_window_start(local_now, desired_window.start, desired_window.end)
        if desired_window.valid
        else next_window_start(local_now, desired_window.start)
    )
    next_due = coordinator._room_due(room, "cleaning", now)
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
        room,
        duration_operation,
        duration_passes,
        coordinator.robot_registry_id(active_robot_id) if active_robot_id else None,
    )
    last_cleaned = _as_datetime(detail.get("cleaning"))
    occurrence = coordinator.data.get("occurrences", {}).get(area_id)
    confirmation = (
        coordinator.data.get("water_confirmations", {}).get(str(occurrence.get("occurrence_id")))
        if occurrence else None
    )
    occurrence_view = (
        {
            "program": occurrence.get("program"),
            "current_stage": occurrence.get("current_stage"),
            "scheduled_at": occurrence.get("scheduled_at"),
            "created_at": occurrence.get("created_at"),
            "stages": [
                {
                    key: stage.get(key)
                    for key in (
                        "operation", "passes", "status", "reason",
                        "started_at", "completed_at",
                    )
                }
                for stage in occurrence.get("stages", [])
            ],
        }
        if occurrence else None
    )
    confirmation_view = (
        {
            key: confirmation.get(key)
            for key in ("status", "sent_at", "expires_at", "responded_at")
        }
        if confirmation else None
    )
    episode = coordinator.data.get("water_notification_episodes", {}).get(area_id)
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
        "cleaning_interval": settings["cleaning_interval"],
        "expected_minutes": settings["expected_minutes"],
        "carpet": settings["carpet"],
        "ignore_desired_window": settings["ignore_desired_window"],
        "desired_window_configured_start": desired_window.configured_start,
        "desired_window_configured_end": desired_window.configured_end,
        "desired_window_effective_start": desired_window.start,
        "desired_window_effective_end": desired_window.end,
        "desired_window_start_inherited": desired_window.start_inherited,
        "desired_window_end_inherited": desired_window.end_inherited,
        "desired_window_valid": desired_window.valid,
        "pass_count": settings.get("pass_count"),
        "vacuum_pass_count": settings.get("vacuum_pass_count"),
        "mop_pass_count": settings.get("mop_pass_count"),
        "cleaning_program": settings.get("cleaning_program"),
        "occupancy": detail["occupancy"],
        "occupancy_source": detail["source"],
        "unavailable_radars": detail["unavailable_radars"],
        "last_cleaned": last_cleaned,
        "last_vacuum": _as_datetime(detail.get("vacuum")),
        "last_mop": _as_datetime(detail.get("mop")),
        "vacuum_due": next_due,
        "mop_due": None,
        "next_due": next_due,
        "desired_window_start": desired_window_start,
        "desired_window_next_start": desired_window_start,
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
        "occurrence": occurrence_view,
        "water_confirmation": confirmation_view,
        "last_stage_outcome": detail.get("last_stage_outcome"),
        "last_stage_reason": detail.get("last_stage_reason"),
        "last_stage_at": _as_datetime(detail.get("last_stage_at")),
        "water_notification_episode": (
            {
                key: episode.get(key)
                for key in ("reason", "first_sent_at", "last_sent_at")
            }
            if episode else None
        ),
        "failure": (
            coordinator.scheduler_fault_view()
            if coordinator.fault_affects_room(room)
            else None
        ),
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
        "adapter_id": robot.adapter_id,
        "adapter_schema_version": robot.adapter_schema_version,
        "adapter_capabilities": {
            "portable_area_clean": robot.adapter_capabilities.portable_area_clean,
            "supported_pass_counts": sorted(
                robot.adapter_capabilities.supported_pass_counts
            ),
            "native_area_pass_counts": sorted(
                robot.adapter_capabilities.native_area_pass_counts
            ),
            "vacuum_pass_counts": sorted(robot.adapter_capabilities.vacuum_pass_counts),
            "mop_pass_counts": sorted(robot.adapter_capabilities.mop_pass_counts),
            "supported_operations": sorted(
                robot.adapter_capabilities.supported_operations
            ),
            "water_readiness": {
                "status": robot.adapter_capabilities.water_readiness.status,
                "reason": robot.adapter_capabilities.water_readiness.reason,
                "ready": robot.adapter_capabilities.water_readiness.ready,
                "authoritative": robot.adapter_capabilities.water_readiness.authoritative,
            },
        },
        "adapter_diagnostic": robot.adapter_diagnostic,
        "failure": (
            coordinator.scheduler_fault_view()
            if coordinator.fault_affects_robot(robot)
            else None
        ),
        "settings": coordinator._robot_settings(robot),
    }
