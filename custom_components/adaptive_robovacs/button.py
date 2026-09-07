"""Buttons for safe scheduler diagnostics."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .commands import (
    EvaluateCommand,
    ManualCleanRoomCommand,
    RecheckAndResumeCommand,
    StopAndReturnCommand,
)
from .coordinator import AdaptiveRoboVacsCoordinator
from .entity import (
    AdaptiveEntity,
    async_setup_dynamic_entities,
    robot_unique_fragment,
)
from .models import EvaluationCause, EvaluationMode
from .runtime_data import AdaptiveRoboVacsConfigEntry

PARALLEL_UPDATES = 0


class _PreviewButton(AdaptiveEntity, ButtonEntity):
    def __init__(self, coordinator: AdaptiveRoboVacsCoordinator) -> None:
        super().__init__(
            coordinator, "preview_schedule", "Preview schedule", "scheduler_control"
        )

    async def async_press(self) -> None:
        await self.coordinator.async_execute(
            EvaluateCommand(
                mode=EvaluationMode.PREVIEW,
                cause=EvaluationCause.USER_PREVIEW,
            )
        )


class _ResumeButton(AdaptiveEntity, ButtonEntity):
    def __init__(self, coordinator: AdaptiveRoboVacsCoordinator) -> None:
        super().__init__(
            coordinator,
            "recheck_and_resume",
            "Recheck and resume",
            "fault_resume_control",
        )

    async def async_press(self) -> None:
        await self.coordinator.async_execute(RecheckAndResumeCommand())


class _StopAndReturnButton(AdaptiveEntity, ButtonEntity):
    """Return one vacuum to its dock and cancel its tracked clean, if any."""

    def __init__(
        self, coordinator: AdaptiveRoboVacsCoordinator, robot_entity_id: str
    ) -> None:
        super().__init__(
            coordinator,
            "robot_"
            f"{robot_unique_fragment(coordinator, robot_entity_id)}_stop_and_return",
            "stop and return to dock",
            "robot_stop_return_control",
            robot_entity_id=robot_entity_id,
            robot_name_suffix="stop and return to dock",
        )
        self.robot_entity_id = robot_entity_id

    async def async_press(self) -> None:
        await self.coordinator.async_execute(
            StopAndReturnCommand(
                self.robot_entity_id,
                context=getattr(self, "_context", None),
            )
        )


class _RoomManualCleanButton(AdaptiveEntity, ButtonEntity):
    """One non-queueing room action with its mode fixed by entity identity."""

    def __init__(
        self,
        coordinator: AdaptiveRoboVacsCoordinator,
        area_id: str,
        name: str,
        mode: str,
        label: str,
    ) -> None:
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
        await self.coordinator.async_execute(
            ManualCleanRoomCommand(
                self.area_id,
                self.mode,
                context_id=getattr(context, "id", None),
                user_id=getattr(context, "user_id", None),
            )
        )


def _entities(coordinator: AdaptiveRoboVacsCoordinator) -> list[AdaptiveEntity]:
    entities: list[AdaptiveEntity] = [
        _ResumeButton(coordinator),
        _PreviewButton(coordinator),
    ]
    entities.extend(
        _StopAndReturnButton(coordinator, robot.entity_id)
        for robot in coordinator.data.robots
    )
    for room in coordinator.data.rooms:
        entities.extend(
            [
                _RoomManualCleanButton(
                    coordinator, room.area_id, room.name, "configured", "manual clean"
                ),
                _RoomManualCleanButton(
                    coordinator,
                    room.area_id,
                    room.name,
                    "vacuum_only",
                    "manual vacuum only",
                ),
                _RoomManualCleanButton(
                    coordinator, room.area_id, room.name, "mop_only", "manual mop only"
                ),
            ]
        )
    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AdaptiveRoboVacsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the scheduler preview control."""

    coordinator = entry.runtime_data.coordinator
    async_setup_dynamic_entities(
        entry, async_add_entities, coordinator, lambda: _entities(coordinator)
    )
