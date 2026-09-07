"""Status sensors for Adaptive RoboVacs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import AdaptiveRoboVacsCoordinator
from .entity import AdaptiveEntity, async_setup_dynamic_entities, robot_unique_fragment
from .models import JobPhase, WaterReadiness, format_time_until
from .presentation import (
    active_job_attributes,
    duration_estimate_attributes,
    effective_profile_attributes,
    eligibility_attributes,
    fault_attributes,
    manual_audit_attributes,
    occurrence_attributes,
    robot_hold_attributes,
    robot_settings_attributes,
    room_decision_attributes,
    room_recovery_attributes,
    scheduler_attributes,
    water_confirmation_attributes,
    water_episode_attributes,
)
from .runtime_data import AdaptiveRoboVacsConfigEntry

PARALLEL_UPDATES = 0


def _fault_fields(failure: dict[str, object] | None) -> dict[str, object]:
    return {
        "failure_code": failure.get("failure_code") if failure else None,
        "failure_summary": failure.get("failure_summary") if failure else None,
        "failure_since": failure.get("failure_since") if failure else None,
        "repair_active": bool(failure),
    }


class _SchedulerSensor(AdaptiveEntity, SensorEntity):
    def __init__(self, coordinator: AdaptiveRoboVacsCoordinator) -> None:
        super().__init__(coordinator, "scheduler", "Scheduler", "scheduler_status")

    @property
    def native_value(self) -> str:
        scheduler = self.coordinator.data.scheduler
        if scheduler.scheduler_limited:
            return "Scheduler limited"
        if scheduler.observe_only:
            return "observe-only"
        if scheduler.party_mode:
            return "party mode"
        return "ready"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        scheduler = self.coordinator.data.scheduler
        failure = fault_attributes(scheduler.failure)
        return {
            **super().extra_state_attributes,
            **scheduler_attributes(scheduler),
            **_fault_fields(failure),
        }


class _RobotStatusSensor(AdaptiveEntity, SensorEntity):
    def __init__(
        self, coordinator: AdaptiveRoboVacsCoordinator, robot_entity_id: str
    ) -> None:
        super().__init__(
            coordinator,
            f"robot_{robot_unique_fragment(coordinator, robot_entity_id)}_status",
            "status",
            "robot_status",
            robot_entity_id=robot_entity_id,
            robot_name_suffix="status",
        )
        self.robot_entity_id = robot_entity_id

    @property
    def native_value(self) -> str:
        robot = self.robot_view(self.robot_entity_id)
        return "Scheduler held" if robot.failure else robot.state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        robot = self.robot_view(self.robot_entity_id)
        active = active_job_attributes(robot.active)
        settings = robot_settings_attributes(robot.settings)
        failure = fault_attributes(robot.failure)
        capabilities = robot.adapter_capabilities
        water = capabilities.water_readiness
        water_attributes = (
            {
                "status": water.status,
                "reason": water.reason,
                "ready": water.ready,
                "authoritative": water.authoritative,
            }
            if isinstance(water, WaterReadiness)
            else {
                "status": water,
                "reason": water,
                "ready": False,
                "authoritative": False,
            }
        )
        adapter_capabilities = {
            "portable_area_clean": capabilities.portable_area_clean,
            "supported_pass_counts": sorted(capabilities.supported_pass_counts),
            "native_area_pass_counts": sorted(capabilities.native_area_pass_counts),
            "vacuum_pass_counts": sorted(capabilities.vacuum_pass_counts),
            "mop_pass_counts": sorted(capabilities.mop_pass_counts),
            "cleaning_depth_options": list(capabilities.cleaning_depth_options),
            "native_mop_profile": capabilities.native_mop_profile,
            "supported_operations": sorted(capabilities.supported_operations),
            "water_readiness": water_attributes,
        }
        return {
            **super().extra_state_attributes,
            "floor_id": robot.floor_id,
            "battery": robot.battery,
            "ready": robot.ready,
            "reason": robot.reason,
            "activity": active,
            "room": robot.active_room,
            "rooms": list(robot.active_rooms),
            "activity_source": (
                active.get("source")
                if active
                else "native_app_assumed"
                if robot.state == "cleaning"
                else None
            ),
            "activity_phase": active.get("phase") if active else None,
            "scheduler_hold": robot_hold_attributes(robot.scheduler_hold),
            "cleaning_mode": settings["mode"],
            "double_pass": settings["double_pass"],
            "mop_double_pass": settings["mop_double_pass"],
            "cleaning_program": settings["cleaning_program"],
            "mopping_enabled": settings["mopping_enabled"],
            "fan_speed": settings["fan_speed"],
            "mop_mode": settings["mop_mode"],
            "mop_intensity": settings["mop_intensity"],
            "cleaning_depth": settings["cleaning_depth"],
            "configured_profile_defaults": {
                key: settings[key]
                for key in (
                    "fan_speed",
                    "mode",
                    "mop_mode",
                    "mop_intensity",
                    "cleaning_depth",
                )
            },
            "observed_profile": {
                "fan_speed": robot.observed_profile.fan_speed,
                "mode": robot.observed_profile.mode,
                "mop_mode": robot.observed_profile.mop_mode,
                "mop_intensity": robot.observed_profile.mop_intensity,
                "passes": robot.observed_profile.passes,
            },
            "adapter_id": robot.adapter_id,
            "adapter_schema_version": robot.adapter_schema_version,
            "adapter_capabilities": adapter_capabilities,
            "mop_profile_summary": robot.mop_profile_summary,
            "water_readiness": water_attributes,
            "adapter_diagnostic": robot.adapter_diagnostic,
            **_fault_fields(failure),
        }


class _RoomScheduleSensor(AdaptiveEntity, SensorEntity):
    def __init__(
        self, coordinator: AdaptiveRoboVacsCoordinator, area_id: str, name: str
    ) -> None:
        super().__init__(
            coordinator,
            f"room_{area_id}_next_clean",
            f"{name} next clean",
            "room_schedule",
            area_id=area_id,
        )
        self.area_id = area_id

    @property
    def native_value(self) -> str:
        room = self.room_view(self.area_id)
        if room.recovery:
            return "Room blocked — recovery confirmation required"
        if room.failure:
            return "Room blocked"
        if room.active:
            if room.active.phase in {
                JobPhase.RECOVERY_WAITING,
                JobPhase.COMPLETION_HELD,
            }:
                return "Completion pending"
            if room.active.phase is JobPhase.DOCK_COMPLETION_PENDING:
                return "Dock servicing"
            if room.active.phase is JobPhase.CANCELLING:
                return "Returning to dock"
            if room.active.phase is JobPhase.ERROR_WAITING:
                return "Scheduler held"
            if room.active.phase is JobPhase.PAUSED:
                return "Paused"
            if room.active_robot_state == "returning":
                return "Returning"
            return "In Progress"
        if room.next_candidate:
            return "ready now"
        if not room.enabled:
            return "disabled"
        if room.block_reason == "not due":
            return format_time_until(room.next_due, dt_util.utcnow())
        if room.block_reason in {
            "waiting for desired cleaning window",
            "unresolved occupancy; waiting for desired cleaning window",
        }:
            return format_time_until(
                room.desired_window_start,
                dt_util.as_local(dt_util.utcnow()),
            )
        return room.block_reason

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        room = self.room_view(self.area_id)
        candidate = room.next_candidate
        active = active_job_attributes(room.active)
        failure = fault_attributes(
            room.failure or (room.recovery.failure if room.recovery else None)
        )
        return {
            **super().extra_state_attributes,
            "room": room.name,
            "floor_id": room.floor_id,
            "bedroom": room.bedroom,
            "ignore_desired_window": room.ignore_desired_window,
            "desired_window_configured_start": room.desired_window_configured_start,
            "desired_window_configured_end": room.desired_window_configured_end,
            "desired_window_effective_start": room.desired_window_effective_start,
            "desired_window_effective_end": room.desired_window_effective_end,
            "desired_window_start_inherited": room.desired_window_start_inherited,
            "desired_window_end_inherited": room.desired_window_end_inherited,
            "desired_window_valid": room.desired_window_valid,
            "pass_count": room.vacuum_pass_count,
            "vacuum_pass_count": room.vacuum_pass_count,
            "mop_pass_count": room.mop_pass_count,
            "cleaning_program": (
                room.cleaning_program.value if room.cleaning_program else None
            ),
            "fan_speed": room.fan_speed,
            "mode": room.mode,
            "mop_mode": room.mop_mode,
            "mop_intensity": room.mop_intensity,
            "cleaning_depth": room.cleaning_depth,
            "effective_profiles": [
                effective_profile_attributes(item) for item in room.effective_profiles
            ],
            "latest_manual_request": manual_audit_attributes(
                room.latest_manual_request
            ),
            "effective_pass_count": (
                room.active.passes
                if room.active
                else candidate.passes
                if candidate
                else None
            ),
            "cleaning_due_at": room.next_due.isoformat(),
            "vacuum_due_at": room.next_due.isoformat(),
            "mop_due_at": None,
            "estimated_start": candidate.due_at.isoformat() if candidate else None,
            "operation": candidate.operation.value if candidate else None,
            "forecast_confidence": candidate.confidence if candidate else 0,
            "occupancy": room.occupancy,
            "occupancy_source": room.occupancy_source,
            "vacancy_diagnostic": room.vacancy_diagnostic.as_attributes(),
            "robot_eligibility": [
                eligibility_attributes(item) for item in room.robot_eligibility
            ],
            "assignment_available": room.assignment_available,
            "latest_scheduler_decision": room_decision_attributes(
                room.latest_scheduler_decision
            ),
            "map_status": room.map_status,
            "map_error": room.map_error,
            "block_reason": room.block_reason,
            "legacy_deferral_review_needed": room.legacy_deferral_review_needed,
            "desired_window_start": room.desired_window_start.isoformat(),
            "desired_window_next_start": room.desired_window_start.isoformat(),
            "unresolved_window_start": room.desired_window_start.isoformat(),
            "active_job_source": active.get("source") if active else None,
            "active_robot": room.active_robot,
            "active_robot_state": room.active_robot_state,
            "active_operation": active.get("operation") if active else None,
            "active_phase": active.get("phase") if active else None,
            "active_started_at": active.get("observed_started") if active else None,
            "expected_end_at": active.get("expected_end") if active else None,
            "active_completion_confidence": (
                active.get("completion_confidence") if active else None
            ),
            "active_hold_reason": active.get("hold_reason") if active else None,
            "learned_duration_minutes": room.effective_duration_minutes,
            "duration_sample_count": room.duration_sample_count,
            "predicted_total_minutes": room.predicted_total_minutes,
            "required_vacancy_minutes": room.required_vacancy_minutes,
            "duration_model_version": room.duration_model_version,
            "duration_model_learned": room.duration_model_learned,
            "duration_estimates_by_robot": [
                duration_estimate_attributes(item)
                for item in room.duration_estimates_by_robot
            ],
            "occurrence": occurrence_attributes(room.occurrence),
            "room_recovery": room_recovery_attributes(room.recovery),
            "water_confirmation": water_confirmation_attributes(
                room.water_confirmation
            ),
            "last_stage_outcome": room.last_stage_outcome,
            "last_stage_reason": room.last_stage_reason,
            "last_completion_confidence": (
                room.last_stage_reason
                if room.last_stage_outcome == "completed"
                else None
            ),
            "last_stage_at": (
                room.last_stage_at.isoformat() if room.last_stage_at else None
            ),
            "last_stage_summary": room.last_stage_summary,
            "water_notification_episode": water_episode_attributes(
                room.water_notification_episode
            ),
            **_fault_fields(failure),
        }


class _RoomLastCleanedSensor(AdaptiveEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self, coordinator: AdaptiveRoboVacsCoordinator, area_id: str, name: str
    ) -> None:
        super().__init__(
            coordinator,
            f"room_{area_id}_last_cleaned",
            f"{name} last cleaned",
            "room_last_cleaned",
            area_id=area_id,
        )
        self.area_id = area_id

    @property
    def native_value(self) -> datetime | None:
        return self.room_view(self.area_id).last_cleaned

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        room = self.room_view(self.area_id)
        return {
            **super().extra_state_attributes,
            "last_cleaned_display": room.last_cleaned_display,
            "using_initial_cadence_baseline": room.using_initial_cadence_baseline,
            "last_vacuum": room.last_vacuum.isoformat() if room.last_vacuum else None,
            "last_mop": room.last_mop.isoformat() if room.last_mop else None,
        }


class _RoomOccupancySensor(AdaptiveEntity, SensorEntity):
    def __init__(
        self, coordinator: AdaptiveRoboVacsCoordinator, area_id: str, name: str
    ) -> None:
        super().__init__(
            coordinator,
            f"room_{area_id}_occupancy",
            f"{name} occupancy",
            "room_occupancy",
            area_id=area_id,
        )
        self.area_id = area_id

    @property
    def native_value(self) -> str:
        return self.room_view(self.area_id).occupancy

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        room = self.room_view(self.area_id)
        return {
            **super().extra_state_attributes,
            "source": room.occupancy_source,
            "radars": list(room.radar_entity_ids),
            "motion_fallbacks": list(room.fallback_entity_ids),
            "unavailable_radars": room.unavailable_radars,
        }


class _RoomManualStatusSensor(AdaptiveEntity, SensorEntity):
    def __init__(
        self, coordinator: AdaptiveRoboVacsCoordinator, area_id: str, name: str
    ) -> None:
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
        event = self.room_view(self.area_id).latest_manual_request
        return (
            str(event.outcome or "unknown").replace("_", " ")
            if event
            else "never requested"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            **super().extra_state_attributes,
            "latest_request": manual_audit_attributes(
                self.room_view(self.area_id).latest_manual_request
            ),
        }


def _entities(coordinator: AdaptiveRoboVacsCoordinator) -> list[AdaptiveEntity]:
    entities: list[AdaptiveEntity] = [_SchedulerSensor(coordinator)]
    for robot in coordinator.data.robots:
        entities.extend(
            [
                _RobotStatusSensor(coordinator, robot.entity_id),
            ]
        )
    for room in coordinator.data.rooms:
        entities.extend(
            [
                _RoomScheduleSensor(coordinator, room.area_id, room.name),
                _RoomLastCleanedSensor(coordinator, room.area_id, room.name),
                _RoomOccupancySensor(coordinator, room.area_id, room.name),
                _RoomManualStatusSensor(coordinator, room.area_id, room.name),
            ]
        )
    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AdaptiveRoboVacsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up scheduler status entities."""

    coordinator = entry.runtime_data.coordinator
    async_setup_dynamic_entities(
        entry,
        async_add_entities,
        coordinator,
        lambda: _entities(coordinator),
    )
