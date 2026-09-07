"""Room activity and per-robot previews from immutable application views."""

from __future__ import annotations

from .models import JobPhase, cleaning_profile_is_supported
from .snapshots import RobotView, RoomRobotPreviewView, RoomView, SchedulerView


def room_status(room: RoomView) -> str:
    """Keep activity and diagnostic descriptions separate from timestamps."""

    if room.recovery:
        return "Room blocked — recovery confirmation required"
    if room.failure:
        return "Room blocked"
    if room.active:
        phases = {
            JobPhase.RECOVERY_WAITING: "Completion pending",
            JobPhase.COMPLETION_HELD: "Completion pending",
            JobPhase.DOCK_COMPLETION_PENDING: "Dock servicing",
            JobPhase.CANCELLING: "Returning to dock",
            JobPhase.ERROR_WAITING: "Scheduler held",
            JobPhase.PAUSED: "Paused",
        }
        return phases.get(
            room.active.phase,
            "Returning" if room.active_robot_state == "returning" else "In Progress",
        )
    if not room.enabled:
        return "disabled"
    if room.next_candidate:
        return "ready now"
    if room.block_reason in {
        "not due",
        "waiting for desired cleaning window",
        "unresolved occupancy; waiting for desired cleaning window",
    }:
        return "Scheduled"
    return room.block_reason


def robot_preview_reason(
    room: RoomView, robot: RobotView, scheduler: SchedulerView
) -> str | None:
    """Explain blockers without guessing when occupancy or readiness will clear."""

    if room.active or room.recovery or room.failure or not room.enabled:
        return room_status(room)
    if scheduler.storage_safe_mode:
        return "Storage recovery required"
    if scheduler.observe_only:
        return "Observe-only mode"
    if scheduler.party_mode:
        return "Party Mode"
    if not robot.ready:
        return robot.reason
    occurrence = room.occurrence
    if occurrence:
        if (
            not occurrence.manual_override
            and occurrence.robot_entity_id != robot.entity_id
        ):
            return "Occurrence assigned to another robot"
        if occurrence.current_stage >= len(occurrence.stages):
            return "Completion pending"
        stage = occurrence.stages[occurrence.current_stage]
        if not robot.adapter_capabilities.supports(stage.operation, stage.passes):
            return "Robot does not support the scheduled stage"
        if stage.cleaning_profile and not cleaning_profile_is_supported(
            stage.cleaning_profile, robot.adapter_capabilities
        ):
            return "Stored cleaning profile is not compatible"
    elif not any(
        profile.compatible and profile.robot_entity_id == robot.entity_id
        for profile in room.effective_profiles
    ):
        return "Cleaning profile is not compatible"
    if room.water_confirmation and room.water_confirmation.status == "pending":
        return "Waiting for water confirmation"
    if room.next_clean_at is None:
        return "No valid schedule time"
    return None


def room_robot_previews(
    room: RoomView, robots: tuple[RobotView, ...], scheduler: SchedulerView
) -> tuple[RoomRobotPreviewView, ...]:
    """Membership follows the registry floor, regardless of current candidates."""

    return tuple(
        RoomRobotPreviewView(
            robot.entity_id,
            "blocked"
            if (reason := robot_preview_reason(room, robot, scheduler))
            else "conditional",
            reason,
        )
        for robot in robots
        if robot.floor_id == room.floor_id
    )
