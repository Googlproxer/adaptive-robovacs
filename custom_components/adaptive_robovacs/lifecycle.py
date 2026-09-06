"""Home Assistant subscriptions and scheduled wakeups."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import datetime, timedelta
from typing import Any

from homeassistant.const import (
    EVENT_CALL_SERVICE,
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_STATE_CHANGED,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .commands import EvaluateCommand
from .models import EvaluationCause, EvaluationMode


class SchedulerRuntime:
    """Own external callbacks for one scheduler application."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        interval_handler: Callable[[datetime], Coroutine[Any, Any, None]],
        call_service_handler: Callable[[Event[Any]], None],
        state_changed_handler: Callable[[Event[Any]], None],
        device_registry_handler: Callable[[Event[Any]], None],
        notification_action_handler: Callable[[Event[Any]], None],
        notification_cleared_handler: Callable[[Event[Any]], None],
        home_assistant_started_handler: Callable[[Event[Any]], None],
        submit: Callable[[EvaluateCommand], Coroutine[Any, Any, object]],
        create_task: Callable[[Coroutine[Any, Any, object]], object],
    ) -> None:
        self._hass = hass
        self._interval_handler = interval_handler
        self._call_service_handler = call_service_handler
        self._state_changed_handler = state_changed_handler
        self._device_registry_handler = device_registry_handler
        self._notification_action_handler = notification_action_handler
        self._notification_cleared_handler = notification_cleared_handler
        self._home_assistant_started_handler = home_assistant_started_handler
        self._submit = submit
        self._create_task = create_task
        self._unsubscribers: list[Callable[[], None]] = []

    async def async_start(self, settle_until: datetime) -> None:
        """Register callbacks only after durable recovery has completed."""

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
                    EVENT_STATE_CHANGED,
                    self._state_changed_handler,
                ),
                self._hass.bus.async_listen(
                    dr.EVENT_DEVICE_REGISTRY_UPDATED,
                    self._device_registry_handler,
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

        @callback
        def refresh_late_vendor_entities(_timestamp: datetime) -> None:
            self._create_task(
                self._submit(
                    EvaluateCommand(
                        mode=EvaluationMode.PREVIEW,
                        cause=EvaluationCause.CAPABILITY_REFRESH,
                        detail="post-start-capability-refresh",
                        coalesce=True,
                    )
                )
            )

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

    async def async_stop(self) -> None:
        """Unsubscribe every external callback exactly once."""

        while self._unsubscribers:
            self._unsubscribers.pop()()
