"""Compatibility for map-selection holds saved before map capture was removed."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ..adapters.roborock import RoborockMappingError, resolve_roborock_area_mapping
from ..discovery import DiscoveredRobot, DiscoverySnapshot
from ..models import map_recovery_hold_is_manual
from ..repair_service import RepairService
from ..state import SchedulerState
from ..storage import SchedulerStore


class ApplicationLegacyMapMixin:
    """Release an old maintenance hold only through a non-dispatching Repair."""

    hass: HomeAssistant
    state: SchedulerState
    discovery: DiscoverySnapshot
    storage: SchedulerStore
    repairs: RepairService
    _lock: asyncio.Lock
    _storage_safe_mode: bool
    _closing: bool

    if TYPE_CHECKING:

        async def async_refresh_discovery(self, *, notify: bool = True) -> None: ...

        def robot_for_registry_id(self, registry_id: str) -> DiscoveredRobot | None: ...

        def _reset_ready_confirmation(self, entity_id: str) -> None: ...

        def _notify_listeners(self) -> None: ...

    def _sync_retired_map_issues(self) -> None:
        self.repairs.sync_retired_map_holds(
            self.state.robot_holds, self.discovery.robots.values()
        )

    async def async_acknowledge_retired_map(
        self, registry_id: str, held_at: str
    ) -> dict[str, object]:
        """Validate current HA mapping and save before releasing the old hold."""

        async with self._lock:
            if self._closing or self._storage_safe_mode:
                return {"cleared": False, "reason": "recovery_unavailable"}
            await self.async_refresh_discovery(notify=False)
            hold = self.state.robot_holds.get(registry_id)
            if (
                hold is None
                or not map_recovery_hold_is_manual(hold.reason)
                or (hold.held_at.isoformat() if hold.held_at else "") != held_at
            ):
                return {"cleared": False, "reason": "recovery_changed"}
            robot = self.robot_for_registry_id(registry_id)
            observed = self.hass.states.get(robot.entity_id) if robot else None
            if (
                robot is None
                or observed is None
                or observed.state not in {"docked", "idle"}
                or self.state.active_jobs.get(registry_id) is not None
            ):
                return {"cleared": False, "reason": "awaiting_safe_dock"}
            entity = er.async_get(self.hass).async_get(robot.entity_id)
            options = entity.options.get("vacuum") if entity else None
            mapping = (
                options.get("area_mapping") if isinstance(options, Mapping) else None
            )
            if (
                not robot.supports_area_clean
                or not isinstance(options, Mapping)
                or not isinstance(mapping, Mapping)
                or not mapping
            ):
                return {"cleared": False, "reason": "mapping_invalid"}
            try:
                for area_id in mapping:
                    resolve_roborock_area_mapping(options, (str(area_id),))
            except RoborockMappingError:
                return {"cleared": False, "reason": "mapping_invalid"}
            holds = dict(self.state.robot_holds)
            holds.pop(registry_id)
            updated = replace(self.state, robot_holds=holds)
            await self.storage.async_save(updated)
            self.state = updated
            self._reset_ready_confirmation(robot.entity_id)
            self._sync_retired_map_issues()
            self._notify_listeners()
            return {"cleared": True, "dispatch_started": False}
