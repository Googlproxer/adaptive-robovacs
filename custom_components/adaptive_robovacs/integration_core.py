"""Adaptive RoboVacs custom integration."""

from __future__ import annotations

from collections.abc import Mapping

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store

from .application import SchedulerApplication
from .const import (
    DOMAIN,
    MAP_RECOVERY_STORAGE_KEY,
    MAP_RECOVERY_STORE_VERSION,
    PLATFORMS,
    STORAGE_KEY,
    STORE_VERSION,
)
from .coordinator import AdaptiveRoboVacsCoordinator
from .repairs_manager import (
    cleaning_program_issue_id,
    notification_delivery_issue_id,
    scheduler_halted_issue_id,
    two_pass_issue_id,
)
from .runtime_data import AdaptiveRoboVacsConfigEntry, AdaptiveRoboVacsRuntimeData
from .services import async_register_services, async_unregister_services


async def async_setup_entry(
    hass: HomeAssistant, entry: AdaptiveRoboVacsConfigEntry
) -> bool:
    """Set up Adaptive RoboVacs from a config entry."""

    application = SchedulerApplication(hass, entry)
    await application.async_initialize()
    coordinator = AdaptiveRoboVacsCoordinator(application)
    entry.runtime_data = AdaptiveRoboVacsRuntimeData(
        coordinator=coordinator,
        application=application,
        lifecycle=application.lifecycle,
    )

    await async_register_services(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: AdaptiveRoboVacsConfigEntry
) -> bool:
    """Unload a config entry."""

    runtime = entry.runtime_data
    runtime.application.begin_shutdown()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        runtime.coordinator.close()
        await runtime.application.async_shutdown()
        if not any(
            loaded_entry.entry_id != entry.entry_id
            for loaded_entry in hass.config_entries.async_entries(DOMAIN)
        ):
            await async_unregister_services(hass)
    else:
        runtime.application.cancel_shutdown()
    return unload_ok


async def async_remove_entry(
    hass: HomeAssistant, entry: AdaptiveRoboVacsConfigEntry
) -> None:
    """Remove all durable data and Repairs owned by a deleted config entry."""

    store: Store[dict[str, object]] = Store(
        hass, STORE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}"
    )
    stored = await store.async_load()
    room_ids: set[str] = set()
    if isinstance(stored, dict):
        room_settings = stored.get("room_settings")
        if isinstance(room_settings, dict):
            room_ids.update(str(area_id) for area_id in room_settings)
        legacy_settings = stored.get("settings")
        if isinstance(legacy_settings, dict) and isinstance(
            legacy_settings.get("rooms"), dict
        ):
            room_ids.update(str(area_id) for area_id in legacy_settings["rooms"])

    issue_ids = {
        scheduler_halted_issue_id(entry.entry_id),
        notification_delivery_issue_id(entry.entry_id),
    }
    for area_id in room_ids:
        issue_ids.add(two_pass_issue_id(entry.entry_id, area_id))
        issue_ids.add(cleaning_program_issue_id(entry.entry_id, area_id))

    registry = ir.async_get(hass)
    for (domain, issue_id), issue in tuple(registry.issues.items()):
        data = issue.data if isinstance(issue.data, Mapping) else {}
        if domain == DOMAIN and data.get("entry_id") == entry.entry_id:
            issue_ids.add(issue_id)
    for issue_id in issue_ids:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
    await store.async_remove()
    map_store: Store[dict[str, object]] = Store(
        hass, MAP_RECOVERY_STORE_VERSION, f"{MAP_RECOVERY_STORAGE_KEY}.{entry.entry_id}"
    )
    await map_store.async_remove()
