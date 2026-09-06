"""Switch entities for Adaptive RoboVacs."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
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


class _AdaptiveSwitch(AdaptiveEntity, SwitchEntity):
    """Base persistent scheduler switch."""

    setting_key: str

    @property
    def is_on(self) -> bool:
        raise NotImplementedError


class _GlobalSwitch(_AdaptiveSwitch):
    def __init__(
        self, coordinator: AdaptiveRoboVacsCoordinator, key: str, name: str
    ) -> None:
        super().__init__(coordinator, f"global_{key}", name, "global_control")
        self.setting_key = key

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data.scheduler.global_setting(self.setting_key))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_execute(SetGlobalCommand(self.setting_key, True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_execute(SetGlobalCommand(self.setting_key, False))


class _RobotSwitch(_AdaptiveSwitch):
    def __init__(
        self,
        coordinator: AdaptiveRoboVacsCoordinator,
        robot_entity_id: str,
        key: str,
        label: str,
    ) -> None:
        super().__init__(
            coordinator,
            f"robot_{robot_unique_fragment(coordinator, robot_entity_id)}_{key}",
            label,
            "robot_control",
            robot_entity_id=robot_entity_id,
            robot_name_suffix=label,
        )
        self.robot_entity_id = robot_entity_id
        self.setting_key = key

    @property
    def is_on(self) -> bool:
        settings = self.robot_view(self.robot_entity_id).settings
        if self.setting_key == "enabled":
            return settings.enabled
        if self.setting_key == "double_pass":
            return settings.double_pass
        if self.setting_key == "mop_double_pass":
            return settings.mop_double_pass
        return False

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_execute(
            SetRobotSettingCommand(self.robot_entity_id, self.setting_key, True)
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_execute(
            SetRobotSettingCommand(self.robot_entity_id, self.setting_key, False)
        )


class _RoomSwitch(_AdaptiveSwitch):
    def __init__(
        self,
        coordinator: AdaptiveRoboVacsCoordinator,
        area_id: str,
        key: str,
        name: str,
    ) -> None:
        role = {
            "enabled": "room_enabled_control",
            "ignore_desired_window": "room_ignore_desired_window_control",
        }.get(key, "room_control")
        super().__init__(
            coordinator, f"room_{area_id}_{key}", name, role, area_id=area_id
        )
        self.area_id = area_id
        self.setting_key = key

    @property
    def is_on(self) -> bool:
        room = self.room_view(self.area_id)
        return (
            room.enabled
            if self.setting_key == "enabled"
            else room.ignore_desired_window
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_execute(
            SetRoomSettingCommand(self.area_id, self.setting_key, True)
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_execute(
            SetRoomSettingCommand(self.area_id, self.setting_key, False)
        )


def _entities(coordinator: AdaptiveRoboVacsCoordinator) -> list[AdaptiveEntity]:
    entities: list[AdaptiveEntity] = [
        _GlobalSwitch(coordinator, "party_mode", "Party mode"),
        _GlobalSwitch(coordinator, "observe_only", "Observe-only mode"),
    ]
    for robot in coordinator.data.robots:
        entities.append(
            _RobotSwitch(coordinator, robot.entity_id, "enabled", "enabled")
        )
        if 2 in robot.adapter_capabilities.vacuum_pass_counts:
            entities.append(
                _RobotSwitch(
                    coordinator, robot.entity_id, "double_pass", "double vacuum pass"
                )
            )
        if 2 in robot.adapter_capabilities.mop_pass_counts:
            entities.append(
                _RobotSwitch(
                    coordinator, robot.entity_id, "mop_double_pass", "double mop pass"
                )
            )
    for room in coordinator.data.rooms:
        entities.extend(
            [
                _RoomSwitch(
                    coordinator, room.area_id, "enabled", f"{room.name} enabled"
                ),
                _RoomSwitch(
                    coordinator,
                    room.area_id,
                    "ignore_desired_window",
                    f"{room.name} ignore desired cleaning window",
                ),
            ]
        )
    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AdaptiveRoboVacsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up registry-driven switches."""

    coordinator = entry.runtime_data.coordinator
    async_setup_dynamic_entities(
        entry, async_add_entities, coordinator, lambda: _entities(coordinator)
    )
