"""Privacy-safe diagnostics for Adaptive RoboVacs."""

from __future__ import annotations

from collections import Counter
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .runtime_data import AdaptiveRoboVacsConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: AdaptiveRoboVacsConfigEntry,
) -> dict[str, Any]:
    """Return bounded performance counters without household identifiers."""

    application = entry.runtime_data.application
    registry = er.async_get(hass)
    platform_counts = Counter(
        entity.domain
        for entity in registry.entities.values()
        if entity.config_entry_id == entry.entry_id
    )
    return {
        "runtime": application.metrics.as_dict(
            {
                "rooms": len(application.discovery.rooms),
                "robots": len(application.discovery.robots),
                "watched_entities": len(application._watch_entity_ids),
                "capability_entities": len(application._watch_capability_entity_ids),
                "registered_entities": sum(platform_counts.values()),
            }
        ),
        "entity_platform_counts": dict(sorted(platform_counts.items())),
        "storage_safe_mode": application._storage_safe_mode,
    }
