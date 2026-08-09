"""Services exposed by Adaptive RoboVacs."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, SERVICE_EVALUATE, SERVICE_RECORD_MANUAL_CLEAN


def _coordinator(hass: HomeAssistant):
    entries = hass.data.get(DOMAIN, {})
    if not entries:
        raise vol.Invalid("Adaptive RoboVacs is not configured")
    return next(iter(entries.values()))


async def async_register_services(hass: HomeAssistant) -> None:
    """Register services once, including before a config entry is loaded."""

    if hass.services.has_service(DOMAIN, SERVICE_EVALUATE):
        return

    async def evaluate(call: ServiceCall) -> dict[str, Any]:
        return await _coordinator(hass).async_evaluate(
            dry_run=bool(call.data.get("dry_run", False)), reason="service"
        )

    async def manual_clean(call: ServiceCall) -> dict[str, Any]:
        return await _coordinator(hass).async_record_manual_clean(
            call.data["robot_entity_id"],
            list(call.data["area_ids"]),
            list(call.data.get("operations", ["vacuum"])),
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_EVALUATE,
        evaluate,
        schema=vol.Schema({vol.Optional("dry_run", default=False): cv.boolean}),
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
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
async def async_unregister_services(hass: HomeAssistant) -> None:
    """Keep global services available once registered during the HA process."""

    # Home Assistant requires actions to remain registered for validation even
    # when no config entry is loaded. They raise a helpful error until setup.
    return None
