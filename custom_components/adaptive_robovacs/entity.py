"""Shared entity helpers for Adaptive RoboVacs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import SIGNAL_DISCOVERY_UPDATED
from .coordinator import AdaptiveRoboVacsCoordinator
from .runtime_data import AdaptiveRoboVacsConfigEntry
from .snapshots import RobotView, RoomView


def robot_unique_fragment(
    coordinator: AdaptiveRoboVacsCoordinator, robot_entity_id: str
) -> str:
    """Return the durable fragment retained for an existing entity identity."""

    robot = coordinator.data.robot_by_entity_id(robot_entity_id)
    return robot.unique_fragment if robot else robot_entity_id


class AdaptiveEntity(CoordinatorEntity[AdaptiveRoboVacsCoordinator]):
    """Base entity backed by the scheduler's durable state."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: AdaptiveRoboVacsCoordinator,
        unique_key: str,
        name: str,
        role: str,
        area_id: str | None = None,
        robot_entity_id: str | None = None,
        robot_name_suffix: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{unique_key}"
        self._attr_name = name
        self._role = role
        self._area_id = area_id
        self._robot_entity_id = robot_entity_id
        robot = (
            coordinator.data.robot_by_entity_id(robot_entity_id)
            if robot_entity_id
            else None
        )
        self._robot_registry_id = robot.registry_id if robot else None
        self._robot_name_suffix = robot_name_suffix

    def _resolve_robot_entity_id(self) -> str | None:
        """Follow a vacuum entity rename through its stable registry entry."""

        if self._robot_registry_id:
            robot = self.coordinator.data.robot_by_registry_id(self._robot_registry_id)
            if robot:
                self._robot_entity_id = robot.entity_id
                if hasattr(self, "robot_entity_id"):
                    self.robot_entity_id = robot.entity_id
        return self._robot_entity_id

    def room_view(self, area_id: str) -> RoomView:
        """Return this update's room view."""

        room = self.coordinator.data.room(area_id)
        if room is None:
            raise KeyError(area_id)
        return room

    def robot_view(self, entity_id: str) -> RobotView:
        """Return this update's robot view, following registry renames."""

        current_entity_id = self._resolve_robot_entity_id() or entity_id
        robot = self.coordinator.data.robot_by_entity_id(current_entity_id)
        if robot is None:
            raise KeyError(current_entity_id)
        return robot

    @property
    def name(self) -> str | None:
        """Keep robot-owned entity labels aligned with the live robot name."""

        if self._robot_entity_id and self._robot_name_suffix:
            entity_id = self._resolve_robot_entity_id()
            robot = self.coordinator.data.robot_by_entity_id(entity_id or "")
            robot_name = robot.name if robot else self._robot_entity_id
            return f"{robot_name} {self._robot_name_suffix}"
        return self._attr_name

    @property
    def available(self) -> bool:
        """Mark entities unavailable when their registry-backed object disappears."""

        if not super().available:
            return False
        if self._area_id and self.coordinator.data.room(self._area_id) is None:
            return False
        self._resolve_robot_entity_id()
        return not self._robot_entity_id or any(
            robot.registry_id == self._robot_registry_id
            for robot in self.coordinator.data.robots
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Make custom cards discover entities without fixed entity IDs."""

        attributes: dict[str, Any] = {
            "adaptive_robovacs_entry_id": self.coordinator.entry.entry_id,
            "adaptive_robovacs_role": self._role,
        }
        if self._area_id:
            attributes["area_id"] = self._area_id
        if self._robot_entity_id:
            self._resolve_robot_entity_id()
            attributes["robot_entity_id"] = self._robot_entity_id
        return attributes


def async_setup_dynamic_entities(
    entry: AdaptiveRoboVacsConfigEntry,
    async_add_entities: AddEntitiesCallback,
    coordinator: AdaptiveRoboVacsCoordinator,
    factory: Callable[[], list[AdaptiveEntity]],
) -> None:
    """Add initial and newly discovered entities without an integration reload."""

    known: set[str] = set()

    @callback
    def add_entities(_entry_id: str | None = None) -> None:
        entities = [
            entity
            for entity in factory()
            if entity.unique_id is not None and entity.unique_id not in known
        ]
        known.update(entity.unique_id for entity in entities if entity.unique_id)
        if entities:
            entities_to_add: list[Entity] = list(entities)
            async_add_entities(entities_to_add)

    add_entities()
    entry.async_on_unload(
        async_dispatcher_connect(
            coordinator.hass,
            SIGNAL_DISCOVERY_UPDATED,
            add_entities,
        )
    )
