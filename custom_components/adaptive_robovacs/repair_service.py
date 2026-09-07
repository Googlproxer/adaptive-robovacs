"""Independent Home Assistant Repairs infrastructure."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .discovery import DiscoveredRobot, DiscoveredRoom
from .models import (
    cleaning_profile_is_supported,
    effective_cleaning_program,
    expand_cleaning_program,
    map_recovery_hold_is_manual,
    resolve_cleaning_profile,
    stage_pass_count,
)
from .repairs_manager import (
    cleaning_program_issue_id,
    fault_summary,
    notification_delivery_issue_id,
    retired_map_hold_issue_id,
    robot_dispatch_fault_issue_id,
    robot_error_recovery_issue_id,
    room_dispatch_fault_issue_id,
    room_recovery_issue_id,
    room_recovery_summary,
    scheduler_halted_issue_id,
    two_pass_issue_id,
)
from .state import (
    CleaningOccurrence,
    RobotHold,
    RobotSettings,
    RoomRecovery,
    RoomSettings,
    SchedulerFault,
)


class RepairService:
    """Create and clear infrastructure-level scheduler Repairs."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id

    @property
    def storage_issue_id(self) -> str:
        return f"storage_unsafe_{self._entry_id}"

    def set_storage_unsafe(self, active: bool, reason: str | None = None) -> None:
        if not active:
            ir.async_delete_issue(self._hass, DOMAIN, self.storage_issue_id)
            return
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            self.storage_issue_id,
            is_fixable=False,
            is_persistent=True,
            severity=ir.IssueSeverity.ERROR,
            translation_key="storage_unsafe",
            translation_placeholders={"reason": reason or "validation failed"},
            data={"entry_id": self._entry_id},
        )

    def unresolved_issue_id(self, legacy_key: str) -> str:
        return f"unresolved_robot_reference_{self._entry_id}_{legacy_key}"

    def sync_unresolved_robot_references(
        self, unresolved_keys: tuple[str, ...]
    ) -> None:
        expected = {
            self.unresolved_issue_id(legacy_key) for legacy_key in unresolved_keys
        }
        registry = ir.async_get(self._hass)
        prefix = f"unresolved_robot_reference_{self._entry_id}_"
        for (domain, issue_id), _issue in tuple(registry.issues.items()):
            if (
                domain == DOMAIN
                and issue_id.startswith(prefix)
                and issue_id not in expected
            ):
                ir.async_delete_issue(self._hass, DOMAIN, issue_id)
        for legacy_key in unresolved_keys:
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                self.unresolved_issue_id(legacy_key),
                is_fixable=False,
                is_persistent=True,
                severity=ir.IssueSeverity.ERROR,
                translation_key="unresolved_robot_reference",
                translation_placeholders={"legacy_key": legacy_key},
                data={"entry_id": self._entry_id, "legacy_key": legacy_key},
            )

    def set_notification_delivery_issue(self, active: bool) -> None:
        """Create or clear the actionable all-user delivery Repair."""

        issue_id = notification_delivery_issue_id(self._entry_id)
        if not active:
            ir.async_delete_issue(self._hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="notification_delivery_failed",
            translation_placeholders={},
            data={"entry_id": self._entry_id},
        )

    def sync_dispatch_faults(
        self,
        robot_faults: Mapping[str, SchedulerFault],
        room_faults: Mapping[str, SchedulerFault],
        robots: Iterable[DiscoveredRobot],
        rooms: Mapping[str, DiscoveredRoom],
    ) -> None:
        """Create scoped Repairs from typed fault state."""

        robot_by_registry_id = {robot.registry_id: robot for robot in robots}
        ir.async_delete_issue(
            self._hass,
            DOMAIN,
            scheduler_halted_issue_id(self._entry_id),
        )
        for registry_id, fault in robot_faults.items():
            robot = robot_by_registry_id.get(registry_id)
            room = rooms.get(fault.room_area_id)
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                robot_dispatch_fault_issue_id(self._entry_id, registry_id),
                is_fixable=True,
                is_persistent=True,
                severity=ir.IssueSeverity.ERROR,
                translation_key="robot_dispatch_fault",
                translation_placeholders={
                    "robot": robot.name if robot else "the selected vacuum",
                    "room": room.name if room else "the selected room",
                    "reason": fault_summary(fault.reason_code),
                },
                data={
                    "entry_id": self._entry_id,
                    "robot_registry_id": registry_id,
                },
            )
        for area_id, fault in room_faults.items():
            room = rooms.get(area_id)
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                room_dispatch_fault_issue_id(self._entry_id, area_id),
                is_fixable=True,
                is_persistent=True,
                severity=ir.IssueSeverity.ERROR,
                translation_key="room_dispatch_fault",
                translation_placeholders={
                    "room": room.name if room else "the selected room",
                    "reason": fault_summary(fault.reason_code),
                },
                data={"entry_id": self._entry_id, "area_id": area_id},
            )

    def sync_room_recoveries(
        self,
        recoveries: Mapping[str, RoomRecovery],
        robots: Iterable[DiscoveredRobot],
        rooms: Mapping[str, DiscoveredRoom],
    ) -> None:
        """Recreate durable room Repairs and remove only stale issues we own."""

        prefix = room_recovery_issue_id(self._entry_id, "")
        expected = {
            room_recovery_issue_id(self._entry_id, area_id) for area_id in recoveries
        }
        for domain, issue_id in tuple(ir.async_get(self._hass).issues):
            if (
                domain == DOMAIN
                and issue_id.startswith(prefix)
                and issue_id not in expected
            ):
                ir.async_delete_issue(self._hass, DOMAIN, issue_id)
        by_registry = {robot.registry_id: robot for robot in robots}
        for area_id, recovery in recoveries.items():
            room = rooms.get(area_id)
            robot = by_registry.get(recovery.robot_registry_id)
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                room_recovery_issue_id(self._entry_id, area_id),
                is_fixable=True,
                is_persistent=True,
                severity=ir.IssueSeverity.ERROR,
                translation_key="room_error_recovery",
                translation_placeholders={
                    "room": room.name if room else "the affected room",
                    "robot": robot.name if robot else "the vacuum",
                    "operation": str(recovery.operation),
                    "reason": room_recovery_summary(recovery.error_category),
                },
                data={
                    "entry_id": self._entry_id,
                    "area_id": area_id,
                    "recovery_id": recovery.recovery_id,
                },
            )

    def set_robot_error_recovery(
        self, registry_id: str, held_at: datetime, robot: DiscoveredRobot | None
    ) -> None:
        """Offer explicit abandonment when a legacy job cannot bind to a room."""

        ir.async_create_issue(
            self._hass,
            DOMAIN,
            robot_error_recovery_issue_id(self._entry_id, registry_id),
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.ERROR,
            translation_key="robot_error_recovery",
            translation_placeholders={"robot": robot.name if robot else "the vacuum"},
            data={
                "entry_id": self._entry_id,
                "robot_registry_id": registry_id,
                "held_at": held_at.isoformat(),
            },
        )

    def sync_retired_map_holds(
        self, holds: Mapping[str, RobotHold], robots: Iterable[DiscoveredRobot]
    ) -> None:
        """Keep a confirmation path only for maintenance holds from older releases."""

        pending = {
            key: hold
            for key, hold in holds.items()
            if map_recovery_hold_is_manual(hold.reason)
        }
        expected = {retired_map_hold_issue_id(self._entry_id, key) for key in pending}
        registry = ir.async_get(self._hass)
        for (domain, issue_id), issue in tuple(registry.issues.items()):
            if (
                domain == DOMAIN
                and issue.translation_key == "retired_map_hold"
                and issue.data
                and issue.data.get("entry_id") == self._entry_id
                and issue_id not in expected
            ):
                ir.async_delete_issue(self._hass, DOMAIN, issue_id)
        names = {robot.registry_id: robot.name for robot in robots}
        for key, hold in pending.items():
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                retired_map_hold_issue_id(self._entry_id, key),
                is_fixable=True,
                is_persistent=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key="retired_map_hold",
                translation_placeholders={"robot": names.get(key, "the vacuum")},
                data={
                    "entry_id": self._entry_id,
                    "robot_registry_id": key,
                    "held_at": hold.held_at.isoformat() if hold.held_at else "",
                },
            )

    def delete_robot_error_recovery(self, registry_id: str) -> None:
        ir.async_delete_issue(
            self._hass,
            DOMAIN,
            robot_error_recovery_issue_id(self._entry_id, registry_id),
        )

    def delete_robot_dispatch_fault(self, robot_registry_id: str) -> None:
        """Delete one resolved robot fault Repair."""

        ir.async_delete_issue(
            self._hass,
            DOMAIN,
            robot_dispatch_fault_issue_id(
                self._entry_id,
                robot_registry_id,
            ),
        )

    def delete_room_dispatch_fault(self, area_id: str) -> None:
        """Delete one resolved room fault Repair."""

        ir.async_delete_issue(
            self._hass,
            DOMAIN,
            room_dispatch_fault_issue_id(self._entry_id, area_id),
        )

    def sync_two_pass_issues(
        self,
        room_settings: Mapping[str, RoomSettings],
        rooms: Mapping[str, DiscoveredRoom],
        robots: Iterable[DiscoveredRobot],
    ) -> None:
        """Create or delete per-room two-pass compatibility issues."""

        robot_list = tuple(robots)
        for area_id, settings in room_settings.items():
            issue_id = two_pass_issue_id(self._entry_id, area_id)
            room = rooms.get(area_id)
            compatible = bool(
                room
                and any(
                    robot.floor_id == room.floor_id
                    and robot.supports_area_clean
                    and 2 in robot.adapter_capabilities.supported_pass_counts
                    for robot in robot_list
                )
            )
            if room is None or settings.vacuum_pass_count != 2 or compatible:
                ir.async_delete_issue(self._hass, DOMAIN, issue_id)
                continue
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                issue_id,
                is_fixable=True,
                is_persistent=True,
                severity=ir.IssueSeverity.ERROR,
                translation_key="two_pass_no_longer_supported",
                translation_placeholders={"room": room.name},
                data={
                    "entry_id": self._entry_id,
                    "area_id": area_id,
                    "issue_type": "two_pass_no_longer_supported",
                },
            )

    def sync_cleaning_program_issues(
        self,
        room_settings: Mapping[str, RoomSettings],
        robot_settings: Mapping[str, RobotSettings],
        occurrences: Mapping[str, CleaningOccurrence],
        rooms: Mapping[str, DiscoveredRoom],
        robots: Iterable[DiscoveredRobot],
    ) -> None:
        """Sync Repairs for room programs no discovered adapter can execute."""

        robot_list = tuple(robots)
        for area_id, settings in room_settings.items():
            issue_id = cleaning_program_issue_id(self._entry_id, area_id)
            room = rooms.get(area_id)
            occurrence = occurrences.get(area_id)
            compatible = False
            if room and settings.enabled:
                for robot in robot_list:
                    if robot.floor_id != room.floor_id or not robot.supports_area_clean:
                        continue
                    if occurrence and occurrence.robot_registry_id != robot.registry_id:
                        continue
                    robot_policy = robot_settings.get(robot.registry_id)
                    if robot_policy is None or not robot_policy.enabled:
                        continue
                    if occurrence:
                        if occurrence.current_stage >= len(occurrence.stages):
                            continue
                        stage = occurrence.stages[occurrence.current_stage]
                        if robot.adapter_capabilities.supports(
                            stage.operation,
                            stage.passes,
                        ) and (
                            not stage.cleaning_profile
                            or cleaning_profile_is_supported(
                                stage.cleaning_profile,
                                robot.adapter_capabilities,
                            )
                        ):
                            compatible = True
                            break
                        continue
                    program = effective_cleaning_program(
                        settings.cleaning_program,
                        robot_policy.cleaning_program,
                    )
                    operations = expand_cleaning_program(program or "")
                    if operations and all(
                        (
                            passes := stage_pass_count(
                                operation,
                                settings.vacuum_pass_count,
                                settings.mop_pass_count,
                                robot_policy.double_pass,
                                robot_policy.mop_double_pass,
                                robot.adapter_capabilities,
                            )
                        )
                        is not None
                        and robot.adapter_capabilities.supports(operation, passes)
                        and resolve_cleaning_profile(
                            operation,
                            settings,
                            robot_policy,
                            robot.adapter_capabilities,
                        )
                        is not None
                        for operation in operations
                    ):
                        compatible = True
                        break
            if room is None or not settings.enabled or compatible:
                ir.async_delete_issue(self._hass, DOMAIN, issue_id)
                continue
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                issue_id,
                is_fixable=True,
                is_persistent=True,
                severity=ir.IssueSeverity.ERROR,
                translation_key="cleaning_program_incompatible",
                translation_placeholders={"room": room.name},
                data={"entry_id": self._entry_id, "area_id": area_id},
            )
