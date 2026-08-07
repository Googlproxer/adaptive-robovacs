"""Status sensors for Adaptive RoboVacs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .entity import AdaptiveEntity, async_setup_dynamic_entities


class _SchedulerSensor(AdaptiveEntity, SensorEntity):
    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "scheduler", "Scheduler", "scheduler_status")

    @property
    def native_value(self) -> str:
        if self.coordinator.observe_only:
            return "observe-only"
        if self.coordinator.party_mode:
            return "party mode"
        return "ready"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            **super().extra_state_attributes,
            "last_evaluation": self.coordinator.data.get("last_evaluation"),
            "preview": self.coordinator.data.get("last_preview", {}),
            "migration": {
                "complete": self.coordinator.data.get("legacy_migrated", False),
                "matched_rooms": self.coordinator.data.get("legacy_migration_count", 0),
            },
        }


class _RobotStatusSensor(AdaptiveEntity, SensorEntity):
    def __init__(self, coordinator, robot_entity_id: str) -> None:
        super().__init__(
            coordinator,
            f"robot_{robot_entity_id}_status",
            "status",
            "robot_status",
            robot_entity_id=robot_entity_id,
            robot_name_suffix="status",
        )
        self.robot_entity_id = robot_entity_id

    @property
    def native_value(self) -> str:
        state = self.coordinator.robot_state(self.robot_entity_id)
        return "cleaning" if state["active"] else state["state"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.robot_state(self.robot_entity_id)
        return {
            **super().extra_state_attributes,
            "floor_id": state["floor_id"],
            "battery": state["battery"],
            "ready": state["ready"],
            "reason": state["reason"],
            "activity": state["active"],
            "room": state["active_room"],
            "cleaning_mode": state["settings"].get("mode"),
            "double_pass": state["settings"].get("double_pass"),
            "mopping_enabled": state["settings"].get("mopping_enabled"),
        }


class _RoomScheduleSensor(AdaptiveEntity, SensorEntity):
    def __init__(self, coordinator, area_id: str, name: str) -> None:
        super().__init__(coordinator, f"room_{area_id}_next_clean", f"{name} next clean", "room_schedule", area_id=area_id)
        self.area_id = area_id

    @property
    def native_value(self) -> str:
        state = self.coordinator.room_state(self.area_id)
        if state["active"]:
            return "In Progress"
        candidate = state["next_candidate"]
        if candidate:
            return "ready now"
        if not state["enabled"]:
            return "disabled"
        return state["block_reason"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.room_state(self.area_id)
        candidate = state["next_candidate"]
        return {
            **super().extra_state_attributes,
            "room": state["name"],
            "floor_id": state["floor_id"],
            "bedroom": state["bedroom"],
            "bedroom_transit": state["bedroom_transit"],
            "carpet": state["carpet"],
            "vacuum_due_at": state["vacuum_due"].isoformat(),
            "mop_due_at": state["mop_due"].isoformat() if state["mop_due"] else None,
            "estimated_start": candidate["due_at"].isoformat() if candidate else None,
            "operation": candidate["operation"] if candidate else None,
            "forecast_confidence": candidate["confidence"] if candidate else 0,
            "occupancy": state["occupancy"],
            "occupancy_source": state["occupancy_source"],
            "map_status": state["map_status"],
            "map_error": state["map_error"],
            "block_reason": state["block_reason"],
            "active_job_source": state["active"].get("source") if state["active"] else None,
            "active_robot": (
                next(
                    (robot_id for robot_id, job in self.coordinator.data["active"].items() if job is state["active"]),
                    None,
                )
                if state["active"]
                else None
            ),
            "active_operation": state["active"].get("operation") if state["active"] else None,
            "active_started_at": state["active"].get("observed_started") if state["active"] else None,
            "expected_end_at": state["active"].get("expected_end") if state["active"] else None,
            "active_completion_confidence": state["active"].get("completion_confidence") if state["active"] else None,
            "learned_duration_minutes": state["effective_duration_minutes"],
            "duration_sample_count": state["duration_sample_count"],
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
            ]
        )
    return entities


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    """Set up scheduler status entities."""

    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_setup_dynamic_entities(entry, async_add_entities, coordinator, lambda: _entities(coordinator))
