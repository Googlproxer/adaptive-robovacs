"""Select entities for Adaptive RoboVacs compatibility profiles."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .commands import (
    SelectMapPreviewCommand,
    SetGlobalCommand,
    SetRobotSettingCommand,
    SetRoomCleaningPeriodCommand,
    SetRoomCleaningProfileCommand,
    SetRoomSettingCommand,
)
from .coordinator import AdaptiveRoboVacsCoordinator
from .entity import (
    AdaptiveEntity,
    async_setup_dynamic_entities,
    robot_unique_fragment,
)
from .models import (
    ROOM_CLEANING_PERIOD_OPTIONS,
    ROOM_CLEANING_PROFILE_OPTIONS,
    is_native_mop_profile_value,
)
from .runtime_data import AdaptiveRoboVacsConfigEntry

PARALLEL_UPDATES = 0

TIME_OPTIONS = [
    f"{hour:02d}:{minute:02d}" for hour in range(24) for minute in range(0, 60, 15)
]
USE_GLOBAL_OPTION = "Use global"
PASS_OPTIONS = ["Robot default", "1 pass", "2 passes"]
PROGRAM_OPTIONS = ["Vacuum only", "Mop only", "Vacuum then mop", "Mop then vacuum"]
ROOM_TIME_OPTIONS = [USE_GLOBAL_OPTION, *TIME_OPTIONS]
CLEANING_PERIOD_OPTIONS = list(ROOM_CLEANING_PERIOD_OPTIONS)
CLEANING_PROFILE_OPTIONS = list(ROOM_CLEANING_PROFILE_OPTIONS)
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

    def __init__(
        self, coordinator: AdaptiveRoboVacsCoordinator, key: str, name: str
    ) -> None:
        super().__init__(coordinator, f"global_{key}", name, "global_control")
        self.key = key

    @property
    def current_option(self) -> str:
        return str(self.coordinator.data.scheduler.global_setting(self.key))

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_execute(SetGlobalCommand(self.key, option))


class _RoomTimeSelect(AdaptiveEntity, SelectEntity):
    _attr_options = ROOM_TIME_OPTIONS

    def __init__(
        self,
        coordinator: AdaptiveRoboVacsCoordinator,
        area_id: str,
        key: str,
        name: str,
    ) -> None:
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
        room = self.room_view(self.area_id)
        configured = (
            room.desired_window_configured_start
            if self.key == "desired_window_start"
            else room.desired_window_configured_end
        )
        return str(configured) if configured is not None else USE_GLOBAL_OPTION

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_execute(
            SetRoomSettingCommand(
                self.area_id,
                self.key,
                None if option == USE_GLOBAL_OPTION else option,
            )
        )


class _RoomCleaningPeriodSelect(AdaptiveEntity, SelectEntity):
    """A concise per-room scheduling control for mobile dashboards."""

    _attr_options = CLEANING_PERIOD_OPTIONS

    def __init__(
        self, coordinator: AdaptiveRoboVacsCoordinator, area_id: str, name: str
    ) -> None:
        super().__init__(
            coordinator,
            f"room_{area_id}_cleaning_period",
            name,
            "room_cleaning_period_control",
            area_id=area_id,
        )
        self.area_id = area_id

    @property
    def current_option(self) -> str:
        return self.room_view(self.area_id).cleaning_period

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_execute(
            SetRoomCleaningPeriodCommand(self.area_id, option)
        )


class _RoomCleaningProfileSelect(AdaptiveEntity, SelectEntity):
    """Choose inherited robot defaults or reveal room-level profile controls."""

    _attr_options = CLEANING_PROFILE_OPTIONS

    def __init__(
        self, coordinator: AdaptiveRoboVacsCoordinator, area_id: str, name: str
    ) -> None:
        super().__init__(
            coordinator,
            f"room_{area_id}_cleaning_profile",
            name,
            "room_cleaning_profile_control",
            area_id=area_id,
        )
        self.area_id = area_id

    @property
    def current_option(self) -> str:
        return self.room_view(self.area_id).cleaning_profile

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_execute(
            SetRoomCleaningProfileCommand(self.area_id, option)
        )


class _RoomPassSelect(AdaptiveEntity, SelectEntity):
    _attr_options = PASS_OPTIONS

    def __init__(
        self,
        coordinator: AdaptiveRoboVacsCoordinator,
        area_id: str,
        operation: str,
        name: str,
    ) -> None:
        key = "vacuum_pass_count" if operation == "vacuum" else "mop_pass_count"
        unique_key = "pass_count" if operation == "vacuum" else "mop_pass_count"
        super().__init__(
            coordinator,
            f"room_{area_id}_{unique_key}",
            name,
            "room_pass_count_control"
            if operation == "vacuum"
            else "room_mop_pass_count_control",
            area_id=area_id,
        )
        self.area_id = area_id
        self.key = key

    @property
    def current_option(self) -> str:
        room = self.room_view(self.area_id)
        value = (
            room.vacuum_pass_count
            if self.key == "vacuum_pass_count"
            else room.mop_pass_count
        )
        return (
            "Robot default"
            if value is None
            else f"{value} pass" + ("es" if value == 2 else "")
        )

    async def async_select_option(self, option: str) -> None:
        value = {"Robot default": None, "1 pass": 1, "2 passes": 2}[option]
        await self.coordinator.async_execute(
            SetRoomSettingCommand(self.area_id, self.key, value)
        )


class _RobotProgramSelect(AdaptiveEntity, SelectEntity):
    def __init__(
        self, coordinator: AdaptiveRoboVacsCoordinator, robot_entity_id: str
    ) -> None:
        super().__init__(
            coordinator,
            "robot_"
            f"{robot_unique_fragment(coordinator, robot_entity_id)}_cleaning_program",
            "cleaning program",
            "robot_control",
            robot_entity_id=robot_entity_id,
            robot_name_suffix="cleaning program",
        )
        self.robot_entity_id = robot_entity_id

    @property
    def options(self) -> list[str]:
        robot = self.robot_view(self.robot_entity_id)
        return (
            PROGRAM_OPTIONS
            if "mop" in robot.adapter_capabilities.supported_operations
            else ["Vacuum only"]
        )

    @property
    def current_option(self) -> str:
        value = self.robot_view(self.robot_entity_id).settings.cleaning_program.value
        label = PROGRAM_LABELS.get(value, "Vacuum only")
        return label if label in self.options else self.options[0]

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_execute(
            SetRobotSettingCommand(
                self.robot_entity_id,
                "cleaning_program",
                PROGRAM_VALUES[option],
            )
        )


class _RoomProgramSelect(AdaptiveEntity, SelectEntity):
    def __init__(
        self, coordinator: AdaptiveRoboVacsCoordinator, area_id: str, name: str
    ) -> None:
        super().__init__(
            coordinator,
            f"room_{area_id}_cleaning_program",
            name,
            "room_cleaning_program_control",
            area_id=area_id,
        )
        self.area_id = area_id

    @property
    def options(self) -> list[str]:
        room = self.room_view(self.area_id)
        supports_mopping = any(
            robot.floor_id == room.floor_id and "mop" in robot.supported_operations
            for robot in self.coordinator.data.robots
        )
        return (
            ["Robot default", *PROGRAM_OPTIONS]
            if supports_mopping
            else ["Robot default", "Vacuum only"]
        )

    @property
    def current_option(self) -> str:
        program = self.room_view(self.area_id).cleaning_program
        value = program.value if program else None
        label = PROGRAM_LABELS.get(value, "Robot default") if value else "Robot default"
        return label if label in self.options else self.options[0]

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_execute(
            SetRoomSettingCommand(
                self.area_id,
                "cleaning_program",
                None if option == "Robot default" else PROGRAM_VALUES[option],
            )
        )


class _RobotSelect(AdaptiveEntity, SelectEntity):
    def __init__(
        self,
        coordinator: AdaptiveRoboVacsCoordinator,
        robot_entity_id: str,
        key: str,
        options: tuple[str, ...],
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
        self.key = key
        self._fallback_options = options

    @property
    def options(self) -> list[str]:
        robot = self.coordinator.data.robot_by_entity_id(self.robot_entity_id)
        if robot:
            options = {
                "fan_speed": robot.fan_speed_options,
                "mode": robot.mode_options,
                "mop_mode": robot.mop_mode_options,
                "mop_intensity": robot.mop_intensity_options,
                "cleaning_depth": robot.cleaning_depth_options,
            }[self.key]
        else:
            options = self._fallback_options
        direct_mop_setting = bool(
            robot
            and robot.native_mop_profile
            and self.key in {"mop_mode", "mop_intensity"}
        )
        if direct_mop_setting:
            options = tuple(
                option
                for option in options
                if is_native_mop_profile_value(self.key, option)
            )
        visible = ([NOT_CONFIGURED_OPTION] if not direct_mop_setting else []) + list(
            options
        )
        settings = self.robot_view(self.robot_entity_id).settings
        saved = {
            "fan_speed": settings.fan_speed,
            "mode": settings.mode,
            "mop_mode": settings.mop_mode,
            "mop_intensity": settings.mop_intensity,
            "cleaning_depth": settings.cleaning_depth,
        }[self.key]
        if (
            not direct_mop_setting
            and isinstance(saved, str)
            and saved
            and saved not in visible
        ):
            visible.append(saved)
        return visible

    @property
    def current_option(self) -> str | None:
        settings = self.robot_view(self.robot_entity_id).settings
        setting = {
            "fan_speed": settings.fan_speed,
            "mode": settings.mode,
            "mop_mode": settings.mop_mode,
            "mop_intensity": settings.mop_intensity,
            "cleaning_depth": settings.cleaning_depth,
        }[self.key]
        return setting if setting in self.options else NOT_CONFIGURED_OPTION

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_execute(
            SetRobotSettingCommand(
                self.robot_entity_id,
                self.key,
                None if option == NOT_CONFIGURED_OPTION else option,
            )
        )


class _MapRecoveryPreviewSelect(AdaptiveEntity, SelectEntity):
    """Select an archived map image only; this never talks to the robot."""

    def __init__(
        self, coordinator: AdaptiveRoboVacsCoordinator, robot_entity_id: str
    ) -> None:
        unique_fragment = robot_unique_fragment(coordinator, robot_entity_id)
        super().__init__(
            coordinator,
            f"robot_{unique_fragment}_map_recovery_preview",
            "map snapshot preview",
            "robot_map_snapshot_preview_select",
            robot_entity_id=robot_entity_id,
            robot_name_suffix="map snapshot preview",
        )
        self.robot_entity_id = robot_entity_id

    @property
    def options(self) -> list[str]:
        return list(self.map_view(self.robot_entity_id).preview_options)

    @property
    def current_option(self) -> str | None:
        return self.map_view(self.robot_entity_id).selected_preview_option

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_execute(
            SelectMapPreviewCommand(self.robot_entity_id, option)
        )


class _RoomProfileSelect(AdaptiveEntity, SelectEntity):
    """One room override backed by the union of same-floor robot options."""

    def __init__(
        self,
        coordinator: AdaptiveRoboVacsCoordinator,
        area_id: str,
        name: str,
        key: str,
        label: str,
    ) -> None:
        super().__init__(
            coordinator,
            f"room_{area_id}_{key}",
            f"{name} {label}",
            f"room_{key}_control",
            area_id=area_id,
        )
        self.area_id = area_id
        self.key = key

    def _floor_options(self) -> list[str]:
        room = self.room_view(self.area_id)
        values: list[str] = []
        for robot in self.coordinator.data.robots:
            if robot.floor_id != room.floor_id:
                continue
            options = {
                "fan_speed": robot.fan_speed_options,
                "mode": robot.mode_options,
                "mop_mode": robot.mop_mode_options,
                "mop_intensity": robot.mop_intensity_options,
                "cleaning_depth": robot.cleaning_depth_options,
            }[self.key]
            for option in options:
                if option not in values:
                    values.append(option)
        return values

    @property
    def options(self) -> list[str]:
        visible = ["Robot default", *self._floor_options()]
        room = self.room_view(self.area_id)
        saved = {
            "fan_speed": room.fan_speed,
            "mode": room.mode,
            "mop_mode": room.mop_mode,
            "mop_intensity": room.mop_intensity,
            "cleaning_depth": room.cleaning_depth,
        }[self.key]
        if isinstance(saved, str) and saved and saved not in visible:
            visible.append(saved)
        return visible

    @property
    def current_option(self) -> str:
        room = self.room_view(self.area_id)
        value = {
            "fan_speed": room.fan_speed,
            "mode": room.mode,
            "mop_mode": room.mop_mode,
            "mop_intensity": room.mop_intensity,
            "cleaning_depth": room.cleaning_depth,
        }[self.key]
        return (
            value
            if isinstance(value, str) and value in self.options
            else "Robot default"
        )

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_execute(
            SetRoomSettingCommand(
                self.area_id,
                self.key,
                None if option == "Robot default" else option,
            )
        )


def _entities(coordinator: AdaptiveRoboVacsCoordinator) -> list[AdaptiveEntity]:
    entities: list[AdaptiveEntity] = [
        _TimeSelect(coordinator, "unresolved_start", "Desired cleaning start"),
        _TimeSelect(coordinator, "unresolved_end", "Desired cleaning end"),
    ]
    for robot in coordinator.data.robots:
        entities.append(
            _RobotProgramSelect(
                coordinator,
                robot.entity_id,
            )
        )
        if robot.fan_speed_options:
            entities.append(
                _RobotSelect(
                    coordinator,
                    robot.entity_id,
                    "fan_speed",
                    robot.fan_speed_options,
                    "fan speed",
                )
            )
        if robot.mode_select_available and robot.mode_options:
            label = (
                "vacuum cleaning mode (mopping uses native Mop with suction off)"
                if robot.native_mop_profile
                else "mode"
            )
            entities.append(
                _RobotSelect(
                    coordinator, robot.entity_id, "mode", robot.mode_options, label
                )
            )
        if robot.mop_mode_select_available and robot.mop_mode_options:
            label = "native mop route" if robot.native_mop_profile else "mop mode"
            entities.append(
                _RobotSelect(
                    coordinator,
                    robot.entity_id,
                    "mop_mode",
                    robot.mop_mode_options,
                    label,
                )
            )
        if robot.mop_intensity_select_available and robot.mop_intensity_options:
            label = (
                "native mop water intensity"
                if robot.native_mop_profile
                else "mop intensity"
            )
            entities.append(
                _RobotSelect(
                    coordinator,
                    robot.entity_id,
                    "mop_intensity",
                    robot.mop_intensity_options,
                    label,
                )
            )
        if robot.cleaning_depth_options:
            entities.append(
                _RobotSelect(
                    coordinator,
                    robot.entity_id,
                    "cleaning_depth",
                    robot.cleaning_depth_options,
                    "cleaning depth",
                )
            )
        if (
            (map_view := coordinator.data.map_for_robot(robot.registry_id))
            and map_view.available
            and map_view.preview_options
        ):
            entities.append(_MapRecoveryPreviewSelect(coordinator, robot.entity_id))
    for room in coordinator.data.rooms:
        supports_mopping = any(
            robot.floor_id == room.floor_id and "mop" in robot.supported_operations
            for robot in coordinator.data.robots
        )
        entities.extend(
            [
                _RoomCleaningPeriodSelect(
                    coordinator,
                    room.area_id,
                    f"{room.name} cleaning period",
                ),
                _RoomCleaningProfileSelect(
                    coordinator,
                    room.area_id,
                    f"{room.name} cleaning profile",
                ),
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AdaptiveRoboVacsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up dynamically discovered profile selects."""

    coordinator = entry.runtime_data.coordinator
    async_setup_dynamic_entities(
        entry, async_add_entities, coordinator, lambda: _entities(coordinator)
    )
