"""Behavioral tests for callback ownership and queued lifecycle work."""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.const import (
    EVENT_CALL_SERVICE,
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_STATE_CHANGED,
)
from homeassistant.helpers import device_registry as dr

from custom_components.adaptive_robovacs.lifecycle import SchedulerRuntime
from custom_components.adaptive_robovacs.models import (
    EvaluationCause,
    EvaluationMode,
)

WHEN = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)


class _Bus:
    def __init__(self, unsubscribed: list[str]) -> None:
        self.callbacks = {}
        self.once_callbacks = {}
        self._unsubscribed = unsubscribed

    def async_listen(self, event_type, callback):
        self.callbacks[event_type] = callback
        return lambda: self._unsubscribed.append(event_type)

    def async_listen_once(self, event_type, callback):
        self.once_callbacks[event_type] = callback
        return lambda: self._unsubscribed.append(event_type)


class SchedulerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_registers_every_source_and_unsubscribes_exactly_once(self) -> None:
        unsubscribed: list[str] = []
        bus = _Bus(unsubscribed)
        hass = SimpleNamespace(bus=bus)
        interval_callbacks = []
        point_callbacks = []
        submitted = []
        tasks: list[asyncio.Task[object]] = []

        async def submit(command):
            submitted.append(command)
            return None

        def create_task(coroutine):
            task = asyncio.create_task(coroutine)
            tasks.append(task)
            return task

        def track_interval(_hass, callback, interval):
            interval_callbacks.append((callback, interval))
            return lambda: unsubscribed.append("interval")

        def track_point(_hass, callback, timestamp):
            point_callbacks.append((callback, timestamp))
            return lambda: unsubscribed.append("point")

        runtime = SchedulerRuntime(
            hass,
            interval_handler=lambda _now: submit("interval"),
            call_service_handler=lambda _event: None,
            state_changed_handler=lambda _event: None,
            device_registry_handler=lambda _event: None,
            notification_action_handler=lambda _event: None,
            notification_cleared_handler=lambda _event: None,
            home_assistant_started_handler=lambda _event: None,
            submit=submit,
            create_task=create_task,
        )
        with (
            patch(
                "custom_components.adaptive_robovacs.lifecycle."
                "async_track_time_interval",
                side_effect=track_interval,
            ),
            patch(
                "custom_components.adaptive_robovacs.lifecycle."
                "async_track_point_in_utc_time",
                side_effect=track_point,
            ),
            patch(
                "custom_components.adaptive_robovacs.lifecycle.dt_util.utcnow",
                return_value=WHEN,
            ),
        ):
            await runtime.async_start(WHEN + timedelta(minutes=1))

        self.assertEqual(interval_callbacks[0][1], timedelta(minutes=15))
        self.assertIn(EVENT_CALL_SERVICE, bus.callbacks)
        self.assertIn(EVENT_STATE_CHANGED, bus.callbacks)
        self.assertIn(dr.EVENT_DEVICE_REGISTRY_UPDATED, bus.callbacks)
        self.assertIn("mobile_app_notification_action", bus.callbacks)
        self.assertIn("mobile_app_notification_cleared", bus.callbacks)
        self.assertIn(EVENT_HOMEASSISTANT_STARTED, bus.once_callbacks)
        self.assertEqual(
            tuple(timestamp for _callback, timestamp in point_callbacks),
            (WHEN + timedelta(seconds=30), WHEN + timedelta(minutes=1)),
        )

        for callback, _timestamp in point_callbacks:
            callback(WHEN)
        await asyncio.gather(*tasks)

        self.assertEqual(submitted[0].mode, EvaluationMode.PREVIEW)
        self.assertEqual(
            submitted[0].cause,
            EvaluationCause.CAPABILITY_REFRESH,
        )
        self.assertTrue(submitted[0].coalesce)
        self.assertEqual(submitted[1].mode, EvaluationMode.DISPATCH)
        self.assertEqual(submitted[1].cause, EvaluationCause.STARTUP_SETTLED)

        await runtime.async_stop()
        first_count = len(unsubscribed)
        await runtime.async_stop()
        self.assertEqual(first_count, 9)
        self.assertEqual(len(unsubscribed), first_count)


if __name__ == "__main__":
    unittest.main()
