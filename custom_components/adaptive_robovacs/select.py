"""Select entities for Adaptive RoboVacs compatibility profiles."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .entity import AdaptiveEntity, async_setup_dynamic_entities


class _TimeSelect(AdaptiveEntity, SelectEntity):
    _attr_options = tuple(f"{hour:02d}:{minute:02d}" for hour in range(24) for minute in range(0, 60, 15))

    def __init__(self, coordinator, key: str, name: str) -> None:
        super().__init__(coordinator, f"global_{key}", name, "global_control")
        self.key = key

    @property
    def current_option(self) -> str:
        return str(self.coordinator.data[self.key])

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_global(self.key, option)


class _RobotSelect(AdaptiveEntity, SelectEntity):
    def __init__(self, coordinator, robot_entity_id: str, key: str, options: tuple[str, ...], label: str) -> None:
        super().__init__(
            coordinator,
            f"robot_{robot_entity_id}_{key}",
            label,
            "robot_control",
            robot_entity_id=robot_entity_id,
            robot_name_suffix=label,
        )
        self.robot_entity_id = robot_entity_id
        self.key = key
        self._attr_options = options

    @property
    def current_option(self) -> str | None:
        setting = self.coordinator.robot_state(self.robot_entity_id)["settings"].get(self.key)
        return setting if setting in self.options else (self.options[0] if self.options else None)

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_robot_setting(self.robot_entity_id, self.key, option)


def _entities(coordinator) -> list[AdaptiveEntity]:
    entities: list[AdaptiveEntity] = [
        _TimeSelect(coordinator, "hall_start", "Bedroom-transit start"),
        _TimeSelect(coordinator, "hall_end", "Bedroom-transit end"),
        _TimeSelect(coordinator, "unresolved_start", "Desired cleaning start"),
        _TimeSelect(coordinator, "unresolved_end", "Desired cleaning end"),
    ]
    for robot in coordinator.discovery.robots.values():
        profile = robot.profile
        if profile.mode_select_entity_id and profile.mode_options:
            entities.append(_RobotSelect(coordinator, robot.entity_id, "mode", profile.mode_options, "mode"))
        if profile.mop_mode_select_entity_id and profile.mop_mode_options:
            entities.append(_RobotSelect(coordinator, robot.entity_id, "mop_mode", profile.mop_mode_options, "mop mode"))
        if profile.mop_intensity_select_entity_id and profile.mop_intensity_options:
            entities.append(_RobotSelect(coordinator, robot.entity_id, "mop_intensity", profile.mop_intensity_options, "mop intensity"))
    return entities


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    """Set up dynamically discovered profile selects."""

    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_setup_dynamic_entities(entry, async_add_entities, coordinator, lambda: _entities(coordinator))
