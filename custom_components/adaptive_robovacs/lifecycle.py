"""Home Assistant subscriptions and scheduled wakeups."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import datetime, timedelta
from typing import Any

from homeassistant.const import (
    EVENT_CALL_SERVICE,
    EVENT_HOMEASSISTANT_STARTED,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .commands import EvaluateCommand, RefreshDiscoveryCommand, SchedulerCommand
from .models import EvaluationCause, EvaluationMode, next_daily_window_boundary


class SchedulerRuntime:
    """Own external callbacks for one scheduler application."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        interval_handler: Callable[[datetime], Coroutine[Any, Any, None]],
        call_service_handler: Callable[[Event[Any]], None],
        state_changed_handler: Callable[[Event[Any]], None],
        registry_handler: Callable[[Event[Any]], None],
        notification_action_handler: Callable[[Event[Any]], None],
        notification_cleared_handler: Callable[[Event[Any]], None],
        home_assistant_started_handler: Callable[[Event[Any]], None],
        submit: Callable[[SchedulerCommand], Coroutine[Any, Any, object]],
        create_task: Callable[[Coroutine[Any, Any, object]], object],
        night_window: Callable[[], tuple[str, str]] | None = None,
    ) -> None:
        self._hass = hass
        self._interval_handler = interval_handler
        self._call_service_handler = call_service_handler
        self._state_changed_handler = state_changed_handler
        self._registry_handler = registry_handler
        self._notification_action_handler = notification_action_handler
        self._notification_cleared_handler = notification_cleared_handler
        self._home_assistant_started_handler = home_assistant_started_handler
        self._submit = submit
        self._create_task = create_task
        self._unsubscribers: list[Callable[[], None]] = []
        self._state_cancel: Callable[[], None] | None = None
        self._state_entity_ids: frozenset[str] = frozenset()
        self._night_window = night_window
        self._night_cancel: Callable[[], None] | None = None
        self._night_deadline: datetime | None = None
        self._running = False

    async def async_start(self, settle_until: datetime) -> None:
        """Register callbacks only after durable recovery has completed."""

        self._running = True
        self._bind_state_subscription()
        self.update_adjacency_window()
        self._unsubscribers.extend(
            [
                async_track_time_interval(
                    self._hass,
                    self._interval_handler,
                    timedelta(minutes=15),
                ),
                self._hass.bus.async_listen(
                    EVENT_CALL_SERVICE,
                    self._call_service_handler,
                ),
                self._hass.bus.async_listen(
                    dr.EVENT_DEVICE_REGISTRY_UPDATED,
                    self._registry_handler,
                ),
                self._hass.bus.async_listen(
                    er.EVENT_ENTITY_REGISTRY_UPDATED,
                    self._registry_handler,
                ),
                self._hass.bus.async_listen(
                    ar.EVENT_AREA_REGISTRY_UPDATED,
                    self._registry_handler,
                ),
                self._hass.bus.async_listen(
                    lr.EVENT_LABEL_REGISTRY_UPDATED,
                    self._registry_handler,
                ),
                self._hass.bus.async_listen(
                    "mobile_app_notification_action",
                    self._notification_action_handler,
                ),
                self._hass.bus.async_listen(
                    "mobile_app_notification_cleared",
                    self._notification_cleared_handler,
                ),
                self._hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STARTED,
                    self._home_assistant_started_handler,
                ),
            ]
        )

        async def async_refresh_late_vendor_entities() -> None:
            await self._submit(RefreshDiscoveryCommand("post-start-capability-refresh"))
            await self._submit(
                EvaluateCommand(
                    mode=EvaluationMode.PREVIEW,
                    cause=EvaluationCause.CAPABILITY_REFRESH,
                    detail="post-start-capability-refresh",
                    coalesce=True,
                )
            )

        @callback
        def refresh_late_vendor_entities(_timestamp: datetime) -> None:
            self._create_task(async_refresh_late_vendor_entities())

        self._unsubscribers.append(
            async_track_point_in_utc_time(
                self._hass,
                refresh_late_vendor_entities,
                dt_util.utcnow() + timedelta(seconds=30),
            )
        )

        @callback
        def finish_startup_state_settle(_timestamp: datetime) -> None:
            self._create_task(
                self._submit(
                    EvaluateCommand(
                        mode=EvaluationMode.DISPATCH,
                        cause=EvaluationCause.STARTUP_SETTLED,
                    )
                )
            )

        self._unsubscribers.append(
            async_track_point_in_utc_time(
                self._hass,
                finish_startup_state_settle,
                settle_until,
            )
        )

    @callback
    def stop_adjacency_timer(self) -> None:
        """Invalidate even an already queued boundary callback."""

        if self._night_cancel:
            self._night_cancel()
        self._night_cancel = None
        self._night_deadline = None

    @callback
    def update_state_watchers(self, entity_ids: set[str] | frozenset[str]) -> None:
        """Atomically subscribe only to registry-discovered state sources."""

        watched = frozenset(entity_ids)
        if watched == self._state_entity_ids:
            return
        self._state_entity_ids = watched
        self._bind_state_subscription()

    @callback
    def _bind_state_subscription(self) -> None:
        if self._state_cancel:
            self._state_cancel()
            self._state_cancel = None
        if self._running and self._state_entity_ids:
            self._state_cancel = async_track_state_change_event(
                self._hass,
                self._state_entity_ids,
                self._state_changed_handler,
            )

    @callback
    def update_adjacency_window(self) -> None:
        """Own a single timer for the next real local night transition."""

        if not self._running or self._night_window is None:
            return
        deadline = next_daily_window_boundary(dt_util.now(), *self._night_window())
        if deadline == self._night_deadline:
            return
        self.stop_adjacency_timer()
        if deadline is None:
            return
        self._night_deadline = deadline

        @callback
        def boundary_reached(_timestamp: datetime) -> None:
            if not self._running or self._night_deadline != deadline:
                return
            self._night_cancel = None
            self._night_deadline = None
            self.update_adjacency_window()
            self._create_task(
                self._submit(
                    EvaluateCommand(
                        mode=EvaluationMode.DISPATCH,
                        cause=EvaluationCause.ADJACENCY_BOUNDARY,
                        coalesce=True,
                    )
                )
            )

        self._night_cancel = async_track_point_in_utc_time(
            self._hass, boundary_reached, deadline
        )

    async def async_stop(self) -> None:
        """Unsubscribe every external callback exactly once."""

        self._running = False
        if self._state_cancel:
            self._state_cancel()
            self._state_cancel = None
        self.stop_adjacency_timer()
        while self._unsubscribers:
            self._unsubscribers.pop()()
