"""Repair flows for Adaptive RoboVacs scheduler failures."""

from __future__ import annotations

import voluptuous as vol

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .repairs_manager import scheduler_halted_issue_id
from .repairs_manager import two_pass_issue_id
from .repairs_manager import notification_delivery_issue_id
from .repairs_manager import cleaning_program_issue_id
from .repairs_manager import async_set_notification_delivery_issue


def _description_placeholders(flow: RepairsFlow) -> dict[str, str] | None:
    """Return the issue placeholders used by this Repair flow."""

    issue = ir.async_get(flow.hass).async_get_issue(flow.handler, flow.issue_id)
    return issue.translation_placeholders if issue else None


class SchedulerHaltedRepairFlow(RepairsFlow):
    """Recheck without dispatching, then explicitly resume scheduling."""

    def __init__(self, coordinator) -> None:
        self._coordinator = coordinator

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
            result = await self._coordinator.async_recheck_and_resume()
            if result.cleared:
                return self.async_create_entry(title="", data={})
            errors["base"] = result.reason
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders=_description_placeholders(self),
        )


class TwoPassCompatibilityRepairFlow(RepairsFlow):
    """Recheck whether a room again has a compatible two-pass vacuum."""

    def __init__(self, coordinator, area_id: str) -> None:
        self._coordinator = coordinator
        self._area_id = area_id

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
            if await self._coordinator.async_recheck_room_compatibility(
                self._area_id
            ):
                return self.async_create_entry(title="", data={})
            errors["base"] = "recheck_failed"
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders=_description_placeholders(self),
        )


class NotificationDeliveryRepairFlow(RepairsFlow):
    """Recheck whether at least one Companion notification target exists."""

    def __init__(self, coordinator) -> None:
        self._coordinator = coordinator

    async def async_step_init(self, user_input=None) -> data_entry_flow.FlowResult:
        # Do not let opening this issue recheck and resolve it automatically.
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input=None) -> data_entry_flow.FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if self._coordinator.has_notification_targets():
                async_set_notification_delivery_issue(self._coordinator, False)
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

    async def async_step_confirm(self, user_input=None) -> data_entry_flow.FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if await self._coordinator.async_recheck_cleaning_program_compatibility(
                self._area_id
            ):
                return self.async_create_entry(title="", data={})
            errors["base"] = "recheck_failed"
        return self.async_show_form(
            step_id="confirm", data_schema=vol.Schema({}), errors=errors,
            description_placeholders=_description_placeholders(self),
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the matching repair flow."""

    entry_id = str((data or {}).get("entry_id", ""))
    coordinator = hass.data.get(DOMAIN, {}).get(entry_id)
    if coordinator is None:
        raise ValueError("The Adaptive RoboVacs repair is no longer available")
    if issue_id == scheduler_halted_issue_id(entry_id):
        return SchedulerHaltedRepairFlow(coordinator)
    if issue_id == notification_delivery_issue_id(entry_id):
        return NotificationDeliveryRepairFlow(coordinator)
    area_id = str((data or {}).get("area_id", ""))
    if issue_id == cleaning_program_issue_id(entry_id, area_id):
        return CleaningProgramCompatibilityRepairFlow(coordinator, area_id)
    if issue_id == two_pass_issue_id(entry_id, area_id):
        return TwoPassCompatibilityRepairFlow(coordinator, area_id)
    raise ValueError("The Adaptive RoboVacs repair is no longer available")
