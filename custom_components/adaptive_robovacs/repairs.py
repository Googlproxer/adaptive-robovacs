"""Repair flows for Adaptive RoboVacs scheduler failures."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .commands import (
    AcknowledgeRetiredMapCommand,
    AcknowledgeRobotErrorCommand,
    AcknowledgeRoomRecoveryCommand,
    RecheckAndResumeCommand,
    RecheckCleaningProgramCommand,
    RecheckNotificationTargetsCommand,
    RecheckRoomFaultCommand,
    RecheckTwoPassCompatibilityCommand,
    ResetIncompatibleSettingsCommand,
    ResetUnsafeStoreCommand,
    SchedulerCommand,
    SchedulerCommandResult,
)
from .const import DOMAIN
from .repairs_manager import (
    cleaning_program_issue_id,
    notification_delivery_issue_id,
    retired_map_hold_issue_id,
    robot_dispatch_fault_issue_id,
    robot_error_recovery_issue_id,
    room_dispatch_fault_issue_id,
    room_recovery_issue_id,
    two_pass_issue_id,
)
from .runtime_data import AdaptiveRoboVacsRuntimeData

type CommandSubmitter = Callable[[SchedulerCommand], Awaitable[SchedulerCommandResult]]


def _description_placeholders(flow: RepairsFlow) -> dict[str, str] | None:
    """Return the issue placeholders used by this Repair flow."""

    issue = ir.async_get(flow.hass).async_get_issue(flow.handler, flow.issue_id)
    return issue.translation_placeholders if issue else None


class RobotDispatchFaultRepairFlow(RepairsFlow):
    """Recheck one held robot without dispatching cleaning work."""

    def __init__(
        self,
        submit: CommandSubmitter,
        robot_registry_id: str,
    ) -> None:
        self._submit = submit
        self._robot_registry_id = robot_registry_id

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Start the scheduler-halt repair flow."""

        # Opening a Repair must never be treated as submitting its confirmation.
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Require an explicit confirmation after a non-dispatching recheck."""

        errors: dict[str, str] = {}
        if user_input is not None:
            result = await self._submit(
                RecheckAndResumeCommand(self._robot_registry_id)
            )
            response = result.as_response() if result else {}
            if response.get("cleared"):
                return self.async_create_entry(title="", data={})
            errors["base"] = str(response.get("reason", "recheck_failed"))
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders=_description_placeholders(self),
        )


class ErrorRecoveryRepairFlow(RepairsFlow):
    """Confirm one interruption episode without dispatching physical work."""

    def __init__(
        self,
        submit: CommandSubmitter,
        command: AcknowledgeRoomRecoveryCommand
        | AcknowledgeRobotErrorCommand
        | AcknowledgeRetiredMapCommand,
    ) -> None:
        self._submit = submit
        self._command = command

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            result = await self._submit(self._command)
            response = result.as_response() if result else {}
            if response.get("cleared"):
                return self.async_create_entry(title="", data={})
            errors["base"] = str(response.get("reason", "recovery_unavailable"))
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders=_description_placeholders(self),
        )


class RoomDispatchFaultRepairFlow(RepairsFlow):
    """Recheck one blocked room configuration without dispatching work."""

    def __init__(
        self, submit: CommandSubmitter, area_id: str, *, allow_reset: bool = True
    ) -> None:
        self._submit = submit
        self._area_id = area_id
        self._allow_reset = allow_reset
        self._reset_scope = "room"

    def _action_schema(self) -> vol.Schema:
        choices = {"recheck": "Recheck"}
        if self._allow_reset:
            choices.update(
                {
                    "reset_room": "Reset room cleaning settings",
                    "reset_room_and_pending": (
                        "Reset room settings and discard pending attempt"
                    ),
                    "reset_robot": "Reset robot cleaning settings",
                    "reset_robot_and_pending": (
                        "Reset robot settings and discard pending attempt"
                    ),
                }
            )
        return vol.Schema({vol.Optional("action", default="recheck"): vol.In(choices)})

    async def async_step_reset_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Require a second deliberate action before changing saved settings."""

        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("confirm_reset") is True:
                result = await self._submit(
                    ResetIncompatibleSettingsCommand(self._area_id, self._reset_scope)
                )
                response = result.as_response() if result else {}
                if response.get("cleared"):
                    return self.async_create_entry(title="", data={})
                errors["base"] = str(response.get("reason", "reset_failed"))
            else:
                errors["base"] = "confirmation_required"
        return self.async_show_form(
            step_id="reset_confirm",
            data_schema=vol.Schema(
                {vol.Required("confirm_reset", default=False): bool}
            ),
            errors=errors,
            description_placeholders={
                **(_description_placeholders(self) or {}),
                "scope": self._reset_scope.replace("_", " "),
            },
        )

    async def _reset_selected(
        self, user_input: dict[str, Any]
    ) -> data_entry_flow.FlowResult | None:
        action = user_input.get("action", "recheck")
        if action == "recheck":
            return None
        if not self._allow_reset or action not in {
            "reset_room",
            "reset_room_and_pending",
            "reset_robot",
            "reset_robot_and_pending",
        }:
            return self.async_show_form(
                step_id="confirm",
                data_schema=self._action_schema(),
                errors={"base": "invalid_reset_scope"},
                description_placeholders=_description_placeholders(self),
            )
        self._reset_scope = action.removeprefix("reset_")
        return await self.async_step_reset_confirm()

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if selected := await self._reset_selected(user_input):
                return selected
            result = await self._submit(RecheckRoomFaultCommand(self._area_id))
            response = result.as_response() if result else {}
            if response.get("cleared"):
                return self.async_create_entry(title="", data={})
            errors["base"] = "recheck_failed"
        return self.async_show_form(
            step_id="confirm",
            data_schema=self._action_schema(),
            errors=errors,
            description_placeholders=_description_placeholders(self),
        )


class TwoPassCompatibilityRepairFlow(RoomDispatchFaultRepairFlow):
    """Recheck whether a room again has a compatible two-pass vacuum."""

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        # Home Assistant may supply initial flow data when opening a Repair.
        # Deliberately discard it until the user submits the confirmation form.
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if selected := await self._reset_selected(user_input):
                return selected
            result = await self._submit(
                RecheckTwoPassCompatibilityCommand(self._area_id)
            )
            response = result.as_response() if result else {}
            if response.get("cleared"):
                return self.async_create_entry(title="", data={})
            errors["base"] = "recheck_failed"
        return self.async_show_form(
            step_id="confirm",
            data_schema=self._action_schema(),
            errors=errors,
            description_placeholders=_description_placeholders(self),
        )


class NotificationDeliveryRepairFlow(RepairsFlow):
    """Recheck whether at least one Companion notification target exists."""

    def __init__(self, submit: CommandSubmitter) -> None:
        self._submit = submit

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        # Do not let opening this issue recheck and resolve it automatically.
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            result = await self._submit(RecheckNotificationTargetsCommand())
            response = result.as_response() if result else {}
            if response.get("cleared"):
                return self.async_create_entry(title="", data={})
            errors["base"] = "recheck_failed"
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders=_description_placeholders(self),
        )


class CleaningProgramCompatibilityRepairFlow(TwoPassCompatibilityRepairFlow):
    """Recheck a room's complete ordered program without dispatching."""

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if selected := await self._reset_selected(user_input):
                return selected
            result = await self._submit(RecheckCleaningProgramCommand(self._area_id))
            response = result.as_response() if result else {}
            if response.get("cleared"):
                return self.async_create_entry(title="", data={})
            errors["base"] = "recheck_failed"
        return self.async_show_form(
            step_id="confirm",
            data_schema=self._action_schema(),
            errors=errors,
            description_placeholders=_description_placeholders(self),
        )


class UnsafeStoreRepairFlow(RepairsFlow):
    """Offer a backed-up reset only after explicit user confirmation."""

    def __init__(self, submit: CommandSubmitter) -> None:
        self._submit = submit

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("confirm_reset") is True:
                result = await self._submit(ResetUnsafeStoreCommand())
                response = result.as_response() if result else {}
                if response.get("cleared"):
                    return self.async_create_entry(title="", data={})
                errors["base"] = str(response.get("reason", "reset_failed"))
            else:
                errors["base"] = "confirmation_required"
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema(
                {vol.Required("confirm_reset", default=False): bool}
            ),
            errors=errors,
            description_placeholders=_description_placeholders(self),
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the matching repair flow."""

    entry_id = str((data or {}).get("entry_id", ""))
    runtime = next(
        (
            candidate
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.entry_id == entry_id
            and entry.state is ConfigEntryState.LOADED
            and isinstance(
                (candidate := getattr(entry, "runtime_data", None)),
                AdaptiveRoboVacsRuntimeData,
            )
        ),
        None,
    )
    if runtime is None:
        raise ValueError("The Adaptive RoboVacs repair is no longer available")
    submit = runtime.application.async_execute

    if issue_id == f"storage_unsafe_{entry_id}":
        return UnsafeStoreRepairFlow(submit)
    robot_registry_id = str((data or {}).get("robot_registry_id", ""))
    if issue_id == retired_map_hold_issue_id(entry_id, robot_registry_id):
        return ErrorRecoveryRepairFlow(
            submit,
            AcknowledgeRetiredMapCommand(
                robot_registry_id, str((data or {}).get("held_at", ""))
            ),
        )
    if issue_id == robot_dispatch_fault_issue_id(entry_id, robot_registry_id):
        return RobotDispatchFaultRepairFlow(submit, robot_registry_id)
    if issue_id == notification_delivery_issue_id(entry_id):
        return NotificationDeliveryRepairFlow(submit)
    area_id = str((data or {}).get("area_id", ""))
    if issue_id == room_recovery_issue_id(entry_id, area_id):
        return ErrorRecoveryRepairFlow(
            submit,
            AcknowledgeRoomRecoveryCommand(
                area_id, str((data or {}).get("recovery_id", ""))
            ),
        )
    if issue_id == robot_error_recovery_issue_id(entry_id, robot_registry_id):
        return RobotDispatchFaultRepairFlow(submit, robot_registry_id)
    if issue_id == room_dispatch_fault_issue_id(entry_id, area_id):
        reason_code = str((data or {}).get("reason_code", ""))
        return RoomDispatchFaultRepairFlow(
            submit,
            area_id,
            allow_reset=not reason_code.startswith("area_mapping_"),
        )
    if issue_id == cleaning_program_issue_id(entry_id, area_id):
        return CleaningProgramCompatibilityRepairFlow(submit, area_id)
    if issue_id == two_pass_issue_id(entry_id, area_id):
        return TwoPassCompatibilityRepairFlow(submit, area_id)
    raise ValueError("The Adaptive RoboVacs repair is no longer available")
