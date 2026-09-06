"""Application transaction tests around occurrence preparation and dispatch."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from custom_components.adaptive_robovacs.application import SchedulerApplication
from custom_components.adaptive_robovacs.models import (
    AdapterDispatchResult,
    CleaningOperation,
    DispatchOutcome,
    JobPhase,
    StageStatus,
    WaterReadiness,
)
from custom_components.adaptive_robovacs.state import CleaningStage
from tests.test_application_planning import occurrence, planning_application
from tests.test_application_state import NOW, active_job


def transaction_application() -> SchedulerApplication:
    app = planning_application()
    app.dispatch = SimpleNamespace(
        async_validate_profile=AsyncMock(
            return_value=AdapterDispatchResult(
                DispatchOutcome.READY, "ready", "profile ready"
            )
        ),
        async_dispatch=AsyncMock(return_value=(True, "started")),
    )
    app._async_save = AsyncMock()
    app._async_send_mobile_notification = AsyncMock(return_value=(1, 1))
    app._schedule_water_confirmation = Mock()
    app._schedule_start_confirmation = Mock()
    app._notify_listeners = Mock()
    app.async_execute = AsyncMock(return_value={})

    def close_follow_up(coro, *, name=None):
        del name
        coro.close()
        return None

    app._async_create_task = close_follow_up
    return app


def resolved_candidate(
    app: SchedulerApplication,
    operation: CleaningOperation = CleaningOperation.VACUUM,
):
    mode = "mop_only" if operation is CleaningOperation.MOP else "vacuum_only"
    candidate = app._manual_candidate(
        app.discovery.rooms["study"], NOW, mode, "context", "user"
    )
    resolved, reason = app._resolve_candidate_for_robot(
        candidate, app.discovery.robots["vacuum.alpha"]
    )
    if resolved is None:
        raise AssertionError(reason)
    return resolved


def with_water(app: SchedulerApplication, water: WaterReadiness):
    original = app.discovery.robots["vacuum.alpha"]
    return replace(
        original,
        adapter_capabilities=replace(
            original.adapter_capabilities,
            water_readiness=water,
        ),
    )


class PendingProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_refresh_is_limited_to_pending_docked_occurrences(
        self,
    ) -> None:
        app = transaction_application()
        candidate = resolved_candidate(app)
        candidate = replace(candidate, occurrence=None)
        robot = app.discovery.robots["vacuum.alpha"]
        blocked = AdapterDispatchResult(
            DispatchOutcome.BLOCKED,
            "profile_option_unsupported",
            "stored option disappeared",
        )
        app.dispatch.async_validate_profile.return_value = blocked
        self.assertIs(
            await app._async_refresh_pending_profile_if_needed(robot, candidate),
            candidate,
        )

        active_occurrence = occurrence(profile=resolved_candidate(app).resolved_profile)
        app.state.occurrences["study"] = active_occurrence
        candidate = replace(
            resolved_candidate(app),
            occurrence=active_occurrence,
            occurrence_id=active_occurrence.occurrence_id,
            stage_index=0,
        )
        prior = active_occurrence.stages[0].cleaning_profile
        app._candidate_for_robot = Mock(return_value=None)
        self.assertIs(
            await app._async_refresh_pending_profile_if_needed(robot, candidate),
            candidate,
        )
        self.assertIs(active_occurrence.stages[0].cleaning_profile, prior)

        refreshed = replace(candidate, reason="profile refreshed")
        app._candidate_for_robot.return_value = refreshed
        result = await app._async_refresh_pending_profile_if_needed(robot, candidate)
        self.assertIs(result, refreshed)
        self.assertIs(
            active_occurrence.stages[0].cleaning_profile,
            refreshed.resolved_profile,
        )
        app._async_save.assert_awaited_once()

        app.dispatch.async_validate_profile.side_effect = RuntimeError("private")
        self.assertIs(
            await app._async_refresh_pending_profile_if_needed(robot, candidate),
            candidate,
        )

    async def test_ready_or_non_profile_validation_never_rewrites_history(self) -> None:
        app = transaction_application()
        robot = app.discovery.robots["vacuum.alpha"]
        candidate = resolved_candidate(app)
        for result in (
            AdapterDispatchResult(DispatchOutcome.READY, "ready", "ready"),
            AdapterDispatchResult(
                DispatchOutcome.BLOCKED, "mapping_missing", "mapping"
            ),
        ):
            app.dispatch.async_validate_profile.return_value = result
            self.assertIs(
                await app._async_refresh_pending_profile_if_needed(robot, candidate),
                candidate,
            )
        app._async_save.assert_not_awaited()


class OccurrenceStageTests(unittest.TestCase):
    def test_stage_skip_updates_summary_cadence_and_manual_audit(self) -> None:
        app = transaction_application()
        active_occurrence = occurrence(operation=CleaningOperation.MOP)
        active_occurrence.stages.insert(
            0,
            CleaningStage(
                CleaningOperation.VACUUM,
                1,
                status=StageStatus.COMPLETED,
            ),
        )
        active_occurrence.current_stage = 1
        app.state.occurrences["study"] = active_occurrence
        self.assertTrue(
            app._skip_occurrence_stage(
                "study", 1, StageStatus.SKIPPED_NO_WATER, "water_low", NOW
            )
        )
        history = app.state.room_history["study"]
        self.assertEqual(
            history.last_stage_summary, "vacuum completed; mop skipped for water"
        )
        self.assertEqual(history.cleaning_completed_at, NOW)
        self.assertNotIn("study", app.state.occurrences)

        manual = occurrence(
            operation=CleaningOperation.MOP,
            manual_override=True,
        )
        manual.manual_context_id = "ctx"
        manual.manual_mode = "mop_only"
        app.state.occurrences["study"] = manual
        history.cleaning_completed_at = None
        self.assertTrue(
            app._skip_occurrence_stage(
                "study",
                0,
                StageStatus.SKIPPED_UNCONFIRMED_WATER,
                "declined",
                NOW,
            )
        )
        self.assertIsNone(history.cleaning_completed_at)
        self.assertEqual(
            app.state.audit.manual_events[-1].outcome, "skipped_unconfirmed_water"
        )
        self.assertEqual(history.last_stage_summary, "mop skipped unconfirmed water")

        self.assertFalse(
            app._skip_occurrence_stage(
                "missing", 0, StageStatus.SKIPPED_NO_WATER, "none", NOW
            )
        )


class WaterPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_mop_and_ready_mop_prepare_without_confirmation(self) -> None:
        app = transaction_application()
        robot = app.discovery.robots["vacuum.alpha"]
        candidate = resolved_candidate(app)
        prepared, message = await app._async_prepare_occurrence(robot, candidate, NOW)
        self.assertIsNotNone(prepared.occurrence)
        self.assertIsNone(message)
        self.assertIn("study", app.state.occurrences)
        app._async_save.assert_awaited_once()

        app = transaction_application()
        app.state.water_notification_episodes["study"] = SimpleNamespace()
        ready_robot = with_water(
            app,
            WaterReadiness("sensor_ready", "ready", ready=True, authoritative=True),
        )
        candidate = resolved_candidate(app, CleaningOperation.MOP)
        prepared, message = await app._async_prepare_occurrence(
            ready_robot, candidate, NOW
        )
        self.assertIsNotNone(prepared)
        self.assertIsNone(message)
        self.assertNotIn("study", app.state.water_notification_episodes)

    async def test_scheduled_revalidation_and_unsupported_water_paths(self) -> None:
        app = transaction_application()
        recheck_robot = with_water(
            app,
            WaterReadiness(
                "sensor_blocked",
                "tank_not_attached",
                revalidation_eligible=True,
            ),
        )
        candidate = resolved_candidate(app, CleaningOperation.MOP)
        candidate = replace(candidate, source="scheduler", manual_override=False)
        prepared, message = await app._async_prepare_occurrence(
            recheck_robot, candidate, NOW
        )
        self.assertTrue(prepared.ignore_water_readiness)
        self.assertIsNone(message)

        app = transaction_application()
        unsupported = with_water(
            app, WaterReadiness("unsupported", "mopping_unsupported")
        )
        candidate = resolved_candidate(app, CleaningOperation.MOP)
        prepared, message = await app._async_prepare_occurrence(
            unsupported, candidate, NOW
        )
        self.assertIsNone(prepared)
        self.assertEqual(message, "mopping is not supported")

    async def test_sensor_block_skips_stage_and_notifies_once_per_day(self) -> None:
        app = transaction_application()
        blocked_robot = with_water(
            app,
            WaterReadiness("sensor_blocked", "water_low", authoritative=True),
        )
        candidate = resolved_candidate(app, CleaningOperation.MOP)
        app._async_notify_mop_skipped = AsyncMock()
        prepared, message = await app._async_prepare_occurrence(
            blocked_robot, candidate, NOW
        )
        self.assertIsNone(prepared)
        self.assertEqual(message, "skipped mopping Study: water unavailable")
        app._async_notify_mop_skipped.assert_awaited_once()

        app = transaction_application()
        active_occurrence = occurrence(operation=CleaningOperation.MOP)
        app.state.occurrences["study"] = active_occurrence
        robot = app.discovery.robots["vacuum.alpha"]
        await app._async_notify_mop_skipped(
            app.discovery.rooms["study"], robot, "water_low", active_occurrence, NOW
        )
        app._async_send_mobile_notification.assert_awaited_once()
        app._async_send_mobile_notification.reset_mock()
        await app._async_notify_mop_skipped(
            app.discovery.rooms["study"],
            robot,
            "water_low",
            active_occurrence,
            NOW + timedelta(hours=1),
        )
        app._async_send_mobile_notification.assert_not_awaited()
        await app._async_notify_mop_skipped(
            app.discovery.rooms["study"],
            robot,
            "different_reason",
            active_occurrence,
            NOW + timedelta(hours=2),
        )
        app._async_send_mobile_notification.assert_awaited_once()

    async def test_existing_confirmations_are_confirmed_waiting_or_expired(
        self,
    ) -> None:
        for status, expires_delta, expected_message, confirmed in (
            ("confirmed", timedelta(minutes=5), None, True),
            ("pending", timedelta(minutes=5), "waiting for water confirmation", False),
            (
                "pending",
                timedelta(minutes=-1),
                "mopping cancelled: water confirmation expired",
                False,
            ),
        ):
            with self.subTest(status=status, expires=expires_delta):
                app = transaction_application()
                confirmation_robot = with_water(
                    app, WaterReadiness.confirmation_required()
                )
                candidate = resolved_candidate(app, CleaningOperation.MOP)
                active_occurrence = occurrence(operation=CleaningOperation.MOP)
                app.state.occurrences["study"] = active_occurrence
                candidate = replace(
                    candidate,
                    occurrence=active_occurrence,
                    occurrence_id=active_occurrence.occurrence_id,
                )
                request = SimpleNamespace(
                    status=status,
                    expires_at=NOW + expires_delta,
                    responded_at=None,
                )
                app.state.water_confirmations[active_occurrence.occurrence_id] = request
                prepared, message = await app._async_prepare_occurrence(
                    confirmation_robot, candidate, NOW
                )
                self.assertEqual(message, expected_message)
                self.assertEqual(bool(prepared and prepared.water_confirmed), confirmed)
                if expires_delta < timedelta(0):
                    self.assertEqual(request.status, "expired")
                    self.assertEqual(request.responded_at, NOW)

    async def test_new_confirmation_delivery_is_fail_closed_and_timer_backed(
        self,
    ) -> None:
        for delivery, expected_message, expected_status in (
            ((0, 0), "mopping cancelled: no notification target", "cancelled"),
            ((1, 2), "waiting for water confirmation", "pending"),
            ((2, 2), "waiting for water confirmation", "pending"),
        ):
            with self.subTest(delivery=delivery):
                app = transaction_application()
                app._async_send_mobile_notification.return_value = delivery
                confirmation_robot = with_water(
                    app, WaterReadiness.confirmation_required()
                )
                candidate = resolved_candidate(app, CleaningOperation.MOP)
                prepared, message = await app._async_prepare_occurrence(
                    confirmation_robot, candidate, NOW
                )
                self.assertIsNone(prepared)
                self.assertEqual(message, expected_message)
                if delivery[0]:
                    request = next(iter(app.state.water_confirmations.values()))
                    self.assertEqual(request.status, expected_status)
                    app._schedule_water_confirmation.assert_called_once_with(request)
                else:
                    self.assertEqual(app.state.water_confirmations, {})
                    app._schedule_water_confirmation.assert_not_called()


class DispatchCheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_preflight_skip_mode_skip_and_fault_adapter_are_scoped(self) -> None:
        app = transaction_application()
        robot = app.discovery.robots["vacuum.alpha"]
        candidate = resolved_candidate(app, CleaningOperation.MOP)
        active_occurrence = occurrence(operation=CleaningOperation.MOP)
        app.state.occurrences["study"] = active_occurrence
        candidate = replace(candidate, occurrence=active_occurrence)
        app._async_notify_mop_skipped = AsyncMock()
        await app._async_handle_mop_preflight_blocked(
            robot, candidate, "water_low", NOW
        )
        app._async_notify_mop_skipped.assert_awaited_once()

        app.state.occurrences["study"] = occurrence(operation=CleaningOperation.MOP)
        await app._async_handle_mop_mode_unconfirmed(
            robot, candidate, "mop_mode_unconfirmed", NOW
        )
        self.assertEqual(
            app.state.room_history["study"].last_stage_outcome,
            StageStatus.SKIPPED_NO_MOP,
        )

        app._async_latch_scheduler_fault = AsyncMock()
        await app._async_latch_dispatch_fault(
            robot,
            app.discovery.rooms["study"],
            "failed",
            "dispatch",
            True,
            True,
        )
        app._async_latch_scheduler_fault.assert_awaited_once()

    async def test_checkpoint_accept_abandon_and_shutdown_dispatch_order(self) -> None:
        app = transaction_application()
        robot = app.discovery.robots["vacuum.alpha"]
        room = app.discovery.rooms["study"]
        candidate = resolved_candidate(app)
        active_occurrence = occurrence()
        app.state.occurrences["study"] = active_occurrence
        candidate = replace(candidate, occurrence=active_occurrence)
        job = active_job(occurrence_id=active_occurrence.occurrence_id)
        job.source = "manual_dashboard"

        await app._async_checkpoint_dispatch(robot, job)
        self.assertIs(app.state.active_jobs["registry-alpha"], job)
        await app._async_accept_dispatch(robot, room, candidate, job, NOW)
        self.assertEqual(job.phase, JobPhase.ACCEPTED)
        self.assertEqual(active_occurrence.stages[0].status, StageStatus.RUNNING)
        self.assertEqual(app.state.audit.manual_events[-1].outcome, "started")
        app._schedule_start_confirmation.assert_called_once_with("vacuum.alpha")

        await app._async_abandon_dispatch_checkpoint(robot)
        self.assertIsNone(app.state.active_jobs["registry-alpha"])
        self.assertEqual(app._notify_listeners.call_count, 3)

        app._closing = True
        self.assertEqual(
            await app._async_dispatch(robot, candidate, NOW),
            (False, "coordinator shutting down"),
        )
        app.dispatch.async_dispatch.assert_not_awaited()
        app._closing = False
        self.assertEqual(
            await app._async_dispatch(robot, candidate, NOW), (True, "started")
        )
        app.dispatch.async_dispatch.assert_awaited_once_with(robot, candidate, NOW)


if __name__ == "__main__":
    unittest.main()
