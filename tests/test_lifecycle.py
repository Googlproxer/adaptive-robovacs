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
)
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr

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
    async def test_night_boundaries_rearm_without_duplicate_or_stale_wakeups(self):
        submitted, tasks, points, cancelled = [], [], [], []
        now = WHEN
        window = ["23:00", "09:00"]

        async def submit(command):
            submitted.append(command)

        def create_task(coroutine):
            tasks.append(asyncio.create_task(coroutine))

        def track_point(_hass, callback, timestamp):
            points.append((callback, timestamp))
            return lambda: cancelled.append(timestamp)

        runtime = SchedulerRuntime(
            SimpleNamespace(bus=_Bus([])),
            interval_handler=lambda _now: submit("interval"),
            call_service_handler=lambda _e: None,
            state_changed_handler=lambda _e: None,
            registry_handler=lambda _e: None,
            notification_action_handler=lambda _e: None,
            notification_cleared_handler=lambda _e: None,
            home_assistant_started_handler=lambda _e: None,
            submit=submit,
            create_task=create_task,
            night_window=lambda: tuple(window),
        )
        with (
            patch(
                "custom_components.adaptive_robovacs.lifecycle.dt_util.now",
                side_effect=lambda: now,
            ),
            patch(
                "custom_components.adaptive_robovacs.lifecycle.dt_util.utcnow",
                side_effect=lambda: now,
            ),
            patch(
                "custom_components.adaptive_robovacs.lifecycle.async_track_point_in_utc_time",
                side_effect=track_point,
            ),
            patch(
                "custom_components.adaptive_robovacs.lifecycle.async_track_time_interval",
                return_value=lambda: None,
            ),
        ):
            runtime.update_adjacency_window()
            self.assertEqual(points, [])
            await runtime.async_start(WHEN + timedelta(minutes=1))
            original, original_time = points[0]
            self.assertEqual(original_time, WHEN.replace(hour=23))
            count = len(points)
            runtime.update_adjacency_window()
            self.assertEqual(len(points), count)
            window[0] = "21:00"
            runtime.update_adjacency_window()
            self.assertIn(original_time, cancelled)
            original(original_time)
            self.assertEqual(tasks, [])
            opening, opening_time = points[-1]
            now = opening_time
            opening(now)
            await asyncio.gather(*tasks)
            self.assertEqual(submitted[-1].cause, EvaluationCause.ADJACENCY_BOUNDARY)
            self.assertEqual(submitted[-1].mode, EvaluationMode.DISPATCH)
            closing, closing_time = points[-1]
            self.assertEqual(closing_time, (WHEN + timedelta(days=1)).replace(hour=9))
            now = closing_time
            closing(now)
            await asyncio.gather(*tasks)
            self.assertEqual(len(submitted), 2)
            stale, stale_time = points[-1]
            await runtime.async_stop()
            stale(stale_time)
            runtime.update_adjacency_window()
            self.assertEqual(len(tasks), 2)
            self.assertIsNone(runtime._night_deadline)

    async def test_registers_every_source_and_unsubscribes_exactly_once(self) -> None:
        unsubscribed: list[str] = []
        bus = _Bus(unsubscribed)
        hass = SimpleNamespace(bus=bus)
        interval_callbacks = []
        point_callbacks = []
        state_callbacks = []
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

        def track_states(_hass, entity_ids, callback):
            state_callbacks.append((frozenset(entity_ids), callback))
            return lambda: unsubscribed.append("states")

        runtime = SchedulerRuntime(
            hass,
            interval_handler=lambda _now: submit("interval"),
            call_service_handler=lambda _event: None,
            state_changed_handler=lambda _event: None,
            registry_handler=lambda _event: None,
            notification_action_handler=lambda _event: None,
            notification_cleared_handler=lambda _event: None,
            home_assistant_started_handler=lambda _event: None,
            submit=submit,
            create_task=create_task,
        )
        runtime.update_state_watchers({"vacuum.alpha", "binary_sensor.study"})
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
            patch(
                "custom_components.adaptive_robovacs.lifecycle."
                "async_track_state_change_event",
                side_effect=track_states,
            ),
        ):
            await runtime.async_start(WHEN + timedelta(minutes=1))
            runtime.update_state_watchers({"vacuum.beta"})

        self.assertEqual(interval_callbacks[0][1], timedelta(minutes=15))
        self.assertEqual(
            [item[0] for item in state_callbacks],
            [
                frozenset({"vacuum.alpha", "binary_sensor.study"}),
                frozenset({"vacuum.beta"}),
            ],
        )
        self.assertEqual(unsubscribed.count("states"), 1)
        self.assertIn(EVENT_CALL_SERVICE, bus.callbacks)
        self.assertIn(dr.EVENT_DEVICE_REGISTRY_UPDATED, bus.callbacks)
        self.assertIn(er.EVENT_ENTITY_REGISTRY_UPDATED, bus.callbacks)
        self.assertIn(ar.EVENT_AREA_REGISTRY_UPDATED, bus.callbacks)
        self.assertIn(lr.EVENT_LABEL_REGISTRY_UPDATED, bus.callbacks)
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

        self.assertEqual(submitted[0].reason, "post-start-capability-refresh")
        self.assertEqual(submitted[1].mode, EvaluationMode.PREVIEW)
        self.assertEqual(
            submitted[1].cause,
            EvaluationCause.CAPABILITY_REFRESH,
        )
        self.assertTrue(submitted[1].coalesce)
        self.assertEqual(submitted[2].mode, EvaluationMode.DISPATCH)
        self.assertEqual(submitted[2].cause, EvaluationCause.STARTUP_SETTLED)

        await runtime.async_stop()
        first_count = len(unsubscribed)
        await runtime.async_stop()
        self.assertEqual(first_count, 13)
        self.assertEqual(len(unsubscribed), first_count)


if __name__ == "__main__":
    unittest.main()
