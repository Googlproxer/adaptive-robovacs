"""Read-only map snapshot previews."""

from __future__ import annotations

from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AdaptiveRoboVacsCoordinator
from .entity import (
    AdaptiveEntity,
    async_setup_dynamic_entities,
    robot_unique_fragment,
)
from .runtime_data import AdaptiveRoboVacsConfigEntry

PARALLEL_UPDATES = 0


class _MapRecoveryCamera(AdaptiveEntity, Camera):
    """Serve the currently selected archived preview, never a live robot map."""

    _attr_content_type = "image/png"

    def __init__(
        self, coordinator: AdaptiveRoboVacsCoordinator, robot_entity_id: str
    ) -> None:
        unique_fragment = robot_unique_fragment(coordinator, robot_entity_id)
        AdaptiveEntity.__init__(
            self,
            coordinator,
            f"robot_{unique_fragment}_map_recovery_preview",
            "map snapshot preview",
            "robot_map_snapshot_preview",
            robot_entity_id=robot_entity_id,
            robot_name_suffix="map snapshot preview",
        )
        Camera.__init__(self)
        self.robot_entity_id = robot_entity_id

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        return self.map_view(self.robot_entity_id).selected_preview


def _entities(coordinator: AdaptiveRoboVacsCoordinator) -> list[AdaptiveEntity]:
    return [
        _MapRecoveryCamera(coordinator, robot.entity_id)
        for robot in coordinator.data.robots
        if (map_view := coordinator.data.map_for_robot(robot.registry_id))
        and map_view.available
        and map_view.preview_options
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AdaptiveRoboVacsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one safe preview camera per supported robot."""

    coordinator = entry.runtime_data.coordinator
    async_setup_dynamic_entities(
        entry, async_add_entities, coordinator, lambda: _entities(coordinator)
    )
