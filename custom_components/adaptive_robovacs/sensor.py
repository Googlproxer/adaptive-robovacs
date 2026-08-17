"""Status sensors for Adaptive RoboVacs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .entity import AdaptiveEntity, async_setup_dynamic_entities
from .models import format_time_until


class _SchedulerSensor(AdaptiveEntity, SensorEntity):
    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "scheduler", "Scheduler", "scheduler_status")

    @property
    def native_value(self) -> str:
        if self.coordinator.scheduler_halted:
            return "Scheduler halted"
        if self.coordinator.observe_only:
            return "observe-only"
        if self.coordinator.party_mode:
            return "party mode"
        return "ready"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        failure = self.coordinator.scheduler_fault_view()
        return {
            **super().extra_state_attributes,
            **self.coordinator.scheduler_summary(),
            "failure_code": failure.get("failure_code") if failure else None,
            "failure_summary": failure.get("failure_summary") if failure else None,
            "failure_since": failure.get("failure_since") if failure else None,
            "repair_active": bool(failure),
        }


class _RobotStatusSensor(AdaptiveEntity, SensorEntity):
    def __init__(self, coordinator, robot_entity_id: str) -> None:
        super().__init__(
            coordinator,
            f"robot_{coordinator.robot_unique_fragment(robot_entity_id)}_status",
            "status",
            "robot_status",
            robot_entity_id=robot_entity_id,
            robot_name_suffix="status",
        )
        self.robot_entity_id = robot_entity_id

    @property
    def native_value(self) -> str:
        state = self.coordinator.robot_state(self.robot_entity_id)
        if state["failure"]:
            return "Scheduler halted"
        return state["state"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.robot_state(self.robot_entity_id)
        failure = state["failure"]
        return {
            **super().extra_state_attributes,
            "floor_id": state["floor_id"],
            "battery": state["battery"],
            "ready": state["ready"],
            "reason": state["reason"],
            "activity": state["active"],
            "room": state["active_room"],
            "rooms": state["active_rooms"],
            "activity_source": (
                state["active"].get("source")
                if state["active"]
                else "native_app_assumed" if state["state"] == "cleaning" else None
            ),
            "activity_phase": state["active"].get("phase") if state["active"] else None,
            "scheduler_hold": state["scheduler_hold"],
            "cleaning_mode": state["settings"].get("mode"),
            "double_pass": state["settings"].get("double_pass"),
            "mop_double_pass": state["settings"].get("mop_double_pass"),
            "cleaning_program": state["settings"].get("cleaning_program"),
            "mopping_enabled": state["settings"].get("mopping_enabled"),
            "fan_speed": state["settings"].get("fan_speed"),
            "mop_mode": state["settings"].get("mop_mode"),
            "mop_intensity": state["settings"].get("mop_intensity"),
            "cleaning_depth": state["settings"].get("cleaning_depth"),
            "configured_profile_defaults": {
                key: state["settings"].get(key)
                for key in (
                    "fan_speed",
                    "mode",
                    "mop_mode",
                    "mop_intensity",
                    "cleaning_depth",
                )
            },
            "observed_profile": state["observed_profile"],
            "adapter_id": state["adapter_id"],
            "adapter_schema_version": state["adapter_schema_version"],
            "adapter_capabilities": state["adapter_capabilities"],
            "water_readiness": state["adapter_capabilities"].get("water_readiness"),
            "adapter_diagnostic": state["adapter_diagnostic"],
            "failure_code": failure.get("failure_code") if failure else None,
            "failure_summary": failure.get("failure_summary") if failure else None,
            "failure_since": failure.get("failure_since") if failure else None,
            "repair_active": bool(failure),
        }


class _RoomScheduleSensor(AdaptiveEntity, SensorEntity):
    def __init__(self, coordinator, area_id: str, name: str) -> None:
        super().__init__(coordinator, f"room_{area_id}_next_clean", f"{name} next clean", "room_schedule", area_id=area_id)
        self.area_id = area_id

    @property
    def native_value(self) -> str:
        state = self.coordinator.room_state(self.area_id)
        if state["failure"]:
            return "Scheduler halted"
        if state["active"]:
            if state["active"].get("phase") == "recovery_waiting":
                return "Completion pending"
            if state["active"].get("phase") == "cancelling":
                return "Returning to dock"
            if state["active"].get("phase") == "completion_held":
                return "Completion pending"
            if state["active"].get("phase") == "error_waiting":
                return "Scheduler held"
            if state["active"].get("phase") == "paused":
                return "Paused"
            if state["active_robot_state"] == "returning":
                return "Returning"
            return "In Progress"
        candidate = state["next_candidate"]
        if candidate:
            return "ready now"
        if not state["enabled"]:
            return "disabled"
        if state["block_reason"] == "not due":
            return format_time_until(state["next_due"], dt_util.utcnow())
        if state["block_reason"] in {
            "waiting for desired cleaning window",
            "unresolved occupancy; waiting for desired cleaning window",
        }:
            return format_time_until(
                state["desired_window_start"], dt_util.as_local(dt_util.utcnow())
            )
        return state["block_reason"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.room_state(self.area_id)
        candidate = state["next_candidate"]
        failure = state["failure"]
        return {
            **super().extra_state_attributes,
            "room": state["name"],
            "floor_id": state["floor_id"],
            "bedroom": state["bedroom"],
            "bedroom_transit": state["bedroom_transit"],
            "ignore_desired_window": state["ignore_desired_window"],
            "desired_window_configured_start": state["desired_window_configured_start"],
            "desired_window_configured_end": state["desired_window_configured_end"],
            "desired_window_effective_start": state["desired_window_effective_start"],
            "desired_window_effective_end": state["desired_window_effective_end"],
            "desired_window_start_inherited": state["desired_window_start_inherited"],
            "desired_window_end_inherited": state["desired_window_end_inherited"],
            "desired_window_valid": state["desired_window_valid"],
            "pass_count": state["pass_count"],
            "vacuum_pass_count": state["vacuum_pass_count"],
            "mop_pass_count": state["mop_pass_count"],
            "cleaning_program": state["cleaning_program"],
            "fan_speed": state["fan_speed"],
            "mode": state["mode"],
            "mop_mode": state["mop_mode"],
            "mop_intensity": state["mop_intensity"],
            "cleaning_depth": state["cleaning_depth"],
            "effective_profiles": state["effective_profiles"],
            "latest_manual_request": state["latest_manual_request"],
            "effective_pass_count": (
                state["active"].get("passes")
                if state["active"]
                else candidate.get("passes") if candidate else None
            ),
            "cleaning_due_at": state["next_due"].isoformat(),
            "vacuum_due_at": state["next_due"].isoformat(),
            "mop_due_at": None,
            "estimated_start": candidate["due_at"].isoformat() if candidate else None,
            "operation": candidate["operation"] if candidate else None,
            "forecast_confidence": candidate["confidence"] if candidate else 0,
            "occupancy": state["occupancy"],
            "occupancy_source": state["occupancy_source"],
            "map_status": state["map_status"],
            "map_error": state["map_error"],
            "block_reason": state["block_reason"],
            "desired_window_start": state["desired_window_start"].isoformat(),
            "desired_window_next_start": state["desired_window_next_start"].isoformat(),
            "unresolved_window_start": state["unresolved_window_start"].isoformat(),
            "active_job_source": state["active"].get("source") if state["active"] else None,
            "active_robot": state["active_robot"],
            "active_robot_state": state["active_robot_state"],
            "active_operation": state["active"].get("operation") if state["active"] else None,
            "active_phase": state["active"].get("phase") if state["active"] else None,
            "active_started_at": state["active"].get("observed_started") if state["active"] else None,
            "expected_end_at": state["active"].get("expected_end") if state["active"] else None,
            "active_completion_confidence": state["active"].get("completion_confidence") if state["active"] else None,
            "active_hold_reason": state["active"].get("hold_reason") if state["active"] else None,
            "learned_duration_minutes": state["effective_duration_minutes"],
            "duration_sample_count": state["duration_sample_count"],
            "occurrence": state["occurrence"],
            "water_confirmation": state["water_confirmation"],
            "last_stage_outcome": state["last_stage_outcome"],
            "last_stage_reason": state["last_stage_reason"],
            "last_stage_at": state["last_stage_at"].isoformat() if state["last_stage_at"] else None,
            "water_notification_episode": state["water_notification_episode"],
            "failure_code": failure.get("failure_code") if failure else None,
            "failure_summary": failure.get("failure_summary") if failure else None,
            "failure_since": failure.get("failure_since") if failure else None,
            "repair_active": bool(failure),
        }


class _RoomLastCleanedSensor(AdaptiveEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator, area_id: str, name: str) -> None:
        super().__init__(coordinator, f"room_{area_id}_last_cleaned", f"{name} last cleaned", "room_last_cleaned", area_id=area_id)
        self.area_id = area_id

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.room_state(self.area_id)["last_cleaned"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.room_state(self.area_id)
        return {
            **super().extra_state_attributes,
            "last_vacuum": state["last_vacuum"].isoformat() if state["last_vacuum"] else None,
            "last_mop": state["last_mop"].isoformat() if state["last_mop"] else None,
        }


class _RoomOccupancySensor(AdaptiveEntity, SensorEntity):
    def __init__(self, coordinator, area_id: str, name: str) -> None:
        super().__init__(coordinator, f"room_{area_id}_occupancy", f"{name} occupancy", "room_occupancy", area_id=area_id)
        self.area_id = area_id

    @property
    def native_value(self) -> str:
        return self.coordinator.room_state(self.area_id)["occupancy"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.room_state(self.area_id)
        return {
            **super().extra_state_attributes,
            "source": state["occupancy_source"],
            "radars": state["radars"],
            "motion_fallbacks": state["fallbacks"],
            "unavailable_radars": state["unavailable_radars"],
        }


class _RoomManualStatusSensor(AdaptiveEntity, SensorEntity):
    def __init__(self, coordinator, area_id: str, name: str) -> None:
        super().__init__(
            coordinator,
            f"room_{area_id}_manual_status",
            f"{name} manual request",
            "room_manual_status",
            area_id=area_id,
        )
        self.area_id = area_id

    @property
    def native_value(self) -> str:
        event = self.coordinator.room_state(self.area_id)["latest_manual_request"]
        return (
            str(event.get("outcome", "unknown")).replace("_", " ")
            if event
            else "never requested"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        event = self.coordinator.room_state(self.area_id)["latest_manual_request"]
        return {**super().extra_state_attributes, "latest_request": event}


def _entities(coordinator) -> list[AdaptiveEntity]:
    entities: list[AdaptiveEntity] = [_SchedulerSensor(coordinator)]
    for robot in coordinator.discovery.robots.values():
        entities.append(_RobotStatusSensor(coordinator, robot.entity_id))
    for room in coordinator.discovery.rooms.values():
        entities.extend(
            [
                _RoomScheduleSensor(coordinator, room.area_id, room.name),
                _RoomLastCleanedSensor(coordinator, room.area_id, room.name),
                _RoomOccupancySensor(coordinator, room.area_id, room.name),
                _RoomManualStatusSensor(coordinator, room.area_id, room.name),
            ]
        )
    return entities


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    """Set up scheduler status entities."""

    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_setup_dynamic_entities(entry, async_add_entities, coordinator, lambda: _entities(coordinator))
