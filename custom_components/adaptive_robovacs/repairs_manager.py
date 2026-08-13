"""Home Assistant Repairs lifecycle for actionable scheduler failures."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

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
    "generic_dispatch_failed": "Home Assistant could not start the room clean.",
    "native_dispatch_failed": "The vacuum vendor command could not start the room clean.",
    "start_confirmation_failed": "The vacuum did not confirm that cleaning started.",
    "start_outcome_uncertain": "The integration cannot safely confirm whether the cleaning command started.",
}


def fault_summary(reason_code: str) -> str:
    """Return a stable safe user-facing summary."""

    return FAULT_SUMMARIES.get(
        reason_code,
        "A scheduler cleaning request failed and requires user attention.",
    )


def scheduler_halted_issue_id(entry_id: str) -> str:
    """Return a stable per-config-entry issue ID."""

    return f"scheduler_halted_{entry_id}"


def two_pass_issue_id(entry_id: str, area_id: str) -> str:
    """Return a stable capability issue ID using registry identities only."""

    return f"two_pass_no_longer_supported_{entry_id}_{area_id}"


def async_create_scheduler_halted_issue(
    coordinator: AdaptiveRoboVacCoordinator,
) -> None:
    """Create or refresh the single persistent scheduler halt Repair."""

    fault = coordinator.data.get("scheduler_fault")
    if not fault:
        return
    robot = coordinator.robot_for_registry_id(str(fault["robot_registry_id"]))
    room = coordinator.discovery.rooms.get(str(fault["room_area_id"]))
    ir.async_create_issue(
        coordinator.hass,
        DOMAIN,
        scheduler_halted_issue_id(coordinator.entry.entry_id),
        is_fixable=True,
        is_persistent=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key="scheduler_halted",
        translation_placeholders={
            "robot": robot.name if robot else "the selected vacuum",
            "room": room.name if room else "the selected room",
            "reason": fault_summary(str(fault["reason_code"])),
        },
        data={"entry_id": coordinator.entry.entry_id},
    )


def async_delete_scheduler_halted_issue(
    coordinator: AdaptiveRoboVacCoordinator,
) -> None:
    """Delete the halt issue only after explicit successful resume."""

    ir.async_delete_issue(
        coordinator.hass,
        DOMAIN,
        scheduler_halted_issue_id(coordinator.entry.entry_id),
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
