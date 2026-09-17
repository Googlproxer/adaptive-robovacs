"""Push-only Home Assistant data coordinator for Adaptive RoboVacs."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .commands import SchedulerCommand, SchedulerCommandResult
from .metrics import RuntimeMetrics
from .snapshots import IntegrationSnapshot, SnapshotDelta

_LOGGER = logging.getLogger(__name__)


class CoordinatorSource(Protocol):
    """Application port required by the push-only coordinator."""

    hass: HomeAssistant
    entry: ConfigEntry
    metrics: RuntimeMetrics

    def async_add_listener(
        self,
        listener: Callable[[IntegrationSnapshot | Exception], None],
    ) -> Callable[[], None]: ...

    def current_snapshot(self) -> IntegrationSnapshot: ...

    def async_execute(
        self,
        command: SchedulerCommand,
    ) -> Awaitable[SchedulerCommandResult]: ...


class AdaptiveRoboVacsCoordinator(DataUpdateCoordinator[IntegrationSnapshot]):
    """Publish immutable application snapshots without polling or mutation."""

    def __init__(self, application: CoordinatorSource) -> None:
        self.entry = application.entry
        self.metrics = getattr(application, "metrics", RuntimeMetrics())
        self._submit = application.async_execute
        super().__init__(
            application.hass,
            _LOGGER,
            name=f"adaptive_robovacs_{application.entry.entry_id}",
            config_entry=application.entry,
            update_interval=None,
            always_update=False,
        )
        self.data = application.current_snapshot()
        self._delta = SnapshotDelta.initial()
        self._remove_application_listener: Callable[[], None] = (
            application.async_add_listener(self._handle_application_update)
        )

    def _handle_application_update(
        self, update: IntegrationSnapshot | Exception
    ) -> None:
        """Publish one immutable snapshot produced by the application."""

        if isinstance(update, Exception):
            self._delta = SnapshotDelta.initial()
            self.async_set_update_error(update)
            return
        self._delta = (
            SnapshotDelta.between(self.data, update)
            if self.last_update_success
            else SnapshotDelta.initial()
        )
        self.async_set_updated_data(update)

    def scope_changed(
        self,
        *,
        area_id: str | None,
        robot_registry_id: str | None,
        global_dependency: bool,
    ) -> bool:
        """Return whether one entity's declared projection dependencies changed."""

        if self._delta.full or not self.last_update_success:
            return True
        if global_dependency and self._delta.room_status_global:
            return True
        if area_id is not None:
            return area_id in self._delta.room_ids
        if robot_registry_id is not None:
            return robot_registry_id in self._delta.robot_registry_ids
        return bool(
            self._delta.scheduler
            or self._delta.room_ids
            or self._delta.robot_registry_ids
        )

    def close(self) -> None:
        """Release the application-to-coordinator update bridge."""

        self._remove_application_listener()

    async def async_execute(self, command: SchedulerCommand) -> SchedulerCommandResult:
        """Submit one typed command to the application worker."""

        return await self._submit(command)
