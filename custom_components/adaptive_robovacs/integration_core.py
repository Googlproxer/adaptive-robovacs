"""Adaptive RoboVacs custom integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS
from .coordinator import AdaptiveRoboVacCoordinator
from .services import async_register_services, async_unregister_services
from .repairs_manager import scheduler_halted_issue_id, two_pass_issue_id
from homeassistant.helpers import issue_registry as ir

type AdaptiveRoboVacsConfigEntry = ConfigEntry[AdaptiveRoboVacCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: AdaptiveRoboVacsConfigEntry) -> bool:
    """Set up Adaptive RoboVacs from a config entry."""

    coordinator = AdaptiveRoboVacCoordinator(hass, entry)
    await coordinator.async_initialize()
    entry.runtime_data = coordinator
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await async_register_services(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AdaptiveRoboVacsConfigEntry) -> bool:
    """Unload a config entry."""

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = entry.runtime_data
        await coordinator.async_shutdown()
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if not hass.data.get(DOMAIN):
            await async_unregister_services(hass)
    return unload_ok


async def async_remove_entry(
    hass: HomeAssistant, entry: AdaptiveRoboVacsConfigEntry
) -> None:
    """Remove Repairs issues owned solely by a deleted config entry."""

    ir.async_delete_issue(
        hass, DOMAIN, scheduler_halted_issue_id(entry.entry_id)
    )
    coordinator = entry.runtime_data
    for area_id in coordinator.data.get("settings", {}).get("rooms", {}):
        ir.async_delete_issue(
            hass, DOMAIN, two_pass_issue_id(entry.entry_id, area_id)
        )
