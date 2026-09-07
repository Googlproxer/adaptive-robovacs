"""Stable Repairs identifiers and safe user-facing failure summaries."""

from __future__ import annotations

FAULT_SUMMARIES = {
    "area_mapping_missing": (
        "The selected room is not mapped to this vacuum in Home Assistant."
    ),
    "area_mapping_stale": "The Home Assistant room mapping is stale for this vacuum.",
    "area_mapping_ambiguous": (
        "The Home Assistant room mapping cannot be matched safely to one vacuum map."
    ),
    "area_mapping_recheck_required": (
        "Home Assistant refreshed this vacuum's room mapping; confirm it before "
        "scheduling resumes."
    ),
    "two_pass_no_longer_supported": (
        "The selected vacuum no longer supports native two-pass cleaning."
    ),
    "adapter_request_unsupported": (
        "The selected vacuum no longer supports this cleaning request."
    ),
    "adapter_preflight_failed": (
        "The vacuum adapter could not validate this cleaning request."
    ),
    "profile_apply_failed": "The vacuum cleaning profile could not be applied.",
    "profile_validation_failed": "The vacuum cleaning profile could not be validated.",
    "profile_option_unsupported": (
        "A saved cleaning profile option is no longer supported."
    ),
    "profile_control_unavailable": "A required vacuum profile control is unavailable.",
    "generic_dispatch_failed": "Home Assistant could not start the room clean.",
    "native_dispatch_failed": (
        "The vacuum vendor command could not start the room clean."
    ),
    "start_confirmation_failed": "The vacuum did not confirm that cleaning started.",
    "start_outcome_uncertain": (
        "The integration cannot safely confirm whether the cleaning command started."
    ),
    "native_cleaning_zero_duration": (
        "The vacuum reported that the room clean took zero minutes."
    ),
    "unrecognized_adapter_failure": (
        "The vacuum adapter reported an unrecognized cleaning failure."
    ),
}


def fault_summary(reason_code: str) -> str:
    """Return a stable safe user-facing summary."""

    return FAULT_SUMMARIES.get(
        reason_code,
        "A scheduler cleaning request failed and requires user attention.",
    )


def room_recovery_summary(category: str) -> str:
    """Describe only normalized interruption categories."""

    return {
        "robot_trapped": "The vacuum reported that it was trapped.",
        "brush_jammed": "The vacuum reported a jammed brush.",
        "wheels_jammed": "The vacuum reported jammed wheels.",
        "sensor_error": "The vacuum reported a sensor problem.",
    }.get(category, "The vacuum reported an error during this room clean.")


def room_recovery_issue_id(entry_id: str, area_id: str) -> str:
    return f"room_recovery_{entry_id}_{area_id}"


def robot_error_recovery_issue_id(entry_id: str, registry_id: str) -> str:
    return f"robot_error_recovery_{entry_id}_{registry_id}"


def retired_map_hold_issue_id(entry_id: str, registry_id: str) -> str:
    return f"retired_map_hold_{entry_id}_{registry_id}"


def scheduler_halted_issue_id(entry_id: str) -> str:
    """Return the legacy global-halt issue ID removed by schema 16."""

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
    """Return the stable room-program compatibility Repair ID."""

    return f"cleaning_program_incompatible_{entry_id}_{area_id}"
