"""Select entities for Adaptive RoboVacs compatibility profiles."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .entity import AdaptiveEntity, async_setup_dynamic_entities
from .models import is_native_mop_profile_value

TIME_OPTIONS = tuple(
    f"{hour:02d}:{minute:02d}"
    for hour in range(24)
    for minute in range(0, 60, 15)
)
USE_GLOBAL_OPTION = "Use global"
PASS_OPTIONS = ("Robot default", "1 pass", "2 passes")
PROGRAM_OPTIONS = ("Vacuum only", "Mop only", "Vacuum then mop", "Mop then vacuum")
PROGRAM_VALUES = {
    "Vacuum only": "vacuum_only",
    "Mop only": "mop_only",
    "Vacuum then mop": "vacuum_then_mop",
    "Mop then vacuum": "mop_then_vacuum",
}
PROGRAM_LABELS = {value: label for label, value in PROGRAM_VALUES.items()}
NOT_CONFIGURED_OPTION = "Not configured"


class _TimeSelect(AdaptiveEntity, SelectEntity):
    _attr_options = TIME_OPTIONS

    def __init__(self, coordinator, key: str, name: str) -> None:
        super().__init__(coordinator, f"global_{key}", name, "global_control")
        self.key = key

    @property
    def current_option(self) -> str:
        return str(self.coordinator.get_global_setting(self.key))

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_global(self.key, option)


class _RoomTimeSelect(AdaptiveEntity, SelectEntity):
    _attr_options = (USE_GLOBAL_OPTION, *TIME_OPTIONS)

    def __init__(self, coordinator, area_id: str, key: str, name: str) -> None:
        bound = "start" if key.endswith("start") else "end"
        super().__init__(
            coordinator,
            f"room_{area_id}_{key}",
            name,
            f"room_window_{bound}_control",
            area_id=area_id,
        )
        self.area_id = area_id
        self.key = key

    @property
    def current_option(self) -> str:
        configured = self.coordinator.get_room_setting(self.area_id, self.key)
        return str(configured) if configured is not None else USE_GLOBAL_OPTION

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_room_setting(
            self.area_id,
            self.key,
            None if option == USE_GLOBAL_OPTION else option,
        )


class _RoomPassSelect(AdaptiveEntity, SelectEntity):
    _attr_options = PASS_OPTIONS

    def __init__(self, coordinator, area_id: str, operation: str, name: str) -> None:
        key = "vacuum_pass_count" if operation == "vacuum" else "mop_pass_count"
        unique_key = "pass_count" if operation == "vacuum" else "mop_pass_count"
        super().__init__(
            coordinator,
            f"room_{area_id}_{unique_key}",
            name,
            "room_pass_count_control" if operation == "vacuum" else "room_mop_pass_count_control",
            area_id=area_id,
        )
        self.area_id = area_id
        self.key = key

    @property
    def current_option(self) -> str:
        value = self.coordinator.get_room_setting(self.area_id, self.key)
        return "Robot default" if value is None else f"{value} pass" + ("es" if value == 2 else "")

    async def async_select_option(self, option: str) -> None:
        value = {"Robot default": None, "1 pass": 1, "2 passes": 2}[option]
        await self.coordinator.async_set_room_setting(
            self.area_id, self.key, value
        )


class _RobotProgramSelect(AdaptiveEntity, SelectEntity):
    def __init__(self, coordinator, robot_entity_id: str) -> None:
        super().__init__(coordinator, f"robot_{coordinator.robot_unique_fragment(robot_entity_id)}_cleaning_program",
                         "cleaning program", "robot_control",
                         robot_entity_id=robot_entity_id,
                         robot_name_suffix="cleaning program")
        self.robot_entity_id = robot_entity_id

    @property
    def options(self) -> tuple[str, ...]:
        robot = self.coordinator.discovery.robots[self.robot_entity_id]
        return (
            PROGRAM_OPTIONS
            if "mop" in robot.adapter_capabilities.supported_operations
            else ("Vacuum only",)
        )

    @property
    def current_option(self) -> str:
        value = self.coordinator.robot_state(self.robot_entity_id)["settings"].get("cleaning_program")
        label = PROGRAM_LABELS.get(value, "Vacuum only")
        return label if label in self.options else self.options[0]

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_robot_setting(
            self.robot_entity_id, "cleaning_program", PROGRAM_VALUES[option]
        )


class _RoomProgramSelect(AdaptiveEntity, SelectEntity):
    def __init__(self, coordinator, area_id: str, name: str) -> None:
        super().__init__(coordinator, f"room_{area_id}_cleaning_program", name,
                         "room_cleaning_program_control", area_id=area_id)
        self.area_id = area_id

    @property
    def options(self) -> tuple[str, ...]:
        room = self.coordinator.discovery.rooms[self.area_id]
        supports_mopping = any(
            robot.floor_id == room.floor_id
            and "mop" in robot.adapter_capabilities.supported_operations
            for robot in self.coordinator.discovery.robots.values()
        )
        return (
            ("Robot default", *PROGRAM_OPTIONS)
            if supports_mopping
            else ("Robot default", "Vacuum only")
        )

    @property
    def current_option(self) -> str:
        value = self.coordinator.get_room_setting(self.area_id, "cleaning_program")
        label = PROGRAM_LABELS.get(value, "Robot default")
        return label if label in self.options else self.options[0]

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_room_setting(
            self.area_id, "cleaning_program",
            None if option == "Robot default" else PROGRAM_VALUES[option],
        )


class _RobotSelect(AdaptiveEntity, SelectEntity):
    def __init__(self, coordinator, robot_entity_id: str, key: str, options: tuple[str, ...], label: str) -> None:
        super().__init__(
            coordinator,
            f"robot_{coordinator.robot_unique_fragment(robot_entity_id)}_{key}",
            label,
            "robot_control",
            robot_entity_id=robot_entity_id,
            robot_name_suffix=label,
        )
        self.robot_entity_id = robot_entity_id
        self.key = key
        self._fallback_options = options

    @property
    def options(self) -> tuple[str, ...]:
        robot = self.coordinator.discovery.robots.get(self.robot_entity_id)
        if robot:
            options = {
                "fan_speed": robot.adapter_capabilities.fan_speed_options,
                "mode": robot.adapter_capabilities.mode_options,
                "mop_mode": robot.adapter_capabilities.mop_mode_options,
                "mop_intensity": robot.adapter_capabilities.mop_intensity_options,
                "cleaning_depth": robot.adapter_capabilities.cleaning_depth_options,
            }[self.key]
        else:
            options = self._fallback_options
        direct_mop_setting = bool(
            robot
            and robot.adapter_capabilities.native_mop_profile
            and self.key in {"mop_mode", "mop_intensity"}
        )
        if direct_mop_setting:
            options = tuple(
                option
                for option in options
                if is_native_mop_profile_value(self.key, option)
            )
        visible = ([NOT_CONFIGURED_OPTION] if not direct_mop_setting else []) + list(options)
        saved = self.coordinator.robot_state(self.robot_entity_id)["settings"].get(
            self.key
        )
        if (
            not direct_mop_setting
            and isinstance(saved, str)
            and saved
            and saved not in visible
        ):
            visible.append(saved)
        return tuple(visible)

    @property
    def current_option(self) -> str | None:
        setting = self.coordinator.robot_state(self.robot_entity_id)["settings"].get(self.key)
        return setting if setting in self.options else NOT_CONFIGURED_OPTION

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_robot_setting(
            self.robot_entity_id,
            self.key,
            None if option == NOT_CONFIGURED_OPTION else option,
        )


class _MapRecoveryPreviewSelect(AdaptiveEntity, SelectEntity):
    """Select an archived map image only; this never talks to the robot."""

    def __init__(self, coordinator, robot_entity_id: str) -> None:
        super().__init__(
            coordinator,
            f"robot_{coordinator.robot_unique_fragment(robot_entity_id)}_map_recovery_preview",
            "map recovery preview",
            "robot_map_recovery_preview_select",
            robot_entity_id=robot_entity_id,
            robot_name_suffix="map recovery preview",
        )
        self.robot_entity_id = robot_entity_id

    @property
    def options(self) -> tuple[str, ...]:
        return self.coordinator.map_recovery.preview_options(self.robot_entity_id)

    @property
    def current_option(self) -> str | None:
        return self.coordinator.map_recovery.selected_preview_option(self.robot_entity_id)

    async def async_select_option(self, option: str) -> None:
        self.coordinator.map_recovery.select_preview_option(self.robot_entity_id, option)


class _RoomProfileSelect(AdaptiveEntity, SelectEntity):
    """One room override backed by the union of same-floor robot options."""

    def __init__(self, coordinator, area_id: str, name: str, key: str, label: str) -> None:
        super().__init__(
            coordinator,
            f"room_{area_id}_{key}",
            f"{name} {label}",
            f"room_{key}_control",
            area_id=area_id,
        )
        self.area_id = area_id
        self.key = key

    def _floor_options(self) -> tuple[str, ...]:
        room = self.coordinator.discovery.rooms[self.area_id]
        values: list[str] = []
        for robot in self.coordinator.discovery.robots.values():
            if robot.floor_id != room.floor_id:
                continue
            options = {
                "fan_speed": robot.adapter_capabilities.fan_speed_options,
                "mode": robot.adapter_capabilities.mode_options,
                "mop_mode": robot.adapter_capabilities.mop_mode_options,
                "mop_intensity": robot.adapter_capabilities.mop_intensity_options,
                "cleaning_depth": robot.adapter_capabilities.cleaning_depth_options,
            }[self.key]
            for option in options:
                if option not in values:
                    values.append(option)
        return tuple(values)

    @property
    def options(self) -> tuple[str, ...]:
        visible = ["Robot default", *self._floor_options()]
        saved = self.coordinator.get_room_setting(self.area_id, self.key)
        if isinstance(saved, str) and saved and saved not in visible:
            visible.append(saved)
        return tuple(visible)

    @property
    def current_option(self) -> str:
        value = self.coordinator.get_room_setting(self.area_id, self.key)
        return value if isinstance(value, str) and value in self.options else "Robot default"

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_room_setting(
            self.area_id,
            self.key,
            None if option == "Robot default" else option,
        )


def _entities(coordinator) -> list[AdaptiveEntity]:
    entities: list[AdaptiveEntity] = [
        _TimeSelect(coordinator, "hall_start", "Bedroom-transit start"),
        _TimeSelect(coordinator, "hall_end", "Bedroom-transit end"),
        _TimeSelect(coordinator, "unresolved_start", "Desired cleaning start"),
        _TimeSelect(coordinator, "unresolved_end", "Desired cleaning end"),
    ]
    for robot in coordinator.discovery.robots.values():
        profile = robot.profile
        entities.append(
            _RobotProgramSelect(
                coordinator,
                robot.entity_id,
            )
        )
        if robot.adapter_capabilities.fan_speed_options:
            entities.append(
                _RobotSelect(
                    coordinator,
                    robot.entity_id,
                    "fan_speed",
                    robot.adapter_capabilities.fan_speed_options,
                    "fan speed",
                )
            )
        if profile.mode_select_entity_id and profile.mode_options:
            label = (
                "vacuum cleaning mode (mopping uses native Mop with suction off)"
                if robot.adapter_capabilities.native_mop_profile
                else "mode"
            )
            entities.append(_RobotSelect(coordinator, robot.entity_id, "mode", profile.mode_options, label))
        if profile.mop_mode_select_entity_id and profile.mop_mode_options:
            label = (
                "native mop route"
                if robot.adapter_capabilities.native_mop_profile
                else "mop mode"
            )
            entities.append(_RobotSelect(coordinator, robot.entity_id, "mop_mode", profile.mop_mode_options, label))
        if profile.mop_intensity_select_entity_id and profile.mop_intensity_options:
            label = (
                "native mop water intensity"
                if robot.adapter_capabilities.native_mop_profile
                else "mop intensity"
            )
            entities.append(_RobotSelect(coordinator, robot.entity_id, "mop_intensity", profile.mop_intensity_options, label))
        if robot.adapter_capabilities.cleaning_depth_options:
            entities.append(
                _RobotSelect(
                    coordinator,
                    robot.entity_id,
                    "cleaning_depth",
                    robot.adapter_capabilities.cleaning_depth_options,
                    "cleaning depth",
                )
            )
        if (
            coordinator.map_recovery.capability(robot.entity_id).available
            and coordinator.map_recovery.preview_options(robot.entity_id)
        ):
            entities.append(_MapRecoveryPreviewSelect(coordinator, robot.entity_id))
    for room in coordinator.discovery.rooms.values():
        supports_mopping = any(
            robot.floor_id == room.floor_id
            and "mop" in robot.adapter_capabilities.supported_operations
            for robot in coordinator.discovery.robots.values()
        )
        entities.extend(
            [
                _RoomTimeSelect(
                    coordinator,
                    room.area_id,
                    "desired_window_start",
                    f"{room.name} desired cleaning start",
                ),
                _RoomTimeSelect(
                    coordinator,
                    room.area_id,
                    "desired_window_end",
                    f"{room.name} desired cleaning end",
                ),
                _RoomPassSelect(
                    coordinator,
                    room.area_id,
                    "vacuum",
                    f"{room.name} vacuum passes",
                ),
                _RoomProgramSelect(
                    coordinator,
                    room.area_id,
                    f"{room.name} cleaning program",
                ),
            ]
        )
        if supports_mopping:
            entities.append(
                _RoomPassSelect(
                    coordinator, room.area_id, "mop", f"{room.name} mop passes"
                )
            )
        for key, label in (
            ("fan_speed", "fan speed"),
            ("mode", "mode"),
            ("mop_mode", "mop mode"),
            ("mop_intensity", "mop intensity"),
            ("cleaning_depth", "cleaning depth"),
        ):
            profile_select = _RoomProfileSelect(
                coordinator, room.area_id, room.name, key, label
            )
            if len(profile_select.options) > 1:
                entities.append(profile_select)
    return entities


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    """Set up dynamically discovered profile selects."""

    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_setup_dynamic_entities(entry, async_add_entities, coordinator, lambda: _entities(coordinator))
