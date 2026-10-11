"""Regression tests for shared decisions, safe notifications, and fallback wakeups."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from custom_components.adaptive_robovacs.observations import HomeAssistantObserver
from custom_components.adaptive_robovacs.planner import RoomEvaluation
from custom_components.adaptive_robovacs.projections import room_view
from custom_components.adaptive_robovacs.room_status import room_status
from custom_components.adaptive_robovacs.state import (
    SchedulerState,
    WaterConfirmation,
)
from custom_components.adaptive_robovacs.storage import StateLoadResult
from tests.test_adjacency import add_neighbors
from tests.test_application_evaluation import evaluate, evaluation_application
from tests.test_application_planning import occurrence, planning_application
from tests.test_application_state import NOW


def monitored_application():
    app = planning_application()
    app.notifications = SimpleNamespace(async_send=AsyncMock(), async_clear=AsyncMock())
    app._last_opportunity_observation = None
    app._room_evaluations = {"study": RoomEvaluation(NOW, None, "forecast blocked")}
    app._shutdown_started = Mock(return_value=False)
    return app


class ApplicationOpportunityTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_discards_offline_vacancy_but_preserves_pending_program(self):
        app = monitored_application()
        app.state.occurrences["study"] = occurrence()
        stored, _ = SchedulerState.from_store(app.state.to_store(), app.entry.data)
        original = stored.to_store()
        app.storage.async_load = AsyncMock(return_value=StateLoadResult(stored, False))
        app.async_execute = AsyncMock()
        app.commands = SimpleNamespace(async_start=AsyncMock())
        app.lifecycle.async_start = AsyncMock()
        app._async_recover_active_jobs = AsyncMock()
        app._sync_robot_hold_issues = Mock()
        app._sync_room_recovery_issues = Mock()
        app._async_restore_water_confirmations = AsyncMock()
        await app.async_initialize()
        history = app.state.room_history["study"]
        self.assertIsNone(history.unoccupied_since)
        self.assertEqual(history.occupancy, "unresolved")
        self.assertEqual(app.state.to_store()["occurrences"], original["occurrences"])
        self.assertEqual(history.cleaning_completed_at, NOW - timedelta(days=7))

    async def test_previewed_new_overdue_episode_cannot_reuse_an_old_alert(self):
        app = monitored_application()
        await app._async_update_opportunities(NOW, dry_run=False)
        await app._async_update_opportunities(
            NOW + timedelta(minutes=15), dry_run=False
        )
        original = app.state.room_history["study"].opportunity.alert_signature
        app.state.room_history["study"].cleaning_completed_at = NOW - timedelta(days=5)
        await app._async_update_opportunities(NOW + timedelta(minutes=16), dry_run=True)
        app.notifications.async_clear.assert_not_awaited()
        await app._async_update_opportunities(
            NOW + timedelta(minutes=31), dry_run=False
        )
        app.notifications.async_clear.assert_awaited_once()
        self.assertEqual(app.notifications.async_send.await_count, 2)
        self.assertNotEqual(
            app.state.room_history["study"].opportunity.alert_signature, original
        )

    async def test_pending_water_counts_as_blocked_even_with_an_assigned_robot(self):
        app = monitored_application()
        pending = occurrence()
        pending.scheduled_at = NOW - timedelta(days=5)
        app.state.occurrences["study"] = pending
        app.state.water_confirmations[pending.occurrence_id] = WaterConfirmation(
            request_id="test-request",
            occurrence_id=pending.occurrence_id,
            room_id="study",
            robot_registry_id="registry-alpha",
            stage_index=0,
            confirm_hash="test-confirm-hash",
            cancel_hash="test-cancel-hash",
            tag="test-water",
            sent_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        )
        app._room_evaluations["study"] = RoomEvaluation(
            NOW, None, "assigned", selected_robot_id="vacuum.alpha"
        )
        await app._async_update_opportunities(NOW, dry_run=False)
        await app._async_update_opportunities(
            NOW + timedelta(minutes=15), dry_run=False
        )
        record = app.state.room_history["study"].opportunity
        self.assertEqual(dict(record.totals)["water"], 15)
        self.assertIn(
            "water", app.notifications.async_send.call_args.args[0]["message"]
        )

    async def test_selected_work_manual_requests_and_storage_hold_do_not_prompt(self):
        for mode in ("selected", "manual", "storage"):
            app = monitored_application()
            if mode == "selected":
                app._room_evaluations["study"] = RoomEvaluation(
                    NOW, None, "assigned", selected_robot_id="vacuum.alpha"
                )
            elif mode == "manual":
                app.state.occurrences["study"] = occurrence(manual_override=True)
                app.state.occurrences["study"].mop_delay_expirations = 2
            else:
                app._storage_safe_mode = True
            await app._async_update_opportunities(NOW, dry_run=False)
            await app._async_update_opportunities(
                NOW + timedelta(minutes=15), dry_run=False
            )
            app.notifications.async_send.assert_not_awaited()

    async def test_episode_changes_are_cleared_after_preview_without_sending(self):
        app = monitored_application()
        await app._async_update_opportunities(NOW, dry_run=False)
        await app._async_update_opportunities(
            NOW + timedelta(minutes=15), dry_run=False
        )
        app.state.room_history["study"].cleaning_completed_at = NOW
        await app._async_update_opportunities(NOW + timedelta(minutes=16), dry_run=True)
        app.notifications.async_clear.assert_not_awaited()
        await app._async_update_opportunities(
            NOW + timedelta(minutes=17), dry_run=False
        )
        app.notifications.async_clear.assert_awaited_once()
        self.assertFalse(app.state.room_history["study"].opportunity.alert_signature)

    async def test_cause_change_is_throttled_and_replaces_the_same_tag(self):
        app = monitored_application()
        await app._async_update_opportunities(NOW, dry_run=False)
        await app._async_update_opportunities(
            NOW + timedelta(minutes=15), dry_run=False
        )
        app._room_evaluations["study"] = RoomEvaluation(NOW, None, "robot unavailable")
        for minute in (16, 31, 46):
            await app._async_update_opportunities(
                NOW + timedelta(minutes=minute), dry_run=False
            )
        app.notifications.async_send.assert_awaited_once()
        # Downtime is unknown; the already observed dominant cause can change only
        # after the delivery throttle, never on each scheduler tick.
        await app._async_update_opportunities(
            NOW + timedelta(days=1, minutes=16), dry_run=False
        )
        self.assertEqual(app.notifications.async_send.await_count, 2)
        first, second = app.notifications.async_send.call_args_list
        self.assertEqual(first.args[0]["data"]["tag"], second.args[0]["data"]["tag"])

    async def test_preview_projection_and_audit_use_one_two_pass_decision(self):
        app = evaluation_application()
        app.state.room_settings["study"].vacuum_pass_count = 2
        app.state.room_history["study"].unoccupied_since = NOW - timedelta(minutes=2)
        result = await evaluate(app, dry_run=True)
        row = result["candidates"][0]["robot_eligibility"][0]
        self.assertEqual(row["passes"], 2)
        self.assertFalse(row["eligible"])
        with patch(
            "custom_components.adaptive_robovacs.projections._now", return_value=NOW
        ):
            view = room_view(app, "study")
        self.assertEqual(
            row["vacancy_diagnostic"], view.vacancy_diagnostic.as_attributes()
        )
        self.assertEqual(
            row["vacancy_diagnostic"],
            app.state.audit.room_decisions[-1].evidence.to_mapping()["vacancy"],
        )
        self.assertEqual(room_status(view), "Overdue — forecast blocked")

    async def test_neighbor_owns_horizon_fallback_and_next_wakeup(self):
        app = monitored_application()
        add_neighbors(app)
        history = app.state.room_history["bedroom"]
        history.occupancy = "unoccupied"
        history.unoccupied_since = NOW - timedelta(minutes=10)
        neighbor_settings = app.state.room_settings["bedroom"]
        neighbor_settings.occupancy_sensitivity_minutes = 12
        neighbor_settings.vacancy_fallback_minutes = 90
        app.state.room_settings["study"].occupancy_sensitivity_minutes = 1
        candidate, _ = app._room_candidate(app.discovery.rooms["study"], NOW)
        decisions = app._candidate_robot_diagnostics(candidate)
        diagnostic = decisions[0].adjacency.blockers[0].vacancy
        self.assertEqual(diagnostic.required_minutes, 12)
        self.assertEqual(diagnostic.fallback_at, NOW + timedelta(minutes=80))
        app._room_evaluations["study"] = RoomEvaluation(
            NOW, candidate, decisions[0].eligibility.reason, decisions
        )
        await app._async_update_opportunities(NOW, dry_run=True)
        self.assertEqual(
            app.lifecycle.update_opportunity_deadline.call_args.args[0],
            diagnostic.fallback_at,
        )
        neighbor_settings.vacancy_fallback_enabled = False
        self.assertFalse(
            app._forecast(
                app.discovery.rooms["bedroom"], NOW + timedelta(hours=5), 1
            ).allowed
        )

    def test_short_vacancies_are_quality_evidence_and_sensor_loss_is_censored(self):
        app = planning_application()
        app.observer = HomeAssistantObserver(app.hass)
        history = app.state.room_history["study"]
        history.unoccupied_since = NOW - timedelta(seconds=21)
        radar = "binary_sensor.study_radar"
        app.hass.states.values[radar] = SimpleNamespace(state="on")
        app._observe_occupancy(NOW)
        self.assertFalse(history.occupancy_samples)
        self.assertEqual(history.occupancy_transitions[-1].vacancy_seconds, 21)
        app.hass.states.values[radar].state = "off"
        app._observe_occupancy(NOW + timedelta(seconds=1))
        app.hass.states.values[radar].state = "unavailable"
        app._observe_occupancy(NOW + timedelta(minutes=20))
        self.assertFalse(history.occupancy_samples)
        self.assertIsNone(history.unoccupied_since)
        app.hass.states.values[radar].state = "off"
        app._observe_occupancy(NOW + timedelta(minutes=21))
        self.assertEqual(history.unoccupied_since, NOW + timedelta(minutes=21))
        with patch(
            "custom_components.adaptive_robovacs.projections._now",
            return_value=NOW + timedelta(minutes=21),
        ):
            view = room_view(app, "study")
        self.assertEqual(view.occupancy_quality.subminute_vacancies_24h, 1)
        self.assertEqual(view.occupancy_quality.median_vacancy_seconds, 21)

    def test_final_gate_reobserves_occupied_disabled_window_and_fallback_changes(self):
        for change, expected in (
            ("occupied", "room became occupied"),
            ("disabled", "room disabled"),
            ("window", "desired cleaning window"),
            ("fallback", "insufficient surviving"),
            ("unavailable", "unresolved occupancy"),
        ):
            app = planning_application()
            app.observer = HomeAssistantObserver(app.hass)
            app.hass.states.values["binary_sensor.study_radar"] = SimpleNamespace(
                state="off"
            )
            candidate, _ = app._room_candidate(app.discovery.rooms["study"], NOW)
            candidate = app._candidate_for_robot(
                candidate, app.discovery.robots["vacuum.alpha"]
            )
            self.assertIsNotNone(candidate)
            settings = app.state.room_settings["study"]
            if change == "occupied":
                app.hass.states.values["binary_sensor.study_radar"].state = "on"
            elif change == "disabled":
                settings.enabled = False
            elif change == "window":
                app._desired_window_allows = Mock(return_value=False)
            elif change == "fallback":
                settings.vacancy_fallback_enabled = False
            else:
                app.hass.states.values[
                    "binary_sensor.study_radar"
                ].state = "unavailable"
                app._unresolved_allowed = Mock(return_value=False)
            self.assertIn(
                expected, app._dispatch_adjacency_block_reason(candidate, NOW)
            )

    async def test_alert_is_once_per_episode_and_restart_does_not_duplicate_it(self):
        app = monitored_application()
        await app._async_update_opportunities(NOW, dry_run=False)
        app.notifications.async_send.assert_not_awaited()
        await app._async_update_opportunities(
            NOW + timedelta(minutes=15), dry_run=False
        )
        app.notifications.async_send.assert_awaited_once()
        await app._async_update_opportunities(
            NOW + timedelta(minutes=30), dry_run=False
        )
        app.notifications.async_send.assert_awaited_once()
        restored, _ = SchedulerState.from_store(app.state.to_store(), app.entry.data)
        app.state = restored
        app._last_opportunity_observation = None
        await app._async_update_opportunities(NOW + timedelta(hours=2), dry_run=False)
        app.notifications.async_send.assert_awaited_once()
        self.assertGreater(
            dict(app.state.room_history["study"].opportunity.totals)["unknown"], 0
        )
        app.state.room_history["study"].cleaning_completed_at = NOW + timedelta(hours=2)
        await app._async_update_opportunities(NOW + timedelta(hours=2), dry_run=False)
        app.notifications.async_clear.assert_awaited_once()
        self.assertIsNone(app.state.room_history["study"].opportunity.alert_signature)

    async def test_preview_global_modes_and_disabled_rooms_do_not_prompt(self):
        for mode in ("preview", "party_mode", "observe_only", "disabled"):
            app = monitored_application()
            if mode in {"party_mode", "observe_only"}:
                setattr(app.state.global_settings, mode, True)
            if mode == "disabled":
                app.state.room_settings["study"].enabled = False
            await app._async_update_opportunities(NOW, dry_run=mode == "preview")
            await app._async_update_opportunities(
                NOW + timedelta(minutes=15), dry_run=mode == "preview"
            )
            app.notifications.async_send.assert_not_awaited()
            if mode != "preview":
                self.assertFalse(
                    app.state.room_history["study"].opportunity.observed_window
                )

    async def test_exact_rejected_forecast_reaches_projection_and_audit(self):
        app = monitored_application()
        room = app.discovery.rooms["study"]
        history = app.state.room_history["study"]
        history.unoccupied_since = NOW - timedelta(minutes=2)
        app.state.room_settings["study"].vacuum_pass_count = 2
        candidate, _ = app._room_candidate(room, NOW)
        decisions = app._candidate_robot_diagnostics(candidate)
        self.assertFalse(decisions[0].eligibility.eligible)
        evidence = decisions[0].evidence
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.passes, 2)
        app._room_evaluations["study"] = RoomEvaluation(
            NOW, candidate, decisions[0].eligibility.reason, decisions
        )
        with patch(
            "custom_components.adaptive_robovacs.projections._now", return_value=NOW
        ):
            view = room_view(app, "study")
        self.assertEqual(view.vacancy_diagnostic, evidence.vacancy_diagnostic)
        self.assertEqual(
            view.required_vacancy_minutes,
            evidence.vacancy_diagnostic.required_clear_minutes,
        )
        self.assertEqual(view.robot_eligibility[0].passes, 2)
        app._record_room_decision(room, decisions[0].eligibility.reason, NOW, 1)
        audit = app.state.audit.room_decisions[-1]
        self.assertEqual(audit.required_clear_minutes, view.required_vacancy_minutes)
        self.assertEqual(audit.evidence.to_mapping()["passes"], 2)
        await app._async_update_opportunities(NOW, dry_run=True)
        self.assertEqual(
            app.lifecycle.update_opportunity_deadline.call_args.args[0],
            evidence.vacancy_diagnostic.fallback_at,
        )

    async def test_fallback_controls_validate_and_persist(self):
        app = monitored_application()
        for key, value in (
            ("occupancy_sensitivity_minutes", 12),
            ("vacancy_fallback_minutes", 90),
            ("vacancy_fallback_enabled", False),
        ):
            await app.async_set_room_setting("study", key, value)
            self.assertEqual(app.get_room_setting("study", key), value)
        for key, value in (
            ("occupancy_sensitivity_minutes", -1),
            ("occupancy_sensitivity_minutes", 181),
            ("vacancy_fallback_minutes", 1.5),
            ("vacancy_fallback_minutes", float("nan")),
            ("vacancy_fallback_minutes", True),
            ("vacancy_fallback_enabled", "yes"),
        ):
            with self.assertRaises(ValueError):
                await app.async_set_room_setting("study", key, value)
        history = app.state.room_history["study"]
        history.unoccupied_since = NOW - timedelta(hours=3)
        result = app._forecast(app.discovery.rooms["study"], NOW, 100)
        self.assertEqual(result.required_minutes, 12)
        self.assertFalse(result.allowed)

    async def test_repeated_mop_uses_same_episode_and_is_throttled(self):
        app = monitored_application()
        app.state.occurrences["study"] = replace(occurrence(), mop_delay_expirations=2)
        await app._async_update_opportunities(NOW, dry_run=False)
        await app._async_update_opportunities(
            NOW + timedelta(minutes=15), dry_run=False
        )
        self.assertIn(
            "Mop repeatedly", app.notifications.async_send.call_args.args[0]["message"]
        )
        await app._async_update_opportunities(
            NOW + timedelta(minutes=30), dry_run=False
        )
        app.notifications.async_send.assert_awaited_once()
