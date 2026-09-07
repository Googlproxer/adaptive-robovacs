"""Entry-owned cleanup for features retired in 1.14.0."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
_MAP_ENTITY_SUFFIXES = {
    "button": "_capture_map_snapshot",
    "sensor": "_map_recovery",
    "select": "_map_recovery_preview",
    "camera": "_map_recovery_preview",
}


@callback
def async_retire_map_entities(hass: HomeAssistant, entry_id: str) -> None:
    """Match historical unique IDs even after renames or robot removal."""

    registry = er.async_get(hass)
    prefix = f"{entry_id}_robot_"
    for entity in tuple(er.async_entries_for_config_entry(registry, entry_id)):
        suffix = _MAP_ENTITY_SUFFIXES.get(entity.domain)
        if (
            entity.platform == DOMAIN
            and suffix is not None
            and entity.unique_id.startswith(prefix)
            and entity.unique_id.endswith(suffix)
            and len(entity.unique_id) > len(prefix) + len(suffix)
        ):
            registry.async_remove(entity.entity_id)


async def async_remove_retired_map_archive(hass: HomeAssistant, entry_id: str) -> None:
    """Delete only the obsolete archive, including one that cannot be decoded."""

    store: Store[dict[str, object]] = Store(
        hass, 1, f"{DOMAIN}.map_recovery.{entry_id}"
    )
    try:
        await store.async_remove()
    except OSError:
        _LOGGER.warning(
            "Could not remove retired map archive for entry %s; "
            "cleanup will retry on next setup",
            entry_id,
        )
