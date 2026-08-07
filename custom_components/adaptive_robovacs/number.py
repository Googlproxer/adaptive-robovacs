"""Number entities for Adaptive RoboVacs."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .entity import AdaptiveEntity, async_setup_dynamic_entities


class _AdaptiveNumber(AdaptiveEntity, NumberEntity):
    _attr_mode = NumberMode.BOX


class _GlobalNumber(_AdaptiveNumber):
    _attr_native_min_value = 50
    _attr_native_max_value = 95
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "%"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "global_forecast_confidence", "Forecast confidence", "global_control")

    @property
    def native_value(self) -> float:
        return float(self.coordinator.data["forecast_confidence"])

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_global("forecast_confidence", int(value))


class _RobotNumber(_AdaptiveNumber):
    _attr_native_min_value = 20
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "%"

    def __init__(self, coordinator, robot_entity_id: str, name: str) -> None:
        super().__init__(
            coordinator,
            f"robot_{robot_entity_id}_minimum_battery",
            name,
            "robot_control",
            robot_entity_id=robot_entity_id,
        )
        self.robot_entity_id = robot_entity_id

    @property
    def native_value(self) -> float:
        return float(self.coordinator.robot_state(self.robot_entity_id)["settings"]["minimum_battery"])

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_robot_setting(self.robot_entity_id, "minimum_battery", value)


class _RoomNumber(_AdaptiveNumber):
    def __init__(self, coordinator, area_id: str, key: str, name: str) -> None:
        super().__init__(coordinator, f"room_{area_id}_{key}", name, "room_control", area_id=area_id)
        self.area_id = area_id
        self.key = key
        if key == "expected_minutes":
            self._attr_native_min_value = 5
            self._attr_native_max_value = 180
            self._attr_native_step = 5
            self._attr_native_unit_of_measurement = "min"
        else:
            self._attr_native_min_value = 12
            self._attr_native_max_value = 336
            self._attr_native_step = 1
            self._attr_native_unit_of_measurement = "h"

    @property
    def native_value(self) -> float:
        return float(self.coordinator.room_state(self.area_id)[self.key])

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_room_setting(self.area_id, self.key, value)


def _entities(coordinator) -> list[AdaptiveEntity]:
    entities: list[AdaptiveEntity] = [_GlobalNumber(coordinator)]
    for robot in coordinator.discovery.robots.values():
        entities.append(_RobotNumber(coordinator, robot.entity_id, f"{robot.name} minimum battery"))
    for room in coordinator.discovery.rooms.values():
        entities.extend(
            [
                _RoomNumber(coordinator, room.area_id, "vacuum_interval", f"{room.name} vacuum cadence"),
                _RoomNumber(coordinator, room.area_id, "expected_minutes", f"{room.name} expected duration"),
            ]
        )
        if any(
            robot.floor_id == room.floor_id and robot.profile.supports_mopping
            for robot in coordinator.discovery.robots.values()
        ):
            entities.append(_RoomNumber(coordinator, room.area_id, "mop_interval", f"{room.name} mop cadence"))
    return entities


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    """Set up number controls."""

    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_setup_dynamic_entities(entry, async_add_entities, coordinator, lambda: _entities(coordinator))
