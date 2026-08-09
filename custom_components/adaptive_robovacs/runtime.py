"""Home Assistant service boundary for Adaptive RoboVacs."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.util import slugify
from homeassistant.util import dt as dt_util

from .discovery import DiscoveredRobot, DiscoveredRoom

if TYPE_CHECKING:
    from .coordinator import AdaptiveRoboVacCoordinator


_LOGGER = logging.getLogger(__name__)


class HomeAssistantRuntime:
    """Perform native service calls without owning scheduler decisions or state."""

    def __init__(self, coordinator: AdaptiveRoboVacCoordinator) -> None:
        self._coordinator = coordinator

    async def async_apply_profile(self, robot: DiscoveredRobot, operation: str) -> None:
        """Set optional controls discovered on the selected robot device."""

        coordinator = self._coordinator
        profile = robot.profile
        settings = coordinator._robot_settings(robot)
        selections = (
            (profile.mode_select_entity_id, settings.get("mode")),
            (profile.mop_mode_select_entity_id, settings.get("mop_mode")),
            (profile.mop_intensity_select_entity_id, settings.get("mop_intensity")),
        )
        for entity_id, option in selections:
            if entity_id and option:
                await coordinator.hass.services.async_call(
                    "select", "select_option", {"entity_id": entity_id, "option": option}, blocking=True
                )
        if profile.passes_select_entity_id and settings.get("double_pass"):
            wanted = next(
                (
                    option
                    for option in profile.passes_options
                    if slugify(option) in {"two_pass", "double_pass"}
                ),
                None,
            )
            if wanted:
                await coordinator.hass.services.async_call(
                    "select",
                    "select_option",
                    {"entity_id": profile.passes_select_entity_id, "option": wanted},
                    blocking=True,
                )

    async def async_dispatch(
        self, robot: DiscoveredRobot, candidate: dict[str, Any], now: datetime
    ) -> tuple[bool, str]:
        """Checkpoint, dispatch native cleaning, and keep user errors generic."""

        coordinator = self._coordinator
        room: DiscoveredRoom = candidate["room"]
        active = {
            "room": room.area_id,
            "operation": candidate["operation"],
            "started": now.isoformat(),
            "seen_cleaning": False,
            "phase": "dispatching",
            "source": "scheduler",
            "expected_minutes": candidate["duration_minutes"],
            "expected_end": (now + timedelta(minutes=candidate["duration_minutes"])).isoformat(),
            "last_observed_at": now.isoformat(),
            "passes": candidate["passes"],
        }
        coordinator.data["active"][robot.entity_id] = active
        await coordinator._async_save()
        try:
            await self.async_apply_profile(robot, candidate["operation"])
            await coordinator.hass.services.async_call(
                "vacuum",
                "clean_area",
                {"entity_id": robot.entity_id, "cleaning_area_id": [room.area_id]},
                blocking=True,
            )
        except Exception:  # ServiceValidationError varies between HA versions.
            coordinator.data["active"][robot.entity_id] = None
            detail = coordinator._room_data(room.area_id)
            detail["map_status"] = "error"
            detail["map_error"] = "unknown dispatch error"
            _LOGGER.exception(
                "Adaptive RoboVacs room dispatch failed: robot=%s room=%s area_id=%s operation=%s",
                robot.entity_id,
                room.name,
                room.area_id,
                candidate["operation"],
            )
            await coordinator._async_save()
            return False, "dispatch failed: unknown error"
        active["phase"] = "accepted"
        active["accepted_at"] = dt_util.utcnow().isoformat()
        coordinator._room_data(room.area_id)["map_status"] = "mapped"
        coordinator._room_data(room.area_id)["map_error"] = None
        await coordinator._async_save()
        return True, f"dispatched {room.name}"
