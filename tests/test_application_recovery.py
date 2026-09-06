"""Application-level restart, hold, and recovery behavior."""

from __future__ import annotations

import asyncio
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from custom_components.adaptive_robovacs.application import SchedulerApplication
from custom_components.adaptive_robovacs.commands import EvaluateCommand
from custom_components.adaptive_robovacs.models import (
    CleaningOperation,
    JobPhase,
    JobSource,
)
from custom_components.adaptive_robovacs.state import ActiveJob, RobotHold
from tests.test_application_state import NOW, active_job, state_application


def recovery_application() -> SchedulerApplication:
    app = state_application()
    app._recovery_timers = {}
    app._start_confirmation_timers = {}
    app._ready_confirmation_timers = {}
    app._water_confirmation_timers = {}
    app._async_save = AsyncMock()
    app._cancel_start_confirmation = Mock()
    app._async_complete_job = AsyncMock()
    app._async_latch_scheduler_fault = AsyncMock()
    app._mark_observed_completion = Mock()
    app._set_dock_completion_pending = Mock()
    app._schedule_recovery_completion = Mock()
    app._cancel_recovery_timer = Mock()
    app._cancel_job = Mock()
    app._apply_robot_cancellation_deferral = Mock(return_value=[])
    app._hold_active_job = Mock()
    app._resume_held_job = Mock()
    app._set_held_job_phase = Mock()
    app._mop_washing_is_observed = Mock(return_value=False)
    app._mark_mop_washing_started = Mock()
    app._terminal_completion_is_observed = Mock(return_value=False)
    return app


def tracked_job(
    *,
    phase: JobPhase = JobPhase.ACCEPTED,
    seen_cleaning: bool = False,
) -> ActiveJob:
    job = active_job()
    job.phase = phase
    job.seen_cleaning = seen_cleaning
    job.expected_minutes = 30
    job.last_observed_at = NOW - timedelta(minutes=5)
    return job


class RecoveryHelperTests(unittest.IsolatedAsyncioTestCase):
    def test_normalize_backfills_manual_and_scheduler_lifecycle_fields(self) -> None:
        app = state_application()
        second = app.discovery.rooms["study"]
        app.discovery = type(app.discovery)(
            app.discovery.robots,
            {
                "study": second,
                "hall": second.__class__("hall", "Hall", "ground", frozenset()),
            },
        )
        app.state.ensure_room("hall", is_bedroom=False)[0].expected_minutes = 20
        app.state.room_settings["study"].expected_minutes = 10
        manual = ActiveJob(
            room_id="",
            room_ids=["study", "hall"],
            operation=CleaningOperation.VACUUM,
            phase=JobPhase.CLEANING,
            source=JobSource.MANUAL_HOME_ASSISTANT,
            started_at=NOW - timedelta(minutes=2),
        )
        app._normalise_active_job(manual, NOW)
        self.assertEqual(manual.room_id, "study")
        self.assertEqual(manual.expected_minutes, 30)
        self.assertEqual(manual.last_observed_at, NOW - timedelta(minutes=2))
        self.assertEqual(manual.expected_end, NOW + timedelta(minutes=28))

        scheduled = active_job()
        scheduled.room_ids = []
        scheduled.started_at = None
        app._normalise_active_job(scheduled, NOW)
        self.assertEqual(scheduled.room_ids, ["study"])
        self.assertEqual(scheduled.expected_minutes, 10)
        self.assertEqual(scheduled.last_observed_at, NOW)

    def test_hold_reconciliation_is_driven_by_physical_state(self) -> None:
        app = state_application()
        app._recovery_timers = {}
        active = tracked_job(seen_cleaning=True)
        manual_hold = RobotHold("map_recovery_pending", "held", held_at=NOW)
        app.state.robot_holds["registry-alpha"] = manual_hold
        self.assertEqual(
            app._reconcile_robot_hold(
                "registry-alpha", "docked", active, NOW + timedelta(minutes=1)
            ),
            "held",
        )
        self.assertEqual(manual_hold.last_observed_at, NOW + timedelta(minutes=1))

        app.state.robot_holds.clear()
        self.assertEqual(
            app._reconcile_robot_hold("registry-alpha", "paused", active, NOW),
            "held",
        )
        self.assertEqual(app.state.robot_holds["registry-alpha"].reason, "paused")
        self.assertEqual(
            app._reconcile_robot_hold("registry-alpha", "error", active, NOW),
            "held",
        )
        self.assertEqual(app.state.robot_holds["registry-alpha"].reason, "robot_error")

        app.state.robot_holds.clear()
        self.assertIsNone(
            app._reconcile_robot_hold("registry-alpha", "cleaning", active, NOW)
        )
        hold = RobotHold("user_requested_return", "cancelling", held_at=NOW)
        app.state.robot_holds["registry-alpha"] = hold
        self.assertEqual(
            app._reconcile_robot_hold("registry-alpha", "returning", active, NOW),
            "cancelling",
        )

        hold.reason = "paused"
        hold.phase = "held"
        self.assertEqual(
            app._reconcile_robot_hold("registry-alpha", "cleaning", active, NOW),
            "resumed",
        )
        self.assertNotIn("registry-alpha", app.state.robot_holds)

    def test_held_job_helpers_preserve_interruption_and_completion_semantics(
        self,
    ) -> None:
        app = state_application()
        app._recovery_timers = {"vacuum.alpha": Mock()}
        app._start_confirmation_timers = {}
        job = tracked_job(phase=JobPhase.CLEANING, seen_cleaning=True)
        self.assertTrue(app._hold_active_job("vacuum.alpha", job, "paused", NOW))
        self.assertEqual(job.phase, JobPhase.PAUSED)
        self.assertTrue(job.interrupted)
        self.assertFalse(app._hold_active_job("vacuum.alpha", job, "paused", NOW))

        job.cleaning_finished_at = NOW
        self.assertTrue(app._hold_active_job("vacuum.alpha", job, "error", NOW))
        self.assertEqual(job.phase, JobPhase.COMPLETION_HELD)
        self.assertTrue(job.completion_before_hold)

        app._set_held_job_phase("vacuum.alpha", job, "cancelling", NOW)
        self.assertEqual(job.phase, JobPhase.CANCELLING)
        self.assertEqual(job.hold_reason, "physical_cancellation")
        app._set_held_job_phase("vacuum.alpha", job, "completion_pending", NOW)
        self.assertEqual(job.phase, JobPhase.COMPLETION_HELD)
        self.assertEqual(job.hold_reason, "completion_before_fault")

    def test_resume_rebases_expected_end_and_native_timer(self) -> None:
        app = state_application()
        app._recovery_timers = {}
        app._start_confirmation_timers = {}
        app._cleaning_timer_minutes = Mock(return_value=0.5)
        job = tracked_job(phase=JobPhase.PAUSED)
        job.seen_cleaning = False
        job.interruption_started_at = NOW - timedelta(minutes=4)
        state = SimpleNamespace(last_changed=NOW - timedelta(seconds=10))

        app._resume_held_job("vacuum.alpha", job, state, NOW)

        self.assertEqual(job.phase, JobPhase.CLEANING)
        self.assertTrue(job.seen_cleaning)
        self.assertEqual(job.interruption_minutes, 4)
        self.assertEqual(job.observed_started_at, state.last_changed)
        self.assertEqual(job.timer_start, 0.5)
        self.assertEqual(job.expected_end, state.last_changed + timedelta(minutes=30))

    async def test_cancellation_and_recovery_timer_helpers_are_scoped(self) -> None:
        app = state_application()
        app._recovery_timers = {}
        app.async_execute = AsyncMock(return_value={})
        created = []

        def create_task(coro, *, name=None):
            task = asyncio.create_task(coro, name=name)
            created.append(task)
            return task

        app._async_create_task = create_task
        self.assertEqual(
            app._apply_robot_cancellation_deferral("vacuum.alpha", NOW), []
        )
        self.assertIn("registry-alpha", app.state.robot_cooldowns)
        self.assertEqual(app._apply_robot_cancellation_deferral("missing", NOW), [])

        old = Mock()
        app._recovery_timers["vacuum.alpha"] = old
        with patch(
            "custom_components.adaptive_robovacs.application._now", return_value=NOW
        ):
            app._schedule_recovery_completion(
                "vacuum.alpha", NOW - timedelta(seconds=1)
            )
        await asyncio.gather(*created)
        old.assert_called_once()
        immediate = app.async_execute.await_args.args[0]
        self.assertIsInstance(immediate, EvaluateCommand)
        self.assertEqual(immediate.detail, "recovery-end:vacuum.alpha")

        created.clear()
        callback_holder = {}

        def track(_hass, callback, when):
            callback_holder["callback"] = callback
            callback_holder["when"] = when
            return Mock()

        with (
            patch(
                "custom_components.adaptive_robovacs.application._now",
                return_value=NOW,
            ),
            patch(
                "custom_components.adaptive_robovacs.application."
                "async_track_point_in_utc_time",
                side_effect=track,
            ),
        ):
            app._schedule_recovery_completion(
                "vacuum.alpha", NOW + timedelta(minutes=5)
            )
        self.assertEqual(callback_holder["when"], NOW + timedelta(minutes=5))
        callback_holder["callback"](callback_holder["when"])
        await asyncio.gather(*created)
        self.assertNotIn("vacuum.alpha", app._recovery_timers)

        created.clear()
        app._on_home_assistant_started(SimpleNamespace())
        await asyncio.gather(*created)
        command = app.async_execute.await_args.args[0]
        self.assertIsInstance(command, EvaluateCommand)
        self.assertTrue(command.coalesce)


class ActiveJobRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_uses_observed_cleaning_and_returning_states(self) -> None:
        for state_text, expected_phase in (
            ("cleaning", JobPhase.CLEANING),
            ("returning", JobPhase.RETURNING),
        ):
            with self.subTest(state=state_text):
                app = recovery_application()
                job = tracked_job()
                app.state.active_jobs["registry-alpha"] = job
                app.hass.states.values["vacuum.alpha"] = SimpleNamespace(
                    state=state_text, last_changed=NOW
                )
                with patch(
                    "custom_components.adaptive_robovacs.application._now",
                    return_value=NOW,
                ):
                    await app._async_recover_active_jobs()
                self.assertEqual(job.phase, expected_phase)
                self.assertEqual(job.recovered_at, NOW)
                self.assertEqual(job.seen_cleaning, state_text == "returning")
                app._async_save.assert_awaited_once()

    async def test_recovery_holds_mop_wash_and_uncertain_dispatch(self) -> None:
        app = recovery_application()
        job = tracked_job()
        app.state.active_jobs["registry-alpha"] = job
        app._mop_washing_is_observed.return_value = True
        with patch(
            "custom_components.adaptive_robovacs.application._now", return_value=NOW
        ):
            await app._async_recover_active_jobs()
        app._mark_mop_washing_started.assert_called_once()
        app._async_latch_scheduler_fault.assert_not_awaited()

        app = recovery_application()
        job = tracked_job(phase=JobPhase.MOP_WASHING)
        app.state.active_jobs["registry-alpha"] = job
        app.hass.states.values["vacuum.alpha"] = SimpleNamespace(
            state="idle", last_changed=NOW
        )
        with patch(
            "custom_components.adaptive_robovacs.application._now", return_value=NOW
        ):
            await app._async_recover_active_jobs()
        app._async_latch_scheduler_fault.assert_not_awaited()

        app = recovery_application()
        job = tracked_job(phase=JobPhase.ACCEPTED)
        app.state.active_jobs["registry-alpha"] = job
        app.hass.states.values["vacuum.alpha"] = SimpleNamespace(
            state="docked", last_changed=NOW
        )
        with patch(
            "custom_components.adaptive_robovacs.application._now", return_value=NOW
        ):
            await app._async_recover_active_jobs()
        app._async_latch_scheduler_fault.assert_awaited_once()

    async def test_recovery_finishes_pending_and_terminal_docked_jobs(self) -> None:
        app = recovery_application()
        job = tracked_job(phase=JobPhase.COMPLETION_PENDING, seen_cleaning=True)
        job.cleaning_finished_at = NOW - timedelta(minutes=1)
        job.completion_confidence = "terminal"
        app.state.active_jobs["registry-alpha"] = job
        app.state.robot_holds["registry-alpha"] = RobotHold("paused", "held")
        app.hass.states.values["vacuum.alpha"] = SimpleNamespace(
            state="docked", last_changed=NOW
        )
        with patch(
            "custom_components.adaptive_robovacs.application._now", return_value=NOW
        ):
            await app._async_recover_active_jobs()
        app._async_complete_job.assert_awaited_once_with(
            "vacuum.alpha", job, job.cleaning_finished_at, "terminal"
        )
        self.assertNotIn("registry-alpha", app.state.robot_holds)

        app = recovery_application()
        job = tracked_job(seen_cleaning=True)
        app.state.active_jobs["registry-alpha"] = job
        app.hass.states.values["vacuum.alpha"] = SimpleNamespace(
            state="docked", last_changed=NOW
        )
        app._terminal_completion_is_observed.return_value = True
        with patch(
            "custom_components.adaptive_robovacs.application._now", return_value=NOW
        ):
            await app._async_recover_active_jobs()
        app._mark_observed_completion.assert_called_once()
        app._async_complete_job.assert_awaited_once()

        app = recovery_application()
        job = tracked_job(seen_cleaning=True)
        app.state.active_jobs["registry-alpha"] = job
        app.hass.states.values["vacuum.alpha"] = SimpleNamespace(
            state="docked", last_changed=NOW
        )
        with patch(
            "custom_components.adaptive_robovacs.application._now", return_value=NOW
        ):
            await app._async_recover_active_jobs()
        app._set_dock_completion_pending.assert_called_once_with(
            "vacuum.alpha", job, NOW
        )

    async def test_recovery_waits_for_idle_or_unavailable_physical_evidence(
        self,
    ) -> None:
        for state in (None, "unavailable", "unknown", "idle"):
            with self.subTest(state=state):
                app = recovery_application()
                job = tracked_job(seen_cleaning=True)
                app.state.active_jobs["registry-alpha"] = job
                if state is not None:
                    app.hass.states.values["vacuum.alpha"] = SimpleNamespace(
                        state=state, last_changed=NOW
                    )
                app._set_recovery_waiting = Mock()
                with patch(
                    "custom_components.adaptive_robovacs.application._now",
                    return_value=NOW,
                ):
                    await app._async_recover_active_jobs()
                app._set_recovery_waiting.assert_called_once_with(
                    "vacuum.alpha", job, NOW
                )

    async def test_recovery_applies_each_held_physical_transition(self) -> None:
        cases = (
            "held",
            "resumed",
            "cancelling",
            "completion_pending",
            "cancelled",
            "complete",
        )
        for action in cases:
            with self.subTest(action=action):
                app = recovery_application()
                job = tracked_job(seen_cleaning=True)
                if action == "complete":
                    job.cleaning_finished_at = NOW - timedelta(minutes=1)
                app.state.active_jobs["registry-alpha"] = job
                app.state.robot_holds["registry-alpha"] = RobotHold(
                    "paused", "held", returning_at=NOW - timedelta(seconds=30)
                )
                app._reconcile_robot_hold = Mock(return_value=action)
                app.hass.states.values["vacuum.alpha"] = SimpleNamespace(
                    state="docked", last_changed=NOW
                )
                with (
                    patch(
                        "custom_components.adaptive_robovacs.application._now",
                        return_value=NOW,
                    ),
                    patch(
                        "custom_components.adaptive_robovacs.application_recovery."
                        "offline_held_recovery_outcome",
                        return_value="continue",
                    ),
                ):
                    await app._async_recover_active_jobs()
                if action == "held":
                    app._hold_active_job.assert_called_once()
                elif action == "resumed":
                    app._resume_held_job.assert_called_once()
                elif action in {"cancelling", "completion_pending"}:
                    app._set_held_job_phase.assert_called_once()
                elif action == "cancelled":
                    app._cancel_job.assert_called_once()
                    app._apply_robot_cancellation_deferral.assert_called_once()
                else:
                    app._async_complete_job.assert_awaited_once()

    async def test_offline_hold_cancellation_wins_over_transition(self) -> None:
        app = recovery_application()
        job = tracked_job(seen_cleaning=True)
        app.state.active_jobs["registry-alpha"] = job
        app.state.robot_holds["registry-alpha"] = RobotHold("paused", "held")
        app._reconcile_robot_hold = Mock(return_value="held")
        with (
            patch(
                "custom_components.adaptive_robovacs.application._now",
                return_value=NOW,
            ),
            patch(
                "custom_components.adaptive_robovacs.application_recovery."
                "offline_held_recovery_outcome",
                return_value="cancelled",
            ),
        ):
            await app._async_recover_active_jobs()
        app._cancel_job.assert_called_once_with(
            "vacuum.alpha", job, NOW, "recovered_physical_cancellation"
        )
        self.assertNotIn("registry-alpha", app.state.robot_holds)

    async def test_unconfirmed_checkpoint_is_cleared_and_audited(self) -> None:
        app = recovery_application()
        job = tracked_job(phase=JobPhase.CLEANING, seen_cleaning=False)
        app.state.active_jobs["registry-alpha"] = job
        app.hass.states.values["vacuum.alpha"] = SimpleNamespace(
            state="docked", last_changed=NOW
        )
        with patch(
            "custom_components.adaptive_robovacs.application._now", return_value=NOW
        ):
            await app._async_recover_active_jobs()
        self.assertIsNone(app.state.active_jobs["registry-alpha"])
        self.assertEqual(
            app.state.audit.recovery_events[-1].reason, "unconfirmed checkpoint"
        )


class ActiveJobReconciliationTests(unittest.IsolatedAsyncioTestCase):
    """Exercise live-state reconciliation after startup recovery has settled."""

    async def test_mop_washing_and_failed_start_are_fail_closed(self) -> None:
        app = recovery_application()
        job = tracked_job()
        app.state.active_jobs["registry-alpha"] = job
        app.hass.states.values["vacuum.alpha"] = SimpleNamespace(
            state="washing_the_mop", last_changed=NOW
        )
        app._mop_washing_is_observed.return_value = True
        app._mark_mop_washing_started.return_value = True
        await app._async_reconcile_jobs(NOW)
        app._mark_mop_washing_started.assert_called_once_with(
            app.discovery.robots["vacuum.alpha"], job, NOW
        )
        app._async_save.assert_awaited_once()

        app = recovery_application()
        job = tracked_job()
        job.q10_max_plus_fallback = True
        app.state.active_jobs["registry-alpha"] = job
        app.hass.states.values["vacuum.alpha"] = SimpleNamespace(
            state="paused", last_changed=NOW
        )
        app._async_downgrade_q10_max_plus = AsyncMock()
        await app._async_reconcile_jobs(NOW)
        app._async_downgrade_q10_max_plus.assert_awaited_once()
        app._async_latch_scheduler_fault.assert_awaited_once()
        self.assertEqual(
            app._async_latch_scheduler_fault.await_args.args[2],
            "start_outcome_uncertain",
        )

        app = recovery_application()
        job = tracked_job()
        job.accepted_at = NOW - timedelta(minutes=10)
        app.state.active_jobs["registry-alpha"] = job
        app.hass.states.values["vacuum.alpha"] = SimpleNamespace(
            state="docked", last_changed=NOW
        )
        await app._async_reconcile_jobs(NOW)
        self.assertEqual(
            app._async_latch_scheduler_fault.await_args.args[2],
            "start_confirmation_failed",
        )

    async def test_pending_completion_and_each_hold_outcome(self) -> None:
        app = recovery_application()
        job = tracked_job(phase=JobPhase.COMPLETION_PENDING, seen_cleaning=True)
        app.state.active_jobs["registry-alpha"] = job
        app.state.robot_holds["registry-alpha"] = RobotHold("paused", "held")
        app.hass.states.values["vacuum.alpha"] = SimpleNamespace(
            state="docked", last_changed=NOW - timedelta(seconds=5)
        )
        await app._async_reconcile_jobs(NOW)
        app._mark_observed_completion.assert_called_once()
        app._async_complete_job.assert_awaited_once()
        self.assertNotIn("registry-alpha", app.state.robot_holds)

        for action in (
            "held",
            "resumed",
            "cancelling",
            "completion_pending",
            "cancelled",
            "complete",
        ):
            with self.subTest(action=action):
                app = recovery_application()
                job = tracked_job(seen_cleaning=True)
                if action == "complete":
                    job.cleaning_finished_at = NOW - timedelta(seconds=30)
                app.state.active_jobs["registry-alpha"] = job
                app.state.robot_holds["registry-alpha"] = RobotHold(
                    "paused",
                    "held",
                    returning_at=NOW - timedelta(seconds=10),
                )
                app._reconcile_robot_hold = Mock(return_value=action)
                app.hass.states.values["vacuum.alpha"] = SimpleNamespace(
                    state="idle", last_changed=NOW
                )
                await app._async_reconcile_jobs(NOW)
                if action == "held":
                    app._hold_active_job.assert_called_once()
                elif action == "resumed":
                    app._resume_held_job.assert_called_once()
                elif action in {"cancelling", "completion_pending"}:
                    app._set_held_job_phase.assert_called_once()
                elif action == "cancelled":
                    app._cancel_job.assert_called_once_with(
                        "vacuum.alpha",
                        job,
                        NOW - timedelta(seconds=10),
                        "physical_cancelled",
                    )
                    app._apply_robot_cancellation_deferral.assert_called_once()
                else:
                    app._async_complete_job.assert_awaited_once()

    async def test_cleaning_returning_and_docked_paths_follow_observation(self) -> None:
        app = recovery_application()
        job = tracked_job()
        app.state.active_jobs["registry-alpha"] = job
        app.state.robot_faults["registry-alpha"] = SimpleNamespace(
            room_area_id="study", robot_registry_id="registry-alpha"
        )
        app._discard_unconfirmed_scheduler_job = Mock()
        app.hass.states.values["vacuum.alpha"] = SimpleNamespace(
            state="cleaning", last_changed=NOW
        )
        with patch(
            "custom_components.adaptive_robovacs.application_jobs."
            "should_assume_native_app_clean",
            return_value=True,
        ):
            await app._async_reconcile_jobs(NOW)
        app._discard_unconfirmed_scheduler_job.assert_called_once()

        app = recovery_application()
        job = tracked_job()
        app.state.active_jobs["registry-alpha"] = job
        app.hass.states.values["vacuum.alpha"] = SimpleNamespace(
            state="cleaning", last_changed=NOW - timedelta(minutes=1)
        )
        await app._async_reconcile_jobs(NOW)
        app._resume_held_job.assert_called_once()

        app = recovery_application()
        job = tracked_job(seen_cleaning=True)
        app.state.active_jobs["registry-alpha"] = job
        app._recovery_timers["vacuum.alpha"] = Mock()
        app.hass.states.values["vacuum.alpha"] = SimpleNamespace(
            state="returning", last_changed=NOW
        )
        await app._async_reconcile_jobs(NOW)
        self.assertEqual(job.phase, JobPhase.RETURNING)
        app._cancel_recovery_timer.assert_called_once_with("vacuum.alpha")

        for terminal, expired, expected in (
            (True, False, "complete"),
            (False, True, "complete"),
            (False, False, "pending"),
        ):
            with self.subTest(terminal=terminal, expired=expired):
                app = recovery_application()
                job = tracked_job(seen_cleaning=True)
                job.recovery_crossed = terminal
                app.state.active_jobs["registry-alpha"] = job
                app.hass.states.values["vacuum.alpha"] = SimpleNamespace(
                    state="docked", last_changed=NOW - timedelta(minutes=2)
                )
                app._terminal_completion_is_observed.return_value = terminal
                deadline = NOW - timedelta(seconds=1)
                if not expired:
                    deadline = NOW + timedelta(minutes=2)
                app._dock_completion_deadline = Mock(return_value=deadline)
                await app._async_reconcile_jobs(NOW)
                if expected == "complete":
                    app._mark_observed_completion.assert_called_once()
                    app._async_complete_job.assert_awaited_once()
                else:
                    app._set_dock_completion_pending.assert_called_once()

    async def test_stale_manual_checkpoint_is_audited_but_scheduler_waits(self) -> None:
        for source, cleared in (
            (JobSource.MANUAL_HOME_ASSISTANT, True),
            (JobSource.SCHEDULER, False),
        ):
            with self.subTest(source=source):
                app = recovery_application()
                job = tracked_job()
                job.source = source
                job.started_at = NOW - timedelta(minutes=11)
                job.accepted_at = None
                app.state.active_jobs["registry-alpha"] = job
                app.hass.states.values["vacuum.alpha"] = SimpleNamespace(
                    state="idle", last_changed=NOW
                )
                await app._async_reconcile_jobs(NOW)
                if cleared:
                    self.assertIsNone(app.state.active_jobs["registry-alpha"])
                    self.assertEqual(
                        app.state.audit.manual_events[-1].outcome,
                        "not_started_or_cancelled",
                    )
                else:
                    self.assertIs(app.state.active_jobs["registry-alpha"], job)


class JobCompletionApplicationTests(unittest.IsolatedAsyncioTestCase):
    def test_native_timer_and_elapsed_measurement_are_normalized(self) -> None:
        app = state_application()
        entity_id = "sensor.alpha_cleaning_time"
        self.assertIsNone(app._cleaning_timer_minutes("vacuum.alpha"))
        app.hass.states.values[entity_id] = SimpleNamespace(
            state="invalid", attributes={}
        )
        self.assertIsNone(app._cleaning_timer_minutes("vacuum.alpha"))

        for value, unit, expected in (
            ("2", "h", 120.0),
            ("120", "seconds", 2.0),
            ("7.5", "min", 7.5),
        ):
            with self.subTest(unit=unit):
                app.hass.states.values[entity_id] = SimpleNamespace(
                    state=value, attributes={"unit_of_measurement": unit}
                )
                self.assertEqual(app._cleaning_timer_minutes("vacuum.alpha"), expected)

        job = tracked_job(seen_cleaning=True)
        job.timer_start = 5
        app.hass.states.values[entity_id] = SimpleNamespace(state="12", attributes={})
        self.assertEqual(app._native_timer_elapsed_minutes("vacuum.alpha", job), 7)
        job.timer_start = 20
        self.assertIsNone(app._native_timer_elapsed_minutes("vacuum.alpha", job))

        job.observed_started_at = NOW - timedelta(minutes=12)
        job.cleaning_finished_at = NOW
        job.interruption_minutes = 2
        self.assertEqual(app._measured_duration_minutes(job), 10)

    def test_observed_completion_learns_only_from_eligible_normal_runs(self) -> None:
        app = state_application()
        app._native_timer_elapsed_minutes = Mock(return_value=8.0)
        app._measured_duration_minutes = Mock(return_value=9.0)
        job = tracked_job(seen_cleaning=True)
        job.forecast_sample_eligible = True
        app._mark_observed_completion("vacuum.alpha", job, NOW)
        self.assertEqual(job.cleaning_finished_at, NOW)
        self.assertEqual(job.native_timer_elapsed, 8.0)
        self.assertEqual(job.measured_minutes, 9.0)
        self.assertEqual(job.duration_source, "elapsed_total_v2")

        recovered = tracked_job(seen_cleaning=True)
        recovered.recovery_crossed = True
        app._mark_observed_completion("vacuum.alpha", recovered, NOW)
        self.assertIsNone(recovered.measured_minutes)
        app._mark_observed_completion(
            "vacuum.alpha", recovered, NOW, "inferred", allow_sample=False
        )
        self.assertEqual(recovered.completion_confidence, "inferred")

    def test_cancel_and_complete_apply_reducer_effects(self) -> None:
        app = state_application()
        app._recovery_timers = {}
        app._start_confirmation_timers = {}

        cancelled = tracked_job()
        cancelled.source = JobSource.MANUAL_DASHBOARD
        cancelled.manual_context_id = "cancel-context"
        cancelled.manual_mode = "vacuum_only"
        app.state.active_jobs["registry-alpha"] = cancelled
        app._cancel_job("vacuum.alpha", cancelled, NOW, "user_requested")
        self.assertIsNone(app.state.active_jobs["registry-alpha"])
        self.assertEqual(app.state.audit.manual_events[-1].outcome, "cancelled")
        self.assertEqual(app.state.audit.recovery_events[-1].reason, "user_requested")

        completed = tracked_job(seen_cleaning=True)
        completed.source = JobSource.SCHEDULER
        completed.operation = CleaningOperation.MOP
        completed.measured_minutes = 12
        completed.duration_source = "elapsed_total_v2"
        completed.forecast_sample_eligible = True
        app.state.active_jobs["registry-alpha"] = completed
        app._complete_job("vacuum.alpha", completed, NOW, "observed")
        history = app.state.room_history["study"]
        self.assertEqual(history.mop_completed_at, NOW)
        self.assertEqual(history.cleaning_completed_at, NOW)
        self.assertEqual(history.duration_samples[-1].minutes, 12)
        self.assertIsNone(app.state.active_jobs["registry-alpha"])

    async def test_zero_native_duration_latches_or_cancels_existing_fault(self) -> None:
        for already_faulted in (False, True):
            with self.subTest(already_faulted=already_faulted):
                app = state_application()
                app._async_latch_scheduler_fault = AsyncMock()
                app._cancel_job = Mock()
                app._complete_job = Mock()
                job = tracked_job(seen_cleaning=True)
                job.duration_source = "robot_timer"
                job.measured_minutes = 0
                if already_faulted:
                    app.state.robot_faults["registry-alpha"] = SimpleNamespace()
                completed = await app._async_complete_job(
                    "vacuum.alpha", job, NOW, "observed"
                )
                self.assertFalse(completed)
                self.assertEqual(
                    app.state.room_history["study"].last_stage_reason,
                    "native_cleaning_zero_duration",
                )
                if already_faulted:
                    app._cancel_job.assert_called_once()
                    app._async_latch_scheduler_fault.assert_not_awaited()
                else:
                    app._async_latch_scheduler_fault.assert_awaited_once()
                app._complete_job.assert_not_called()

        app = state_application()
        app._complete_job = Mock()
        job = tracked_job(seen_cleaning=True)
        self.assertTrue(
            await app._async_complete_job("vacuum.alpha", job, NOW, "observed")
        )
        app._complete_job.assert_called_once()

    def test_manual_deferrals_ignore_unknown_robot_and_missing_rooms(self) -> None:
        app = state_application()
        self.assertEqual(
            app._apply_manual_deferral(
                "vacuum.missing", ["study"], [CleaningOperation.VACUUM], NOW
            ),
            [],
        )
        app._room_due = Mock(return_value=NOW + timedelta(hours=12))
        app._set_room_deferral = Mock()
        result = app._apply_manual_deferral(
            "vacuum.alpha",
            ["study", "missing"],
            [CleaningOperation.VACUUM],
            NOW,
        )
        self.assertEqual(result, ["study:vacuum"])
        self.assertEqual(app._set_room_deferral.call_count, 2)


if __name__ == "__main__":
    unittest.main()
