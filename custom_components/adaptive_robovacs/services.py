"""Services exposed by Adaptive RoboVacs."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv

from .const import (
    DOMAIN,
    SERVICE_ACTIVATE_RETAINED_MAP,
    SERVICE_CAPTURE_MAP_SNAPSHOT,
    SERVICE_CLEAR_LEGACY_DEFERRALS,
    SERVICE_CONFIRM_MAP_SELECTION,
    SERVICE_EVALUATE,
    SERVICE_LIST_RETAINED_MAPS,
    SERVICE_LIST_LEGACY_DEFERRALS,
    SERVICE_MANUAL_CLEAN_ROOM,
    SERVICE_RECORD_MANUAL_CLEAN,
)


def _coordinator(hass: HomeAssistant, entry_id: str | None = None):
    entries = hass.data.get(DOMAIN, {})
    if not entries:
        raise vol.Invalid("Adaptive RoboVacs is not configured")
    if entry_id:
        if entry_id not in entries:
            raise vol.Invalid("The selected Adaptive RoboVacs entry is not loaded")
        return entries[entry_id]
    if len(entries) != 1:
        raise vol.Invalid("entry_id is required when multiple Adaptive RoboVacs entries are loaded")
    return next(iter(entries.values()))


async def async_register_services(hass: HomeAssistant) -> None:
    """Register services once, including before a config entry is loaded."""

    if hass.services.has_service(DOMAIN, SERVICE_EVALUATE):
        return

    async def evaluate(call: ServiceCall) -> dict[str, Any]:
        return await _coordinator(hass, call.data.get("entry_id")).async_evaluate(
            dry_run=bool(call.data.get("dry_run", False)), reason="service"
        )

    async def manual_clean(call: ServiceCall) -> dict[str, Any]:
        return await _coordinator(hass, call.data.get("entry_id")).async_record_manual_clean(
            call.data["robot_entity_id"],
            list(call.data["area_ids"]),
            list(call.data.get("operations", ["vacuum"])),
        )

    async def manual_clean_room(call: ServiceCall) -> dict[str, Any]:
        return await _coordinator(
            hass, call.data.get("entry_id")
        ).async_manual_clean_room(
            call.data["area_id"],
            call.data.get("mode", "configured"),
            context_id=call.context.id,
            user_id=call.context.user_id,
        )

    async def list_retained_maps(call: ServiceCall) -> dict[str, Any]:
        return await _coordinator(
            hass, call.data.get("entry_id")
        ).map_recovery.async_list_maps(call.data["robot_entity_id"])

    async def capture_map_snapshot(call: ServiceCall) -> dict[str, Any]:
        return await _coordinator(
            hass, call.data.get("entry_id")
        ).map_recovery.async_capture(call.data["robot_entity_id"])

    async def activate_retained_map(call: ServiceCall) -> dict[str, Any]:
        return await _coordinator(
            hass, call.data.get("entry_id")
        ).map_recovery.async_activate(
            call.data["robot_entity_id"], call.data["map_id"], confirm=call.data["confirm"]
        )

    async def confirm_map_selection(call: ServiceCall) -> dict[str, Any]:
        return await _coordinator(
            hass, call.data.get("entry_id")
        ).map_recovery.async_verify(call.data["robot_entity_id"], confirm=call.data["confirm"])

    async def list_legacy_deferrals(call: ServiceCall) -> dict[str, Any]:
        return {
            "legacy_deferrals": _coordinator(
                hass, call.data.get("entry_id")
            ).legacy_deferral_report()
        }

    async def clear_legacy_deferrals(call: ServiceCall) -> dict[str, Any]:
        return await _coordinator(
            hass, call.data.get("entry_id")
        ).async_clear_legacy_deferrals(list(call.data["area_ids"]))

    hass.services.async_register(
        DOMAIN,
        SERVICE_EVALUATE,
        evaluate,
        schema=vol.Schema(
            {
                vol.Optional("dry_run", default=False): cv.boolean,
                vol.Optional("entry_id"): str,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_RETAINED_MAPS,
        list_retained_maps,
        schema=vol.Schema(
            {
                vol.Required("robot_entity_id"): cv.entity_id,
                vol.Optional("entry_id"): str,
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CAPTURE_MAP_SNAPSHOT,
        capture_map_snapshot,
        schema=vol.Schema(
            {
                vol.Required("robot_entity_id"): cv.entity_id,
                vol.Optional("entry_id"): str,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ACTIVATE_RETAINED_MAP,
        activate_retained_map,
        schema=vol.Schema(
            {
                vol.Required("robot_entity_id"): cv.entity_id,
                vol.Required("map_id"): str,
                vol.Required("confirm"): vol.All(cv.boolean, vol.Equal(True)),
                vol.Optional("entry_id"): str,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CONFIRM_MAP_SELECTION,
        confirm_map_selection,
        schema=vol.Schema(
            {
                vol.Required("robot_entity_id"): cv.entity_id,
                vol.Required("confirm"): vol.All(cv.boolean, vol.Equal(True)),
                vol.Optional("entry_id"): str,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_MANUAL_CLEAN_ROOM,
        manual_clean_room,
        schema=vol.Schema(
            {
                vol.Required("area_id"): str,
                vol.Optional("mode", default="configured"): vol.In(
                    ["configured", "vacuum_only", "mop_only"]
                ),
                vol.Optional("entry_id"): str,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_LEGACY_DEFERRALS,
        list_legacy_deferrals,
        schema=vol.Schema({vol.Optional("entry_id"): str}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_LEGACY_DEFERRALS,
        clear_legacy_deferrals,
        schema=vol.Schema(
            {
                vol.Required("area_ids"): vol.All(cv.ensure_list, [str]),
                vol.Required("confirm"): vol.All(cv.boolean, vol.Equal(True)),
                vol.Optional("entry_id"): str,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RECORD_MANUAL_CLEAN,
        manual_clean,
        schema=vol.Schema(
            {
                vol.Required("robot_entity_id"): cv.entity_id,
                vol.Required("area_ids"): vol.All(cv.ensure_list, [str]),
                vol.Optional("operations", default=["vacuum"]): vol.All(
                    cv.ensure_list, [vol.In(["vacuum", "mop"])]
                ),
                vol.Optional("entry_id"): str,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
async def async_unregister_services(hass: HomeAssistant) -> None:
    """Keep global services available once registered during the HA process."""

    # Home Assistant requires actions to remain registered for validation even
    # when no config entry is loaded. They raise a helpful error until setup.
    return None
