"""Small isolated Core 2026.11+ runtime shared by tests and benchmarks.

Registries are real Core registries loaded empty/read-only in a temporary
configuration directory. Integration Stores remain real filesystem Stores.
No network setup, device services or external pytest fixture plugin is needed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from types import MappingProxyType
from typing import Any

from homeassistant import config_entries, loader
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import condition, entity, translation, trigger
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import label_registry as lr
from homeassistant.util import dt as dt_util
from homeassistant.util.async_ import _SHUTDOWN_RUN_CALLBACK_THREADSAFE


class MockConfigEntry(config_entries.ConfigEntry):
    """A real Core config entry, inserted without starting an integration."""

    def __init__(
        self,
        *,
        domain: str,
        entry_id: str | None = None,
        data: Mapping[str, Any] | None = None,
        options: Mapping[str, Any] | None = None,
        state: ConfigEntryState = ConfigEntryState.NOT_LOADED,
    ) -> None:
        super().__init__(
            domain=domain,
            entry_id=entry_id,
            data=data or {},
            options=options or {},
            state=state,
            version=1,
            minor_version=1,
            source=config_entries.SOURCE_USER,
            title="Synthetic integration",
            unique_id=None,
            discovery_keys=MappingProxyType({}),
            subentries_data=(),
        )

    def add_to_hass(self, hass: HomeAssistant) -> None:
        hass.config_entries._entries[self.entry_id] = self


@asynccontextmanager
async def async_test_home_assistant(*, config_dir: str) -> AsyncIterator[HomeAssistant]:
    """Initialize only the Core services exercised by this repository."""

    prior_timezone = dt_util.get_default_time_zone()
    hass = HomeAssistant(config_dir)
    try:
        await hass.config.async_set_time_zone("UTC")
        hass.config.skip_pip = True
        hass.config_entries = config_entries.ConfigEntries(hass, {"homeassistant": {}})
        hass.config_entries._initialized.set()
        hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, hass.config_entries._async_shutdown
        )
        entity.async_setup(hass)
        loader.async_setup(hass)
        hass.data[loader.DATA_CUSTOM_COMPONENTS] = {}
        await condition.async_setup(hass)
        await trigger.async_setup(hass)
        translation.async_setup(hass)
        dr.async_setup(hass)
        for registry in (ar, cr, dr, er, fr, ir, lr):
            await registry.async_load(hass, load_empty=True)
        hass.set_state(CoreState.running)
        yield hass
    finally:
        try:
            if hass.state is not CoreState.not_running:
                await hass.async_stop(force=True)
        finally:
            dt_util.set_default_time_zone(prior_timezone)
            # Core shutdown marks the shared loop as unavailable to threads.
            # A later benchmark scenario owns a fresh Core on this same loop.
            with suppress(AttributeError):
                delattr(hass.loop, _SHUTDOWN_RUN_CALLBACK_THREADSAFE)
