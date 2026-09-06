"""Push-only Home Assistant data coordinator for Adaptive RoboVacs."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .commands import SchedulerCommand, SchedulerCommandResult
from .snapshots import IntegrationSnapshot

_LOGGER = logging.getLogger(__name__)


class CoordinatorSource(Protocol):
    """Application port required by the push-only coordinator."""

    hass: HomeAssistant
    entry: ConfigEntry

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
        self._remove_application_listener: Callable[[], None] = (
            application.async_add_listener(self._handle_application_update)
        )

    def _handle_application_update(
        self, update: IntegrationSnapshot | Exception
    ) -> None:
        """Publish one immutable snapshot produced by the application."""

        if isinstance(update, Exception):
            self.async_set_update_error(update)
            return
        self.async_set_updated_data(update)

    def close(self) -> None:
        """Release the application-to-coordinator update bridge."""

        self._remove_application_listener()

    async def async_execute(self, command: SchedulerCommand) -> SchedulerCommandResult:
        """Submit one typed command to the application worker."""

        return await self._submit(command)
