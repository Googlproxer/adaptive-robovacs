"""Buttons for safe scheduler diagnostics."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .entity import AdaptiveEntity, async_setup_dynamic_entities


class _PreviewButton(AdaptiveEntity, ButtonEntity):
    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "preview_schedule", "Preview schedule", "scheduler_control")

    async def async_press(self) -> None:
        await self.coordinator.async_evaluate(dry_run=True, reason="dashboard_preview")


class _ConfirmHeldCleanCancelledButton(AdaptiveEntity, ButtonEntity):
    """Require an explicit acknowledgement before a held robot can schedule again."""

    _attr_icon = "mdi:cancel"

    def __init__(self, coordinator, robot_entity_id: str) -> None:
        super().__init__(
            coordinator,
            f"robot_{robot_entity_id}_confirm_held_clean_cancelled",
            "Confirm held clean cancelled",
            "robot_control",
            robot_entity_id=robot_entity_id,
            robot_name_suffix="Confirm held clean cancelled",
        )
        self.robot_entity_id = robot_entity_id

    async def async_press(self) -> None:
        await self.coordinator.async_confirm_held_clean_cancelled(self.robot_entity_id)


def _entities(coordinator) -> list[AdaptiveEntity]:
    return [
        _PreviewButton(coordinator),
        *[
            _ConfirmHeldCleanCancelledButton(coordinator, robot.entity_id)
            for robot in coordinator.discovery.robots.values()
        ],
    ]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    """Set up the preview and safe held-clean controls."""

    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_setup_dynamic_entities(entry, async_add_entities, coordinator, lambda: _entities(coordinator))
