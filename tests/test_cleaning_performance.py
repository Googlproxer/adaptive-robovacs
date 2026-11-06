"""Safety and durability regressions for active-clean performance paths."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from custom_components.adaptive_robovacs.commands import StateChangedCommand
from custom_components.adaptive_robovacs.models import JobPhase
from custom_components.adaptive_robovacs.sensor import _RobotStatusSensor
from custom_components.adaptive_robovacs.state import RobotHold, SchedulerState
from custom_components.adaptive_robovacs.storage import SchedulerStore
from tests.test_application_planning import planning_application
from tests.test_application_recovery import recovery_application
from tests.test_application_state import (
    ENTRY_DATA,
    NOW,
    active_job,
    robot,
    state_application,
)
from tests.test_entities import _Coordinator
from tests.test_storage import _Store


def progress_application():
    app = state_application()
    app.storage = SchedulerStore.__new__(SchedulerStore)
    app.storage._store = _Store(None)
    app.state.evaluation.last_evaluation_at = NOW
    app.hass.states.values["vacuum.alpha"] = SimpleNamespace(
        state="cleaning", last_changed=NOW - timedelta(minutes=1)
    )
    job = active_job()
    job.phase = JobPhase.CLEANING
    job.seen_cleaning = True
    job.started_at = NOW - timedelta(minutes=1)
    job.timer_start = 0
    app.state.active_jobs["registry-alpha"] = job
    app.async_execute = app._async_execute_command
    return app, job


def timer_command(previous=1.0, current=1.1):
    return StateChangedCommand(
        "sensor.alpha_cleaning_time",
        str(previous),
        str(current),
        NOW,
        current,
        previous,
    )


async def progress(app, *, seconds=1, command=None):
    with patch(
        "custom_components.adaptive_robovacs.application.core._now",
        return_value=NOW + timedelta(seconds=seconds),
    ):
        await app._async_handle_watched_state_change(
            command or timer_command(), capability_changed=False
        )


class CleaningProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_progress_preserves_both_robots_observation_checkpoints(self):
        app, first = progress_application()
        second_robot = robot("vacuum.beta", registry_id="registry-beta")
        app.discovery = replace(
            app.discovery,
            robots={**app.discovery.robots, second_robot.entity_id: second_robot},
        )
        second = replace(first)
        app.state.active_jobs[second_robot.registry_id] = second
        app.hass.states.values[second_robot.entity_id] = SimpleNamespace(
            state="cleaning", last_changed=NOW
        )
        await progress(app)
        restored, _ = SchedulerState.from_store(app.storage._store.payload, ENTRY_DATA)
        for job in restored.active_jobs.values():
            self.assertEqual(job.last_observed_at, NOW + timedelta(seconds=1))
        app.async_evaluate.assert_not_awaited()

    async def test_other_robot_hold_uncertainty_or_shared_timer_requires_full_path(
        self,
    ):
        for condition in ("held", "unavailable", "accepted", "shared_timer"):
            with self.subTest(condition=condition):
                app, first = progress_application()
                second_robot = robot("vacuum.beta", registry_id="registry-beta")
                if condition == "shared_timer":
                    second_robot = replace(
                        second_robot,
                        profile=replace(
                            second_robot.profile,
                            cleaning_time_entity_id="sensor.alpha_cleaning_time",
                        ),
                    )
                app.discovery = replace(
                    app.discovery,
                    robots={
                        **app.discovery.robots,
                        second_robot.entity_id: second_robot,
                    },
                )
                app.state.active_jobs[second_robot.registry_id] = replace(first)
                app.hass.states.values[second_robot.entity_id] = SimpleNamespace(
                    state="unavailable" if condition == "unavailable" else "cleaning",
                    last_changed=NOW,
                )
                if condition == "held":
                    app.state.robot_holds[second_robot.registry_id] = RobotHold(
                        "paused", "held"
                    )
                elif condition == "accepted":
                    app.state.active_jobs[
                        second_robot.registry_id
                    ].phase = JobPhase.ACCEPTED
                await progress(app)
                app.async_evaluate.assert_awaited_once()

    async def test_failed_progress_write_is_retried_without_losing_restart_evidence(
        self,
    ):
        app, _ = progress_application()
        await app._async_save()
        checkpoint = app.storage._store.payload
        with (
            patch.object(
                app.storage._store, "async_save", side_effect=OSError("disk failed")
            ),
            self.assertRaises(OSError),
        ):
            await progress(app)
        self.assertIs(app.storage._store.payload, checkpoint)
        self.assertIsNone(app._last_durable_fingerprint)
        app.async_evaluate.assert_not_awaited()
        await progress(app, seconds=2)
        restored, _ = SchedulerState.from_store(app.storage._store.payload, ENTRY_DATA)
        self.assertTrue(
            restored.active_jobs["registry-alpha"].native_timer_progress_seen
        )
        self.assertEqual(
            restored.active_jobs["registry-alpha"].last_observed_at,
            NOW + timedelta(seconds=2),
        )

    async def test_sequential_progress_stays_durable_without_replanning(self):
        app, job = progress_application()
        for second in range(1, 17):
            await progress(app, seconds=second)
            restored, _ = SchedulerState.from_store(
                app.storage._store.payload, ENTRY_DATA
            )
            checkpoint = restored.active_jobs["registry-alpha"]
            self.assertTrue(checkpoint.native_timer_progress_seen)
            self.assertEqual(
                checkpoint.last_observed_at, NOW + timedelta(seconds=second)
            )
        self.assertEqual(len(app.storage._store.saved), 16)
        self.assertTrue(job.native_timer_progress_seen)
        app.async_evaluate.assert_not_awaited()
        app._notify_listeners.assert_not_called()

    async def test_full_evaluation_resumes_at_thirty_second_boundary(self):
        app, _ = progress_application()
        await progress(app, seconds=29)
        app.async_evaluate.assert_not_awaited()
        await progress(app, seconds=30)
        app.async_evaluate.assert_awaited_once()

    async def test_nonroutine_timer_or_observed_state_never_uses_light_path(self):
        for state, previous, current, evaluated in (
            ("cleaning", 1.0, 0.0, NOW),
            ("cleaning", 1.0, 1.0, NOW),
            ("cleaning", None, 1.1, NOW),
            ("cleaning", 1.0, float("nan"), NOW),
            ("cleaning", 1.0, 1.1, None),
            ("cleaning", 1.0, 1.1, NOW + timedelta(seconds=2)),
            ("returning", 1.0, 1.1, NOW),
            ("docked", 1.0, 1.1, NOW),
            ("paused", 1.0, 1.1, NOW),
            ("unavailable", 1.0, 1.1, NOW),
        ):
            with self.subTest(state=state, previous=previous, current=current):
                app, _ = progress_application()
                app.hass.states.values["vacuum.alpha"].state = state
                app.state.evaluation.last_evaluation_at = evaluated
                await progress(app, command=timer_command(previous, current))
                app.async_evaluate.assert_awaited_once()

    async def test_pending_start_and_fault_or_hold_require_full_evaluation(self):
        for condition in (
            "accepted",
            "unseen",
            "hold",
            "robot_fault",
            "room_fault",
            "recovery",
            "adapter_watch",
        ):
            with self.subTest(condition=condition):
                app, job = progress_application()
                if condition == "accepted":
                    job.phase = JobPhase.ACCEPTED
                elif condition == "unseen":
                    job.seen_cleaning = False
                elif condition == "hold":
                    app.state.robot_holds["registry-alpha"] = RobotHold(
                        "paused", "held"
                    )
                elif condition == "adapter_watch":
                    robot = app.discovery.robots["vacuum.alpha"]
                    app.discovery = replace(
                        app.discovery,
                        robots={
                            robot.entity_id: replace(
                                robot,
                                adapter_capabilities=replace(
                                    robot.adapter_capabilities,
                                    watched_entity_ids=("sensor.alpha_cleaning_time",),
                                ),
                            )
                        },
                    )
                else:
                    section, key = {
                        "robot_fault": ("robot_faults", "registry-alpha"),
                        "room_fault": ("room_faults", "study"),
                        "recovery": ("room_recoveries", "study"),
                    }[condition]
                    # Exercise routing independently of the fault codec.
                    getattr(app.state, section)[key] = object()
                    app._async_save = AsyncMock()
                await progress(app)
                app.async_evaluate.assert_awaited_once()

    async def test_other_safety_events_remain_immediate_during_progress_window(self):
        for entity_id, old, new in (
            ("binary_sensor.study_radar", "off", "on"),
            ("sensor.alpha_status", "cleaning", "error"),
            ("sensor.alpha_battery", "81", "79"),
            ("vacuum.alpha", "cleaning", "returning"),
        ):
            with self.subTest(entity_id=entity_id):
                app, _ = progress_application()
                await progress(
                    app, command=StateChangedCommand(entity_id, old, new, NOW)
                )
                app.async_evaluate.assert_awaited_once()

    async def test_restart_keeps_observed_job_and_excludes_cross_restart_learning(self):
        app, _ = progress_application()
        await progress(app)
        recovered = recovery_application()
        recovered.state, _ = SchedulerState.from_store(
            app.storage._store.payload, ENTRY_DATA
        )
        recovered.hass.states.values["vacuum.alpha"] = SimpleNamespace(
            state="cleaning", last_changed=NOW
        )
        with patch(
            "custom_components.adaptive_robovacs.application.core._now",
            return_value=NOW + timedelta(seconds=2),
        ):
            await recovered._async_recover_active_jobs()
        job = recovered.state.active_jobs["registry-alpha"]
        self.assertEqual(job.phase, JobPhase.CLEANING)
        self.assertTrue(job.native_timer_progress_seen)
        self.assertTrue(job.recovery_crossed)
        self.assertFalse(job.forecast_sample_eligible)
        recovered._async_complete_job.assert_not_awaited()


class PreparedPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_validate_once_per_write_and_never_for_unchanged_save(self):
        app, _ = progress_application()
        validator = SchedulerState._validate_current_schema
        with patch.object(
            SchedulerState, "_validate_current_schema", wraps=validator
        ) as validate:
            await app._async_save()
            self.assertEqual(validate.call_count, 1)
            await app._async_save()
            self.assertEqual(validate.call_count, 1)
            await app._async_save(force=True)
            self.assertEqual(validate.call_count, 2)
        self.assertEqual(len(app.storage._store.saved), 2)
        self.assertIn("evaluation", app.storage._store.payload)

    async def test_invalid_payload_and_failed_write_do_not_advance_fingerprint(self):
        app, _ = progress_application()
        await app._async_save()
        fingerprint = app._last_durable_fingerprint
        app.state.room_settings["study"].expected_minutes = -1
        with self.assertRaises(ValueError):
            await app._async_save()
        self.assertIsNone(app._last_durable_fingerprint)
        self.assertEqual(len(app.storage._store.saved), 1)
        app.state.room_settings["study"].expected_minutes = 25
        original = app.storage._store.async_save
        with (
            patch.object(
                app.storage._store, "async_save", side_effect=OSError("disk failed")
            ),
            self.assertRaises(OSError),
        ):
            await app._async_save()
        self.assertIsNone(app._last_durable_fingerprint)
        app.storage._store.async_save = original
        await app._async_save()
        self.assertNotEqual(app._last_durable_fingerprint, fingerprint)

    async def test_persisted_payload_is_detached_from_later_mutation(self):
        app, _ = progress_application()
        await app._async_save()
        payload = app.storage._store.payload
        app.state.global_settings.party_mode = True
        self.assertFalse(payload["global"]["party_mode"])


class ForecastReuseTests(unittest.TestCase):
    def test_cache_is_bounded_by_inputs_and_discarded_after_each_pass(self):
        app = planning_application()
        room = app.discovery.rooms["study"]
        from custom_components.adaptive_robovacs.application import policy

        with patch.object(
            policy, "forecast_vacancy", wraps=policy.forecast_vacancy
        ) as forecast:
            with app._forecast_scope():
                first = app._forecast(room, NOW, 10)
                self.assertIs(first, app._forecast(room, NOW, 10))
                app._forecast(room, NOW, 11)
                app._forecast(room, NOW + timedelta(seconds=1), 10)
                self.assertEqual(forecast.call_count, 3)
            self.assertIsNone(app._planning_forecasts)
            app.state.room_history["study"].unoccupied_since = NOW
            with app._forecast_scope():
                self.assertFalse(app._forecast(room, NOW, 10).allowed)
                self.assertEqual(forecast.call_count, 4)
            self.assertTrue(first.allowed)

    def test_failed_planning_does_not_leak_a_cached_forecast(self):
        app = planning_application()
        with (
            self.assertRaisesRegex(RuntimeError, "planning failed"),
            app._forecast_scope(),
        ):
            app._forecast(app.discovery.rooms["study"], NOW, 10)
            raise RuntimeError("planning failed")
        self.assertIsNone(app._planning_forecasts)


class EntityPublicationTests(unittest.TestCase):
    def test_all_platforms_skip_identical_public_values(self):
        from custom_components.adaptive_robovacs import button, number, select, sensor
        from custom_components.adaptive_robovacs import switch as switches

        coordinator = _Coordinator()
        for platform in (button, number, select, sensor, switches):
            for entity in platform._entities(coordinator):
                with (
                    self.subTest(entity=entity.unique_id),
                    patch.object(
                        CoordinatorEntity, "_handle_coordinator_update"
                    ) as write,
                ):
                    entity._handle_coordinator_update()
                    entity._handle_coordinator_update()
                    self.assertEqual(write.call_count, 1)

    def test_equal_entity_values_skip_scope_writes_and_availability_is_preserved(self):
        coordinator = _Coordinator()
        entity = _RobotStatusSensor(coordinator, "vacuum.alpha")
        with patch.object(CoordinatorEntity, "_handle_coordinator_update") as write:
            entity._handle_coordinator_update()
            entity._handle_coordinator_update()
            self.assertEqual(write.call_count, 1)
            coordinator.data.robots[0].battery = 80
            entity._handle_coordinator_update()
            self.assertEqual(write.call_count, 2)
            coordinator.data.robots = ()
            entity._handle_coordinator_update()
            entity._handle_coordinator_update()
            self.assertEqual(write.call_count, 3)
