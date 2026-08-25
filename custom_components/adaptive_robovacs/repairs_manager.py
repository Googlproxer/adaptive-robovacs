"""Home Assistant Repairs lifecycle for actionable scheduler failures."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .models import (
    effective_cleaning_program,
    cleaning_profile_is_supported,
    expand_cleaning_program,
    resolve_cleaning_profile,
    stage_pass_count,
)

if TYPE_CHECKING:
    from .coordinator import AdaptiveRoboVacCoordinator


FAULT_SUMMARIES = {
    "area_mapping_missing": "The selected room is not mapped to this vacuum in Home Assistant.",
    "area_mapping_stale": "The Home Assistant room mapping is stale for this vacuum.",
    "area_mapping_ambiguous": "The Home Assistant room mapping cannot be matched safely to one vacuum map.",
    "two_pass_no_longer_supported": "The selected vacuum no longer supports native two-pass cleaning.",
    "adapter_request_unsupported": "The selected vacuum no longer supports this cleaning request.",
    "adapter_preflight_failed": "The vacuum adapter could not validate this cleaning request.",
    "profile_apply_failed": "The vacuum cleaning profile could not be applied.",
    "profile_validation_failed": "The vacuum cleaning profile could not be validated.",
    "profile_option_unsupported": "A saved cleaning profile option is no longer supported.",
    "profile_control_unavailable": "A required vacuum profile control is unavailable.",
    "generic_dispatch_failed": "Home Assistant could not start the room clean.",
    "native_dispatch_failed": "The vacuum vendor command could not start the room clean.",
    "start_confirmation_failed": "The vacuum did not confirm that cleaning started.",
    "start_outcome_uncertain": "The integration cannot safely confirm whether the cleaning command started.",
    "native_cleaning_zero_duration": "The vacuum reported that the room clean took zero minutes.",
}


def fault_summary(reason_code: str) -> str:
    """Return a stable safe user-facing summary."""

    return FAULT_SUMMARIES.get(
        reason_code,
        "A scheduler cleaning request failed and requires user attention.",
    )


def scheduler_halted_issue_id(entry_id: str) -> str:
    """Return the legacy global-halt issue ID for cleanup during migration."""

    return f"scheduler_halted_{entry_id}"


def robot_dispatch_fault_issue_id(entry_id: str, robot_registry_id: str) -> str:
    """Return a stable robot-scoped dispatch Repair ID."""

    return f"robot_dispatch_fault_{entry_id}_{robot_registry_id}"


def room_dispatch_fault_issue_id(entry_id: str, area_id: str) -> str:
    """Return a stable room-scoped configuration Repair ID."""

    return f"room_dispatch_fault_{entry_id}_{area_id}"


def two_pass_issue_id(entry_id: str, area_id: str) -> str:
    """Return a stable capability issue ID using registry identities only."""

    return f"two_pass_no_longer_supported_{entry_id}_{area_id}"


def notification_delivery_issue_id(entry_id: str) -> str:
    """Return the stable issue ID for unreachable Companion targets."""

    return f"notification_delivery_failed_{entry_id}"


def cleaning_program_issue_id(entry_id: str, area_id: str) -> str:
    return f"cleaning_program_incompatible_{entry_id}_{area_id}"


def async_set_notification_delivery_issue(
    coordinator: AdaptiveRoboVacCoordinator, active: bool
) -> None:
    """Create or clear the actionable all-user delivery Repair."""

    issue_id = notification_delivery_issue_id(coordinator.entry.entry_id)
    if not active:
        ir.async_delete_issue(coordinator.hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        coordinator.hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="notification_delivery_failed",
        translation_placeholders={},
        data={"entry_id": coordinator.entry.entry_id},
    )


def async_sync_dispatch_fault_issues(
    coordinator: AdaptiveRoboVacCoordinator,
) -> None:
    """Create scoped Repairs without turning a single robot fault global."""

    ir.async_delete_issue(
        coordinator.hass, DOMAIN, scheduler_halted_issue_id(coordinator.entry.entry_id)
    )
    for registry_id, fault in coordinator.data.get("robot_faults", {}).items():
        robot = coordinator.robot_for_registry_id(str(registry_id))
        room = coordinator.discovery.rooms.get(str(fault.get("room_area_id", "")))
        ir.async_create_issue(
            coordinator.hass,
            DOMAIN,
            robot_dispatch_fault_issue_id(coordinator.entry.entry_id, str(registry_id)),
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.ERROR,
            translation_key="robot_dispatch_fault",
            translation_placeholders={
                "robot": robot.name if robot else "the selected vacuum",
                "room": room.name if room else "the selected room",
                "reason": fault_summary(str(fault.get("reason_code", ""))),
            },
            data={
                "entry_id": coordinator.entry.entry_id,
                "robot_registry_id": str(registry_id),
            },
        )
    for area_id, fault in coordinator.data.get("room_faults", {}).items():
        room = coordinator.discovery.rooms.get(str(area_id))
        ir.async_create_issue(
            coordinator.hass,
            DOMAIN,
            room_dispatch_fault_issue_id(coordinator.entry.entry_id, str(area_id)),
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.ERROR,
            translation_key="room_dispatch_fault",
            translation_placeholders={
                "room": room.name if room else "the selected room",
                "reason": fault_summary(str(fault.get("reason_code", ""))),
            },
            data={"entry_id": coordinator.entry.entry_id, "area_id": str(area_id)},
        )


def async_create_scheduler_halted_issue(
    coordinator: AdaptiveRoboVacCoordinator,
) -> None:
    """Compatibility wrapper for the former global-halt call site."""

    async_sync_dispatch_fault_issues(coordinator)


def async_delete_scheduler_halted_issue(
    coordinator: AdaptiveRoboVacCoordinator,
) -> None:
    """Delete the halt issue only after explicit successful resume."""

    ir.async_delete_issue(
        coordinator.hass,
        DOMAIN,
        scheduler_halted_issue_id(coordinator.entry.entry_id),
    )


def async_delete_robot_dispatch_fault_issue(
    coordinator: AdaptiveRoboVacCoordinator, robot_registry_id: str
) -> None:
    ir.async_delete_issue(
        coordinator.hass,
        DOMAIN,
        robot_dispatch_fault_issue_id(coordinator.entry.entry_id, robot_registry_id),
    )


def async_delete_room_dispatch_fault_issue(
    coordinator: AdaptiveRoboVacCoordinator, area_id: str
) -> None:
    ir.async_delete_issue(
        coordinator.hass,
        DOMAIN,
        room_dispatch_fault_issue_id(coordinator.entry.entry_id, area_id),
    )


def async_sync_two_pass_issues(coordinator: AdaptiveRoboVacCoordinator) -> None:
    """Create or delete per-room two-pass compatibility issues."""

    for area_id, settings in coordinator.data["settings"]["rooms"].items():
        issue_id = two_pass_issue_id(coordinator.entry.entry_id, area_id)
        room = coordinator.discovery.rooms.get(area_id)
        compatible = bool(
            room
            and any(
                robot.floor_id == room.floor_id
                and robot.supports_area_clean
                and 2 in robot.adapter_capabilities.supported_pass_counts
                for robot in coordinator.discovery.robots.values()
            )
        )
        if room is None or settings.get("pass_count") != 2 or compatible:
            ir.async_delete_issue(coordinator.hass, DOMAIN, issue_id)
            continue
        ir.async_create_issue(
            coordinator.hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.ERROR,
            translation_key="two_pass_no_longer_supported",
            translation_placeholders={
                "room": room.name if room else "the selected room"
            },
            data={
                "entry_id": coordinator.entry.entry_id,
                "area_id": area_id,
                "issue_type": "two_pass_no_longer_supported",
            },
        )


def async_sync_cleaning_program_issues(
    coordinator: AdaptiveRoboVacCoordinator,
) -> None:
    """Sync Repairs for saved room programs no adapter can execute."""

    for area_id, settings in coordinator.data["settings"]["rooms"].items():
        issue_id = cleaning_program_issue_id(coordinator.entry.entry_id, area_id)
        room = coordinator.discovery.rooms.get(area_id)
        occurrence = coordinator.data.get("occurrences", {}).get(area_id)
        compatible = False
        if room and settings.get("enabled", True):
            for robot in coordinator.discovery.robots.values():
                if robot.floor_id != room.floor_id or not robot.supports_area_clean:
                    continue
                if occurrence and occurrence.get("robot_registry_id") != robot.registry_id:
                    continue
                robot_settings = coordinator._robot_settings(robot)
                if not robot_settings.get("enabled", True):
                    continue
                if occurrence:
                    stage_index = int(occurrence.get("current_stage", 0))
                    stages = occurrence.get("stages", [])
                    if stage_index >= len(stages):
                        continue
                    stage = stages[stage_index]
                    operation = str(stage.get("operation"))
                    profile = stage.get("cleaning_profile")
                    if (
                        robot.adapter_capabilities.supports(
                            operation, int(stage.get("passes", 1))
                        )
                        and (
                            not isinstance(profile, dict)
                            or not profile
                            or cleaning_profile_is_supported(
                                profile, robot.adapter_capabilities
                            )
                        )
                    ):
                        compatible = True
                        break
                    continue
                program = effective_cleaning_program(
                    settings.get("cleaning_program"),
                    str(robot_settings.get("cleaning_program", "vacuum_only")),
                )
                operations = expand_cleaning_program(program or "")
                if not operations:
                    continue
                if all(
                    (
                        passes := stage_pass_count(
                            operation,
                            settings.get("vacuum_pass_count"),
                            settings.get("mop_pass_count"),
                            bool(robot_settings.get("double_pass")),
                            bool(robot_settings.get("mop_double_pass")),
                            robot.adapter_capabilities,
                        )
                    ) is not None
                    and robot.adapter_capabilities.supports(operation, passes)
                    and resolve_cleaning_profile(
                        operation,
                        settings,
                        robot_settings,
                        robot.adapter_capabilities,
                    )
                    is not None
                    for operation in operations
                ):
                    compatible = True
                    break
        if room is None or not settings.get("enabled", True) or compatible:
            ir.async_delete_issue(coordinator.hass, DOMAIN, issue_id)
            continue
        ir.async_create_issue(
            coordinator.hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.ERROR,
            translation_key="cleaning_program_incompatible",
            translation_placeholders={"room": room.name},
            data={"entry_id": coordinator.entry.entry_id, "area_id": area_id},
        )
