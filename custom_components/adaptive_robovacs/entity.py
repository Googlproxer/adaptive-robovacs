"""Shared entity helpers for Adaptive RoboVacs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, SIGNAL_DISCOVERY_UPDATED
from .coordinator import AdaptiveRoboVacCoordinator


class AdaptiveEntity(Entity):
    """Base entity backed by the scheduler's durable state."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: AdaptiveRoboVacCoordinator,
        unique_key: str,
        name: str,
        role: str,
        area_id: str | None = None,
        robot_entity_id: str | None = None,
        robot_name_suffix: str | None = None,
    ) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{unique_key}"
        self._attr_name = name
        self._role = role
        self._area_id = area_id
        self._robot_entity_id = robot_entity_id
        robot = (
            coordinator.discovery.robots.get(robot_entity_id)
            if robot_entity_id
            else None
        )
        self._robot_registry_id = robot.registry_id if robot else None
        self._robot_name_suffix = robot_name_suffix

    def _resolve_robot_entity_id(self) -> str | None:
        """Follow a vacuum entity rename through its stable registry entry."""

        if self._robot_registry_id:
            robot = self.coordinator.robot_for_registry_id(self._robot_registry_id)
            if robot:
                self._robot_entity_id = robot.entity_id
                if hasattr(self, "robot_entity_id"):
                    self.robot_entity_id = robot.entity_id
        return self._robot_entity_id

    @property
    def name(self) -> str | None:
        """Keep robot-owned entity labels aligned with the live robot name."""

        if self._robot_entity_id and self._robot_name_suffix:
            entity_id = self._resolve_robot_entity_id()
            robot = self.coordinator.discovery.robots.get(entity_id)
            robot_name = robot.name if robot else self._robot_entity_id
            return f"{robot_name} {self._robot_name_suffix}"
        return self._attr_name

    async def async_added_to_hass(self) -> None:
        """Subscribe to scheduler updates."""

        self.async_on_remove(self.coordinator.async_add_listener(self._handle_update))

    @callback
    def _handle_update(self) -> None:
        """Publish a state change while the discovered object still exists.

        Discovery is live: applying an exclusion label can remove a room while
        its existing platform entities are still registered for this runtime.
        Those stale entities must not call back into ``room_state`` or
        ``robot_state`` until Home Assistant reloads the platform.
        """

        if self._area_id and self._area_id not in self.coordinator.discovery.rooms:
            return
        self._resolve_robot_entity_id()
        if self._robot_entity_id and self._robot_entity_id not in self.coordinator.discovery.robots:
            return
        self.async_write_ha_state()

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
    entry: ConfigEntry,
    async_add_entities: Callable[[list[Entity]], None],
    coordinator: AdaptiveRoboVacCoordinator,
    factory: Callable[[], list[AdaptiveEntity]],
) -> None:
    """Add initial and newly discovered entities without an integration reload."""

    known: set[str] = set()

    @callback
    def add_entities(_entry_id: str | None = None) -> None:
        entities = [
            entity for entity in factory() if entity.unique_id is not None and entity.unique_id not in known
        ]
        known.update(entity.unique_id for entity in entities if entity.unique_id)
        if entities:
            async_add_entities(entities)

    add_entities()
    entry.async_on_unload(
        async_dispatcher_connect(
            coordinator.hass,
            SIGNAL_DISCOVERY_UPDATED,
            add_entities,
        )
    )
