"""Switch entities for Adaptive RoboVacs."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .entity import AdaptiveEntity, async_setup_dynamic_entities


class _AdaptiveSwitch(AdaptiveEntity, SwitchEntity):
    """Base persistent scheduler switch."""

    setting_key: str

    @property
    def is_on(self) -> bool:
        raise NotImplementedError


class _GlobalSwitch(_AdaptiveSwitch):
    def __init__(self, coordinator, key: str, name: str) -> None:
        super().__init__(coordinator, f"global_{key}", name, "global_control")
        self.setting_key = key

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.get_global_setting(self.setting_key))

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_global(self.setting_key, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_global(self.setting_key, False)


class _RobotSwitch(_AdaptiveSwitch):
    def __init__(self, coordinator, robot_entity_id: str, key: str, label: str) -> None:
        super().__init__(
            coordinator,
            f"robot_{coordinator.robot_unique_fragment(robot_entity_id)}_{key}",
            label,
            "robot_control",
            robot_entity_id=robot_entity_id,
            robot_name_suffix=label,
        )
        self.robot_entity_id = robot_entity_id
        self.setting_key = key

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.robot_state(self.robot_entity_id)["settings"].get(self.setting_key, False))

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_robot_setting(self.robot_entity_id, self.setting_key, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_robot_setting(self.robot_entity_id, self.setting_key, False)


class _RoomSwitch(_AdaptiveSwitch):
    def __init__(self, coordinator, area_id: str, key: str, name: str) -> None:
        super().__init__(coordinator, f"room_{area_id}_{key}", name, "room_control", area_id=area_id)
        self.area_id = area_id
        self.setting_key = key

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.room_state(self.area_id)[self.setting_key])

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_room_setting(self.area_id, self.setting_key, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_room_setting(self.area_id, self.setting_key, False)


def _entities(coordinator) -> list[AdaptiveEntity]:
    entities: list[AdaptiveEntity] = [
        _GlobalSwitch(coordinator, "party_mode", "Party mode"),
        _GlobalSwitch(coordinator, "observe_only", "Observe-only mode"),
    ]
    for robot in coordinator.discovery.robots.values():
        entities.append(_RobotSwitch(coordinator, robot.entity_id, "enabled", "enabled"))
        if 2 in robot.adapter_capabilities.vacuum_pass_counts:
            entities.append(
                _RobotSwitch(coordinator, robot.entity_id, "double_pass", "double vacuum pass")
            )
        if 2 in robot.adapter_capabilities.mop_pass_counts:
            entities.append(
                _RobotSwitch(coordinator, robot.entity_id, "mop_double_pass", "double mop pass")
            )
    for room in coordinator.discovery.rooms.values():
        entities.extend(
            [
                _RoomSwitch(coordinator, room.area_id, "enabled", f"{room.name} enabled"),
                _RoomSwitch(coordinator, room.area_id, "carpet", f"{room.name} carpet (no mopping)"),
                _RoomSwitch(
                    coordinator,
                    room.area_id,
                    "ignore_desired_window",
                    f"{room.name} ignore desired cleaning window",
                ),
            ]
        )
    return entities


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    """Set up registry-driven switches."""

    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_setup_dynamic_entities(entry, async_add_entities, coordinator, lambda: _entities(coordinator))
