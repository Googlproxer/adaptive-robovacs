"""Tests for typed Home Assistant event ingestion and observations."""

from __future__ import annotations

import asyncio
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from custom_components.adaptive_robovacs.application import SchedulerApplication
from custom_components.adaptive_robovacs.commands import (
    EvaluateCommand,
    ObservedManualCleanCommand,
    RefreshDiscoveryCommand,
    StateChangedCommand,
)
from custom_components.adaptive_robovacs.models import (
    CleaningOperation,
    ManualCleanRequest,
    RoomObservation,
)
from custom_components.adaptive_robovacs.observations import (
    HouseObservation,
    ObservedRoom,
)
from custom_components.adaptive_robovacs.state import Deferral, RobotCooldown
from tests.test_application_state import NOW, active_job, state_application


def event_application() -> tuple[SchedulerApplication, list[asyncio.Task]]:
    app = state_application()
    app._watch_entity_ids = {"vacuum.alpha", "sensor.alpha_battery"}
    app._effective_duration = Mock(return_value=(12.5, 3))
    app.async_execute = AsyncMock(return_value={})
    tasks = []

    def create_task(coro, *, name=None):
        task = asyncio.create_task(coro, name=name)
        tasks.append(task)
        return task

    app._async_create_task = create_task
    return app, tasks


class ApplicationEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_call_service_ingests_only_user_room_clean_requests(self) -> None:
        app, tasks = event_application()
        request = ManualCleanRequest("vacuum.alpha", ("study",))
        event = SimpleNamespace(
            data={
                "domain": "vacuum",
                "service": "clean_area",
                "service_data": {"entity_id": "vacuum.alpha", "area_id": "study"},
            },
            context=SimpleNamespace(user_id="user-1", id="context-1"),
        )
        with patch(
            "custom_components.adaptive_robovacs.application_events."
            "parse_manual_clean_request",
            return_value=request,
        ):
            app._on_call_service(event)
        await asyncio.gather(*tasks)
        command = app.async_execute.await_args.args[0]
        self.assertIsInstance(command, ObservedManualCleanCommand)
        self.assertEqual(command.context_id, "context-1")

        app.async_execute.reset_mock()
        app._on_call_service(SimpleNamespace(data={}, context=event.context))
        with patch(
            "custom_components.adaptive_robovacs.application_events."
            "parse_manual_clean_request",
            return_value=None,
        ):
            app._on_call_service(event)
        app.async_execute.assert_not_awaited()

    async def test_state_registry_and_interval_callbacks_enqueue_typed_work(
        self,
    ) -> None:
        app, tasks = event_application()
        old_state = SimpleNamespace(state="docked")
        new_state = SimpleNamespace(state="cleaning", last_changed=NOW)
        app._on_state_changed(
            SimpleNamespace(
                data={
                    "entity_id": "vacuum.alpha",
                    "old_state": old_state,
                    "new_state": new_state,
                }
            )
        )
        app._on_state_changed(SimpleNamespace(data={"entity_id": "sensor.unwatched"}))
        app._on_device_registry_updated(
            SimpleNamespace(data={"action": "create", "changes": {"labels": []}})
        )
        app._on_device_registry_updated(
            SimpleNamespace(data={"action": "update", "changes": {"name": "new"}})
        )
        app._on_device_registry_updated(
            SimpleNamespace(data={"action": "update", "changes": {"labels": []}})
        )
        app._on_home_assistant_started(SimpleNamespace())
        await asyncio.gather(*tasks)
        await app._async_interval(NOW)
        commands = [call.args[0] for call in app.async_execute.await_args_list]
        self.assertIsInstance(commands[0], StateChangedCommand)
        self.assertEqual(commands[0].old_state, "docked")
        self.assertEqual(commands[0].new_state, "cleaning")
        self.assertEqual(commands[0].changed_at, NOW)
        self.assertIsInstance(commands[1], RefreshDiscoveryCommand)
        self.assertIsInstance(commands[2], EvaluateCommand)
        self.assertTrue(commands[2].coalesce)
        self.assertIsInstance(commands[3], EvaluateCommand)
        self.assertTrue(commands[3].coalesce)

        app.async_refresh_discovery = AsyncMock()
        await app._async_refresh_discovery_after_device_label_change()
        app.async_refresh_discovery.assert_awaited_once()

    async def test_observed_manual_checkpoint_is_persisted_before_follow_up(
        self,
    ) -> None:
        app, tasks = event_application()
        request = ManualCleanRequest("vacuum.alpha", ("study",))
        with patch(
            "custom_components.adaptive_robovacs.application._now", return_value=NOW
        ):
            await app._async_record_observed_manual_clean(request, "context-1")
        await asyncio.gather(*tasks)

        job = app.state.active_jobs["registry-alpha"]
        self.assertEqual(job.room_ids, ["study"])
        self.assertEqual(job.expected_minutes, 12.5)
        self.assertEqual(job.expected_end, NOW + timedelta(minutes=12.5))
        self.assertEqual(job.manual_context_id, "context-1")
        self.assertEqual(app.state.audit.manual_events[-1].outcome, "requested")
        app.storage.async_save.assert_awaited_once_with(app.state)
        app._notify_listeners.assert_called_once()
        self.assertIsInstance(app.async_execute.await_args.args[0], EvaluateCommand)

        app.storage.async_save.reset_mock()
        app.state.active_jobs["registry-alpha"] = active_job()
        with patch(
            "custom_components.adaptive_robovacs.application._now", return_value=NOW
        ):
            await app._async_record_observed_manual_clean(request, "context-2")
        self.assertEqual(app.state.audit.manual_events[-1].outcome, "ignored")
        self.assertEqual(
            app.state.audit.manual_events[-1].reason,
            "scheduler job already active",
        )
        app.storage.async_save.assert_awaited_once()

        app.storage.async_save.reset_mock()
        await app._async_record_observed_manual_clean(
            ManualCleanRequest("vacuum.missing", ("study",)), "context-3"
        )
        await app._async_record_observed_manual_clean(
            ManualCleanRequest("vacuum.alpha", ("missing",)), "context-4"
        )
        app.storage.async_save.assert_not_awaited()

    def test_occupancy_observation_tracks_clear_intervals_and_bounds_history(
        self,
    ) -> None:
        app = state_application()
        history = app.state.room_history["study"]
        history.occupancy = "occupied"
        old_sample = SimpleNamespace(started_at=NOW - timedelta(days=100), minutes=5)
        history.occupancy_samples = [old_sample]
        app.observer = SimpleNamespace(
            house=Mock(
                return_value=HouseObservation(
                    rooms=(
                        ObservedRoom(
                            "study", RoomObservation("unoccupied", "radars", 0)
                        ),
                    ),
                    robots=(),
                )
            ),
            robot=Mock(return_value=SimpleNamespace(battery=88.0)),
        )
        app._observe_occupancy(NOW)
        self.assertEqual(history.occupancy, "unoccupied")
        self.assertEqual(history.unoccupied_since, NOW)
        self.assertEqual(history.occupancy_samples, [])
        self.assertEqual(app._robot_battery(app.discovery.robots["vacuum.alpha"]), 88.0)

        history.unoccupied_since = NOW - timedelta(minutes=20)
        app.observer.house.return_value = HouseObservation(
            rooms=(ObservedRoom("study", RoomObservation("occupied", "fallback", 2)),),
            robots=(),
        )
        app._observe_occupancy(NOW)
        self.assertEqual(history.occupancy, "occupied")
        self.assertIsNone(history.unoccupied_since)
        self.assertEqual(history.occupancy_samples[-1].minutes, 20)
        self.assertEqual(history.unavailable_radars, 2)

        history.occupancy = "unoccupied"
        history.unoccupied_since = NOW
        app._observe_occupancy(NOW)
        self.assertEqual(len(history.occupancy_samples), 1)

    def test_elapsed_robot_cooldowns_expire_independently(self) -> None:
        app = state_application()
        app.state.robot_cooldowns = {
            "expired": RobotCooldown(NOW, NOW - timedelta(minutes=1)),
            "active": RobotCooldown(NOW + timedelta(minutes=1), NOW),
        }
        app._expire_robot_cooldowns(NOW)
        self.assertNotIn("expired", app.state.robot_cooldowns)
        self.assertIn("active", app.state.robot_cooldowns)


class PublicCommandApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_record_manual_clean_persists_typed_audit(self) -> None:
        app = state_application()
        app._apply_manual_deferral = Mock(return_value=["study:vacuum"])
        with patch(
            "custom_components.adaptive_robovacs.application._now", return_value=NOW
        ):
            result = await app.async_record_manual_clean(
                "vacuum.alpha", ["study"], ["vacuum"]
            )
        self.assertEqual(result, {"changed": ["study:vacuum"]})
        app._apply_manual_deferral.assert_called_once_with(
            "vacuum.alpha", ["study"], [CleaningOperation.VACUUM], NOW
        )
        self.assertEqual(
            app.state.audit.manual_events[-1].robot_registry_id,
            "registry-alpha",
        )
        app.storage.async_save.assert_awaited_once_with(app.state)
        app._notify_listeners.assert_called_once()

    async def test_legacy_deferral_review_and_selective_clear(self) -> None:
        app = state_application()
        detail = app.state.room_history["study"]
        detail.deferrals = {
            "vacuum": Deferral(NOW + timedelta(hours=1), "legacy_unknown", NOW),
            "mop": Deferral(NOW + timedelta(hours=2), "manual_clean", NOW),
        }
        report = app.legacy_deferral_report()
        self.assertEqual(report[0]["area_id"], "study")
        self.assertEqual(report[0]["operations"][0]["operation"], "vacuum")

        result = await app.async_clear_legacy_deferrals(["missing", "study"])
        self.assertEqual(result["cleared"], ["study:vacuum"])
        self.assertIn("mop", detail.deferrals)
        app.storage.async_save.assert_awaited_once_with(app.state)
        app._notify_listeners.assert_called_once()

        app.storage.async_save.reset_mock()
        app._notify_listeners.reset_mock()
        result = await app.async_clear_legacy_deferrals(["study"])
        self.assertEqual(result["cleared"], [])
        app.storage.async_save.assert_not_awaited()
        app._notify_listeners.assert_not_called()

    async def test_stop_and_return_handles_shutdown_discovery_and_physical_state(
        self,
    ) -> None:
        app = state_application()
        app.gateway = SimpleNamespace(async_return_to_dock=AsyncMock())
        app._set_held_job_phase = Mock()
        app._cancel_start_confirmation = Mock()

        app._closing = True
        self.assertFalse(
            (await app.async_stop_and_return_to_dock("vacuum.alpha"))["accepted"]
        )
        app._closing = False
        with self.assertRaisesRegex(ValueError, "not discovered"):
            await app.async_stop_and_return_to_dock("vacuum.missing")

        for state, accepted, reason in (
            (None, False, "robot is unavailable"),
            ("unknown", False, "robot is unavailable"),
            ("docked", True, "robot is already docked"),
        ):
            with self.subTest(state=state):
                if state is None:
                    app.hass.states.values.pop("vacuum.alpha", None)
                else:
                    app.hass.states.values["vacuum.alpha"] = SimpleNamespace(
                        state=state
                    )
                result = await app.async_stop_and_return_to_dock("vacuum.alpha")
                self.assertEqual(result, {"accepted": accepted, "reason": reason})
        app.gateway.async_return_to_dock.assert_not_awaited()

    async def test_stop_and_return_checkpoints_active_job_after_gateway_call(
        self,
    ) -> None:
        app = state_application()
        app.gateway = SimpleNamespace(async_return_to_dock=AsyncMock())
        app._set_held_job_phase = Mock()
        app._cancel_start_confirmation = Mock()
        job = active_job()
        app.state.active_jobs["registry-alpha"] = job
        app.hass.states.values["vacuum.alpha"] = SimpleNamespace(state="cleaning")
        context = SimpleNamespace(id="context")
        with patch(
            "custom_components.adaptive_robovacs.application._now", return_value=NOW
        ):
            result = await app.async_stop_and_return_to_dock(
                "vacuum.alpha", context=context
            )
        self.assertTrue(result["accepted"])
        app.gateway.async_return_to_dock.assert_awaited_once_with(
            "vacuum.alpha", context
        )
        hold = app.state.robot_holds["registry-alpha"]
        self.assertEqual(hold.reason, "user_requested_return")
        self.assertEqual(hold.phase, "cancelling")
        self.assertEqual(hold.returning_at, NOW)
        app._set_held_job_phase.assert_called_once_with(
            "vacuum.alpha", job, "cancelling", NOW
        )
        app.storage.async_save.assert_awaited_once_with(app.state)

        app = state_application()
        app.gateway = SimpleNamespace(async_return_to_dock=AsyncMock())
        app.hass.states.values["vacuum.alpha"] = SimpleNamespace(state="idle")
        result = await app.async_stop_and_return_to_dock("vacuum.alpha")
        self.assertTrue(result["accepted"])
        app.storage.async_save.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
