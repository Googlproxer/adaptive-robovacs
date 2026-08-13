"""Buttons for safe scheduler diagnostics."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, SERVICE_MANUAL_CLEAN_ROOM
from .entity import AdaptiveEntity, async_setup_dynamic_entities


class _PreviewButton(AdaptiveEntity, ButtonEntity):
    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "preview_schedule", "Preview schedule", "scheduler_control")

    async def async_press(self) -> None:
        await self.coordinator.async_evaluate(dry_run=True, reason="dashboard_preview")


class _ResumeButton(AdaptiveEntity, ButtonEntity):
    def __init__(self, coordinator) -> None:
        super().__init__(
            coordinator,
            "recheck_and_resume",
            "Recheck and resume",
            "fault_resume_control",
        )

    async def async_press(self) -> None:
        await self.coordinator.async_recheck_and_resume()


class _RoomManualCleanButton(AdaptiveEntity, ButtonEntity):
    """One non-queueing room action with its mode fixed by entity identity."""

    def __init__(self, coordinator, area_id: str, name: str, mode: str, label: str) -> None:
        role = {
            "configured": "room_manual_clean_control",
            "vacuum_only": "room_manual_vacuum_control",
            "mop_only": "room_manual_mop_control",
        }[mode]
        super().__init__(
            coordinator,
            f"room_{area_id}_manual_{mode}",
            f"{name} {label}",
            role,
            area_id=area_id,
        )
        self.area_id = area_id
        self.mode = mode

    async def async_press(self) -> None:
        context = getattr(self, "_context", None)
        await self.coordinator.hass.services.async_call(
            DOMAIN,
            SERVICE_MANUAL_CLEAN_ROOM,
            {
                "entry_id": self.coordinator.entry.entry_id,
                "area_id": self.area_id,
                "mode": self.mode,
            },
            blocking=True,
            context=context,
        )


def _entities(coordinator) -> list[AdaptiveEntity]:
    entities: list[AdaptiveEntity] = [_ResumeButton(coordinator), _PreviewButton(coordinator)]
    for room in coordinator.discovery.rooms.values():
        entities.extend(
            [
                _RoomManualCleanButton(
                    coordinator, room.area_id, room.name, "configured", "manual clean"
                ),
                _RoomManualCleanButton(
                    coordinator, room.area_id, room.name, "vacuum_only", "manual vacuum only"
                ),
                _RoomManualCleanButton(
                    coordinator, room.area_id, room.name, "mop_only", "manual mop only"
                ),
            ]
        )
    return entities


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    """Set up the scheduler preview control."""

    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_setup_dynamic_entities(
        entry, async_add_entities, coordinator, lambda: _entities(coordinator)
    )
