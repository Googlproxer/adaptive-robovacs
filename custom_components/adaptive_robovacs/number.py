"""Number entities for Adaptive RoboVacs."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .commands import SetGlobalCommand, SetRobotSettingCommand, SetRoomSettingCommand
from .coordinator import AdaptiveRoboVacsCoordinator
from .entity import (
    AdaptiveEntity,
    async_setup_dynamic_entities,
    robot_unique_fragment,
)
from .runtime_data import AdaptiveRoboVacsConfigEntry

PARALLEL_UPDATES = 0


class _AdaptiveNumber(AdaptiveEntity, NumberEntity):
    _attr_mode = NumberMode.BOX


class _GlobalNumber(_AdaptiveNumber):
    _attr_native_min_value = 50
    _attr_native_max_value = 95
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "%"

    def __init__(self, coordinator: AdaptiveRoboVacsCoordinator) -> None:
        super().__init__(
            coordinator,
            "global_forecast_confidence",
            "Forecast confidence",
            "global_control",
        )

    @property
    def native_value(self) -> float:
        return self.coordinator.data.scheduler.forecast_confidence

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_execute(
            SetGlobalCommand("forecast_confidence", int(value))
        )


class _RobotNumber(_AdaptiveNumber):
    _attr_native_min_value = 20
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "%"

    def __init__(
        self, coordinator: AdaptiveRoboVacsCoordinator, robot_entity_id: str
    ) -> None:
        super().__init__(
            coordinator,
            "robot_"
            f"{robot_unique_fragment(coordinator, robot_entity_id)}_minimum_battery",
            "minimum battery",
            "robot_control",
            robot_entity_id=robot_entity_id,
            robot_name_suffix="minimum battery",
        )
        self.robot_entity_id = robot_entity_id

    @property
    def native_value(self) -> float:
        return self.robot_view(self.robot_entity_id).settings.minimum_battery

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_execute(
            SetRobotSettingCommand(
                self.robot_entity_id,
                "minimum_battery",
                value,
            )
        )


class _RoomNumber(_AdaptiveNumber):
    def __init__(
        self,
        coordinator: AdaptiveRoboVacsCoordinator,
        area_id: str,
        key: str,
        name: str,
    ) -> None:
        super().__init__(
            coordinator, f"room_{area_id}_{key}", name, "room_control", area_id=area_id
        )
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
        room = self.room_view(self.area_id)
        return (
            room.cleaning_interval
            if self.key == "vacuum_interval"
            else room.expected_minutes
        )

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_execute(
            SetRoomSettingCommand(self.area_id, self.key, value)
        )


def _entities(coordinator: AdaptiveRoboVacsCoordinator) -> list[AdaptiveEntity]:
    entities: list[AdaptiveEntity] = [_GlobalNumber(coordinator)]
    for robot in coordinator.data.robots:
        entities.append(_RobotNumber(coordinator, robot.entity_id))
    for room in coordinator.data.rooms:
        entities.extend(
            [
                # Keep the established unique ID while the control becomes the
                # room's single cleaning cadence in schema 6.
                _RoomNumber(
                    coordinator,
                    room.area_id,
                    "vacuum_interval",
                    f"{room.name} cleaning cadence",
                ),
                _RoomNumber(
                    coordinator,
                    room.area_id,
                    "expected_minutes",
                    f"{room.name} expected duration",
                ),
            ]
        )
    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AdaptiveRoboVacsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up number controls."""

    coordinator = entry.runtime_data.coordinator
    async_setup_dynamic_entities(
        entry, async_add_entities, coordinator, lambda: _entities(coordinator)
    )
