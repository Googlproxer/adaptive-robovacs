"""Explicit deterministic vacuum adapter registry."""

from __future__ import annotations

import logging
from typing import Any

from ..models import AdapterCapabilities
from .base import AdapterMatchContext, VacuumAdapter
from .generic import GenericVacuumAdapter
from .roborock import RoborockVacuumAdapter

_LOGGER = logging.getLogger(__name__)

_GENERIC = GenericVacuumAdapter()
_REGISTERED: tuple[VacuumAdapter, ...] = (RoborockVacuumAdapter(_GENERIC),)
_BY_ID = {adapter.adapter_id: adapter for adapter in (*_REGISTERED, _GENERIC)}


async def async_resolve_adapter(
    hass: Any, context: AdapterMatchContext
) -> tuple[VacuumAdapter, AdapterCapabilities, str | None]:
    """Resolve one vendor adapter or the generic fallback."""

    matches = sorted(
        (adapter for adapter in _REGISTERED if adapter.matches(context)),
        key=lambda adapter: (-adapter.priority, adapter.adapter_id),
    )
    if len(matches) > 1 and matches[0].priority == matches[1].priority:
        _LOGGER.error(
            "Adaptive RoboVacs adapter match is ambiguous: platform=%s adapters=%s",
            context.platform,
            [adapter.adapter_id for adapter in matches],
        )
        capabilities = await _GENERIC.async_capabilities(hass, context)
        return _GENERIC, capabilities, "adapter_registration_ambiguous"
    adapter = matches[0] if matches else _GENERIC
    try:
        capabilities = await adapter.async_capabilities(hass, context)
    except Exception:
        _LOGGER.exception(
            "Adaptive RoboVacs adapter capability probe failed: platform=%s adapter=%s",
            context.platform,
            adapter.adapter_id,
        )
        capabilities = await _GENERIC.async_capabilities(hass, context)
        return _GENERIC, capabilities, "adapter_probe_failed"
    return adapter, capabilities, None


def adapter_for_id(adapter_id: str) -> VacuumAdapter:
    """Return a registered adapter, safely falling back after upgrades."""

    return _BY_ID.get(adapter_id, _GENERIC)
