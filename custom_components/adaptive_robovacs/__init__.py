"""Adaptive RoboVacs custom integration entry point."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .integration_core import async_setup_entry, async_unload_entry
from .services import async_register_services

_STATIC_REGISTERED = f"{DOMAIN}_static_registered"


async def async_setup(hass: HomeAssistant, _config: ConfigType) -> bool:
    """Register integration-wide services and the dynamic dashboard resource."""

    await async_register_services(hass)
    if not hass.data.get(_STATIC_REGISTERED):
        static_path = Path(__file__).parent / "frontend"
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    f"/api/{DOMAIN}/frontend",
                    str(static_path),
                    cache_headers=False,
                )
            ]
        )
        hass.data[_STATIC_REGISTERED] = True
    return True
