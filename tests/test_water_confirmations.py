"""Application tests for durable water-confirmation transactions."""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from custom_components.adaptive_robovacs.application import SchedulerApplication
from custom_components.adaptive_robovacs.commands import (
    EvaluateCommand,
    ExpireWaterConfirmationCommand,
    WaterConfirmationResponseCommand,
)
from custom_components.adaptive_robovacs.state import (
    SchedulerState,
    WaterConfirmation,
)

NOW = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
ENTRY_DATA = {
    "observe_only": False,
    "forecast_confidence": 75,
    "unresolved_start": "01:00",
    "unresolved_end": "05:00",
}


def confirmation(
    *,
    request_id: str = "request-1",
    status: str = "pending",
    expires_at: datetime | None = None,
) -> WaterConfirmation:
    return WaterConfirmation(
        request_id=request_id,
        occurrence_id="occurrence-1",
        room_id="study",
        robot_registry_id="registry-alpha",
        stage_index=1,
        confirm_hash=SchedulerApplication._action_hash("confirm-water"),
        cancel_hash=SchedulerApplication._action_hash("cancel-water"),
        tag="adaptive-water-study",
        sent_at=NOW - timedelta(minutes=1),
        expires_at=expires_at or NOW + timedelta(minutes=10),
        status=status,
    )


def water_application() -> SchedulerApplication:
    app = SchedulerApplication.__new__(SchedulerApplication)
    app.state = SchedulerState.create(ENTRY_DATA)
    app._lock = asyncio.Lock()
    app._water_confirmation_timers = {}
    app._async_save = AsyncMock()
    app._async_clear_mobile_notification = AsyncMock()
    app._skip_occurrence_stage = Mock()
    app.async_execute = AsyncMock(return_value={})
    app._created_tasks = []

    def create_task(coro, *, name=None):
        task = asyncio.create_task(coro, name=name)
        app._created_tasks.append(task)
        return task

    app._async_create_task = create_task
    return app


async def drain(app: SchedulerApplication) -> None:
    if app._created_tasks:
        await asyncio.gather(*app._created_tasks)


class WaterConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_timer_replacement_and_callbacks_enqueue_typed_commands(self) -> None:
        app = water_application()
        old_unsubscribe = Mock()
        app._water_confirmation_timers["request-1"] = old_unsubscribe
        tracked_unsubscribe = Mock()
        callback_holder = {}

        def track(_hass, callback, when):
            callback_holder["callback"] = callback
            callback_holder["when"] = when
            return tracked_unsubscribe

        app.hass = object()
        request = confirmation()
        with patch(
            "custom_components.adaptive_robovacs.application."
            "async_track_point_in_utc_time",
            side_effect=track,
        ):
            app._schedule_water_confirmation(request)

        old_unsubscribe.assert_called_once()
        self.assertIs(
            app._water_confirmation_timers[request.request_id], tracked_unsubscribe
        )
        self.assertEqual(callback_holder["when"], request.expires_at)
        callback_holder["callback"](request.expires_at)
        await drain(app)
        self.assertNotIn(request.request_id, app._water_confirmation_timers)
        self.assertIsInstance(
            app.async_execute.await_args.args[0], ExpireWaterConfirmationCommand
        )

        app._on_mobile_notification_action(
            SimpleNamespace(data={"action": "confirm-water"})
        )
        app._on_mobile_notification_action(SimpleNamespace(data={"action": 5}))
        app._on_mobile_notification_cleared(
            SimpleNamespace(
                data={
                    "adaptive_robovacs_request_id": "request-1",
                    "tag": "adaptive-water-study",
                }
            )
        )
        await drain(app)
        submitted = [call.args[0] for call in app.async_execute.await_args_list]
        self.assertIsInstance(submitted[-2], WaterConfirmationResponseCommand)
        self.assertEqual(submitted[-2].action, "confirm-water")
        self.assertTrue(submitted[-1].dismissed)
        self.assertEqual(submitted[-1].request_id, "request-1")

    async def test_restore_schedules_live_and_expires_stale_requests(self) -> None:
        app = water_application()
        expired = confirmation(
            request_id="expired", expires_at=NOW - timedelta(seconds=1)
        )
        pending = confirmation(request_id="pending")
        ignored = confirmation(request_id="ignored", status="cancelled")
        app.state.water_confirmations = {
            item.occurrence_id + item.request_id: item
            for item in (expired, pending, ignored)
        }
        app._async_expire_water_confirmation = AsyncMock()
        app._schedule_water_confirmation = Mock()

        with patch(
            "custom_components.adaptive_robovacs.application._now", return_value=NOW
        ):
            await app._async_restore_water_confirmations()

        app._async_expire_water_confirmation.assert_awaited_once_with("expired")
        app._schedule_water_confirmation.assert_called_once_with(pending)

    async def test_expiry_skips_unstarted_stage_and_preserves_active_confirmed(
        self,
    ) -> None:
        app = water_application()
        pending = confirmation()
        app.state.water_confirmations[pending.occurrence_id] = pending
        with patch(
            "custom_components.adaptive_robovacs.application._now", return_value=NOW
        ):
            await app._async_expire_water_confirmation(pending.request_id)
            await drain(app)

        self.assertEqual(pending.status, "expired")
        self.assertEqual(pending.responded_at, NOW)
        app._skip_occurrence_stage.assert_called_once()
        app._async_save.assert_awaited_once()
        app._async_clear_mobile_notification.assert_awaited_once_with(pending.tag)
        follow_up = app.async_execute.await_args.args[0]
        self.assertIsInstance(follow_up, EvaluateCommand)
        self.assertEqual(follow_up.detail, "water-confirmation-expired")

        app = water_application()
        confirmed = confirmation(status="confirmed")
        app.state.water_confirmations[confirmed.occurrence_id] = confirmed
        app.state.active_jobs["registry-alpha"] = SimpleNamespace(
            occurrence_id=confirmed.occurrence_id,
            stage_index=confirmed.stage_index,
            phase="cleaning",
        )
        await app._async_expire_water_confirmation(confirmed.request_id)
        self.assertEqual(confirmed.status, "confirmed")
        app._async_save.assert_not_awaited()

        await app._async_expire_water_confirmation("missing")
        app._async_save.assert_not_awaited()

    async def test_confirmation_and_cancellation_are_durable_before_follow_up(
        self,
    ) -> None:
        app = water_application()
        request = confirmation()
        app.state.water_confirmations[request.occurrence_id] = request
        with patch(
            "custom_components.adaptive_robovacs.application._now", return_value=NOW
        ):
            await app._async_handle_water_confirmation(action="confirm-water")
            await drain(app)

        self.assertEqual(request.status, "confirmed")
        self.assertEqual(request.responded_at, NOW)
        app._skip_occurrence_stage.assert_not_called()
        app._async_save.assert_awaited_once()
        app._async_clear_mobile_notification.assert_awaited_once_with(request.tag)
        self.assertEqual(
            app.async_execute.await_args.args[0].detail,
            "water-confirmation-confirmed",
        )

        app = water_application()
        request = confirmation()
        timer = Mock()
        app._water_confirmation_timers[request.request_id] = timer
        app.state.water_confirmations[request.occurrence_id] = request
        with patch(
            "custom_components.adaptive_robovacs.application._now", return_value=NOW
        ):
            await app._async_handle_water_confirmation(
                request_id=request.request_id,
                tag=request.tag,
                dismissed=True,
            )
            await drain(app)

        self.assertEqual(request.status, "cancelled")
        timer.assert_called_once()
        app._skip_occurrence_stage.assert_called_once()
        self.assertEqual(
            app.async_execute.await_args.args[0].detail,
            "water-confirmation-cancelled",
        )

        await app._async_handle_water_confirmation(action="unrelated")
        self.assertEqual(app._async_save.await_count, 1)


if __name__ == "__main__":
    unittest.main()
