"""Read-only map-recovery previews."""

from __future__ import annotations

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .entity import AdaptiveEntity, async_setup_dynamic_entities


class _MapRecoveryCamera(AdaptiveEntity, Camera):
    """Serve the currently selected archived preview, never a live robot map."""

    _attr_content_type = "image/png"

    def __init__(self, coordinator, robot_entity_id: str) -> None:
        AdaptiveEntity.__init__(
            self,
            coordinator,
            f"robot_{coordinator.robot_unique_fragment(robot_entity_id)}_map_recovery_preview",
            "map recovery preview",
            "robot_map_recovery_preview",
            robot_entity_id=robot_entity_id,
            robot_name_suffix="map recovery preview",
        )
        Camera.__init__(self)
        self.robot_entity_id = robot_entity_id

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        return self.coordinator.map_recovery.selected_preview(self.robot_entity_id)


def _entities(coordinator) -> list[AdaptiveEntity]:
    return [
        _MapRecoveryCamera(coordinator, robot.entity_id)
        for robot in coordinator.discovery.robots.values()
        if coordinator.map_recovery.capability(robot.entity_id).available
        and coordinator.map_recovery.preview_options(robot.entity_id)
    ]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    """Set up one safe preview camera per supported robot."""

    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_setup_dynamic_entities(entry, async_add_entities, coordinator, lambda: _entities(coordinator))
