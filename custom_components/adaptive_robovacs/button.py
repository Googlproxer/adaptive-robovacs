"""Buttons for safe scheduler diagnostics."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .entity import AdaptiveEntity


class _PreviewButton(AdaptiveEntity, ButtonEntity):
    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "preview_schedule", "Preview schedule", "scheduler_control")

    async def async_press(self) -> None:
        await self.coordinator.async_evaluate(dry_run=True, reason="dashboard_preview")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    """Set up the non-dispatching schedule preview button."""

    async_add_entities([_PreviewButton(hass.data[DOMAIN][entry.entry_id])])
