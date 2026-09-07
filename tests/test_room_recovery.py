"""Behavioral regressions for persistent room-scoped error recovery."""

from __future__ import annotations

import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from custom_components.adaptive_robovacs.application_room_recovery import (
    matching_occurrence,
)
from custom_components.adaptive_robovacs.commands import (
    AcknowledgeRobotErrorCommand,
    AcknowledgeRoomRecoveryCommand,
    StateChangedCommand,
)
from custom_components.adaptive_robovacs.models import (
    CleaningOperation,
    CleaningProgram,
    FaultCode,
    JobPhase,
    JobSource,
    OccurrenceSource,
    StageStatus,
)
from custom_components.adaptive_robovacs.observations import HomeAssistantObserver
from custom_components.adaptive_robovacs.state import (
    ActiveJob,
    CleaningOccurrence,
    CleaningStage,
    RobotHold,
    RoomRecovery,
    SchedulerFault,
    SchedulerState,
    StateSchemaError,
)
from tests.test_application_state import ENTRY_DATA, NOW, room, state_application


def recovery_application(source=JobSource.SCHEDULER):
    app = state_application()
    robot = app.discovery.robots["vacuum.alpha"]
    robot = replace(
        robot,
        adapter_capabilities=replace(
            robot.adapter_capabilities,
            completion_status_entity_id="sensor.alpha_status",
            terminal_completion_states=frozenset({"charging"}),
            readiness_states=frozenset({"charging"}),
            error_entity_ids=("sensor.alpha_error",),
        ),
    )
    app.discovery = type(app.discovery)(
        {robot.entity_id: robot},
        {**app.discovery.rooms, "hall": room("hall")},
    )
    app.state.ensure_room("hall", False)
    app.observer = HomeAssistantObserver(app.hass)
    app._recovery_timers = {}
    app._mop_washing_is_observed = Mock(return_value=False)
    app._cancel_recovery_timer = Mock()
    app._cancel_start_confirmation = Mock()
    app._reset_ready_confirmation = Mock()
    app.gateway = SimpleNamespace(async_start=AsyncMock())
    app.dispatch = SimpleNamespace(
        async_preflight=AsyncMock(), async_validate_profile=AsyncMock()
    )
    app.state.occurrences["study"] = CleaningOccurrence(
        "occurrence-1",
        "study",
        robot.registry_id,
        robot.entity_id,
        CleaningProgram.VACUUM_THEN_MOP,
        [
            CleaningStage(
                CleaningOperation.VACUUM,
                2,
                StageStatus.COMPLETED,
                started_at=NOW - timedelta(hours=2),
                completed_at=NOW - timedelta(hours=1),
            ),
            CleaningStage(
                CleaningOperation.MOP,
                2,
                StageStatus.RUNNING,
                started_at=NOW - timedelta(minutes=30),
            ),
        ],
        NOW - timedelta(days=2),
        NOW - timedelta(days=2),
        "fake",
        2,
        current_stage=1,
        source=OccurrenceSource(source),
        manual_override=source == JobSource.MANUAL_DASHBOARD,
        bypass_desired_window=source == JobSource.MANUAL_DASHBOARD,
    )
    app.state.active_jobs[robot.registry_id] = ActiveJob(
        room_id="study",
        room_ids=["study"],
        operation=CleaningOperation.MOP,
        phase=JobPhase.CLEANING,
        source=source,
        seen_cleaning=True,
        started_at=NOW - timedelta(minutes=30),
        occurrence_id="occurrence-1",
        stage_index=1,
    )
    app.state.room_history["study"].vacuum_completed_at = NOW - timedelta(hours=1)
    set_observation(app, "error", error="robot_trapped", status="error")
    return app


def set_observation(app, state, *, error="none", status="charging"):
    for entity_id, value in {
        "vacuum.alpha": state,
        "sensor.alpha_error": error,
        "sensor.alpha_status": status,
        "sensor.alpha_battery": "100",
    }.items():
        app.hass.states.values[entity_id] = SimpleNamespace(
            state=value, last_changed=NOW
        )


async def reconcile(app, now=NOW, *, startup=False):
    with (
        patch("custom_components.adaptive_robovacs.application._now", return_value=now),
        patch(
            "custom_components.adaptive_robovacs.application._track_point",
            return_value=Mock(),
        ),
    ):
        if startup:
            await app._async_recover_active_jobs()
        else:
            await app._async_reconcile_jobs(now)


async def detach(app):
    await reconcile(app)
    set_observation(app, "docked")
    await reconcile(app, NOW + timedelta(seconds=1))
    await reconcile(app, NOW + timedelta(seconds=11))


class RoomRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_detached_attempt_cannot_complete_and_retry_can_complete_normally(
        self,
    ):
        app = recovery_application(JobSource.MANUAL_DASHBOARD)
        original = deepcopy(app.state.active_jobs["registry-alpha"])
        vacuum_completed = app.state.room_history["study"].vacuum_completed_at
        await detach(app)
        episode = app.state.room_recoveries["study"]
        for state in ("cleaning", "returning", "docked"):
            set_observation(app, state)
            await reconcile(app, NOW + timedelta(minutes=1))
        self.assertIsNone(app.state.room_history["study"].mop_completed_at)
        self.assertFalse(app.state.room_history["study"].duration_samples)
        await app.async_acknowledge_room_recovery("study", episode.recovery_id)
        # Start the retained pending mop stage as normal dispatch would, then
        # exercise observed completion through the real lifecycle reducer.
        occurrence = app.state.occurrences["study"]
        self.assertEqual(occurrence.current_stage, 1)
        self.assertEqual(occurrence.stages[1].passes, 2)
        occurrence.stages[1] = replace(
            occurrence.stages[1], status=StageStatus.RUNNING, started_at=NOW
        )
        app.state.active_jobs["registry-alpha"] = replace(
            original,
            started_at=NOW,
            observed_started_at=NOW,
            forecast_sample_eligible=True,
        )
        set_observation(app, "cleaning", status="segment_cleaning")
        await reconcile(app, NOW + timedelta(minutes=1))
        set_observation(app, "returning", status="returning_home")
        await reconcile(app, NOW + timedelta(minutes=20))
        set_observation(app, "docked")
        app.hass.states.values["vacuum.alpha"].last_changed = NOW + timedelta(
            minutes=21
        )
        await reconcile(app, NOW + timedelta(minutes=21))
        self.assertIsNone(app.state.active_jobs["registry-alpha"])
        self.assertNotIn("study", app.state.occurrences)
        self.assertEqual(
            app.state.room_history["study"].vacuum_completed_at, vacuum_completed
        )
        self.assertEqual(
            app.state.room_history["study"].mop_completed_at,
            NOW + timedelta(minutes=21),
        )
        self.assertEqual(len(app.state.room_history["study"].duration_samples), 1)

    async def test_missing_robot_resets_dwell_and_optional_diagnostic_is_supported(
        self,
    ):
        app = recovery_application()
        await reconcile(app)
        set_observation(app, "docked")
        await reconcile(app, NOW + timedelta(seconds=1))
        robot = app.discovery.robots.pop("vacuum.alpha")
        await reconcile(app, NOW + timedelta(seconds=11))
        self.assertFalse(app._room_recovery_since)
        app.discovery.robots[robot.entity_id] = replace(
            robot,
            adapter_capabilities=replace(
                robot.adapter_capabilities, error_entity_ids=()
            ),
        )
        await reconcile(app, NOW + timedelta(seconds=12))
        self.assertIsNone(app.state.room_recoveries["study"].detached_at)
        await reconcile(app, NOW + timedelta(seconds=22))
        self.assertIsNotNone(app.state.room_recoveries["study"].detached_at)

    async def test_confirmation_requires_original_job_to_be_detached_and_stage_to_match(
        self,
    ):
        app = recovery_application()
        original = deepcopy(app.state.active_jobs["registry-alpha"])
        await detach(app)
        episode = app.state.room_recoveries["study"]
        app.state.active_jobs["registry-alpha"] = original
        result = await app.async_acknowledge_room_recovery("study", episode.recovery_id)
        self.assertEqual(result["reason"], "awaiting_safe_dock")
        app.state.active_jobs["registry-alpha"] = None
        occurrence = app.state.occurrences["study"]
        occurrence.stages[1] = replace(
            occurrence.stages[1], operation=CleaningOperation.VACUUM
        )
        result = await app.async_acknowledge_room_recovery("study", episode.recovery_id)
        self.assertEqual(result["reason"], "recovery_target_unavailable")
        self.assertIn("study", app.state.room_recoveries)

    async def test_legacy_missing_hold_time_gets_a_stable_actionable_repair(self):
        app = recovery_application()
        active = app.state.active_jobs["registry-alpha"]
        active.stage_index = None
        active.phase = JobPhase.ERROR_WAITING
        active.hold_reason = "robot_error"
        app.state.robot_holds["registry-alpha"] = RobotHold("robot_error", "held")
        await reconcile(app)
        self.assertEqual(app.state.robot_holds["registry-alpha"].held_at, NOW)
        set_observation(app, "docked")
        await reconcile(app, NOW + timedelta(seconds=1))
        await reconcile(app, NOW + timedelta(seconds=11))
        with patch(
            "custom_components.adaptive_robovacs.application._now",
            return_value=NOW + timedelta(seconds=12),
        ):
            result = await app.async_acknowledge_robot_error(
                "registry-alpha", NOW.isoformat()
            )
        self.assertTrue(result["cleared"])
        app.gateway.async_start.assert_not_awaited()

    async def test_room_recovery_does_not_remove_an_independent_hold(self):
        app = recovery_application()
        await reconcile(app)
        hold = RobotHold("map_recovery_pending", "held", held_at=NOW)
        app.state.robot_holds["registry-alpha"] = hold
        set_observation(app, "docked")
        handled = await app._async_handle_room_error(
            "registry-alpha",
            app.discovery.robots["vacuum.alpha"],
            app.state.active_jobs["registry-alpha"],
            "docked",
            NOW + timedelta(minutes=1),
        )
        self.assertFalse(handled)
        self.assertEqual(app.state.robot_holds["registry-alpha"], hold)
        self.assertIsNone(app.state.room_recoveries["study"].detached_at)

    async def test_error_offline_dock_releases_robot_but_only_retries_unfinished_stage(
        self,
    ):
        for source in (JobSource.SCHEDULER, JobSource.MANUAL_DASHBOARD):
            with self.subTest(source=source):
                app = recovery_application(source)
                history = deepcopy(app.state.room_history["study"])
                completed_stage = deepcopy(app.state.occurrences["study"].stages[0])
                await reconcile(app)
                first = app.state.room_recoveries["study"]
                self.assertEqual(first.error_category, "robot_trapped")
                self.assertEqual(
                    app.state.active_jobs["registry-alpha"].phase,
                    JobPhase.ERROR_WAITING,
                )
                self.assertFalse(
                    (
                        await app.async_acknowledge_room_recovery(
                            "study", first.recovery_id
                        )
                    )["cleared"]
                )
                for state in ("idle", "unavailable", "returning"):
                    set_observation(app, state)
                    await reconcile(app, NOW + timedelta(seconds=1))
                    self.assertIn("registry-alpha", app.state.robot_holds)
                set_observation(app, "docked")
                await reconcile(app, NOW + timedelta(seconds=2))
                await reconcile(app, NOW + timedelta(seconds=11))
                self.assertIsNotNone(app.state.active_jobs["registry-alpha"])
                await reconcile(app, NOW + timedelta(seconds=12))
                self.assertIsNone(app.state.active_jobs["registry-alpha"])
                self.assertNotIn("registry-alpha", app.state.robot_holds)
                readiness = app._robot_technically_ready(
                    app.discovery.robots["vacuum.alpha"]
                )
                self.assertTrue(readiness[0], readiness[1])
                self.assertEqual(
                    app._room_candidate(app.discovery.rooms["study"], NOW)[1],
                    "room recovery blocked pending Repair",
                )
                self.assertNotEqual(
                    app._room_candidate(app.discovery.rooms["hall"], NOW)[1],
                    "room recovery blocked pending Repair",
                )
                occurrence = app.state.occurrences["study"]
                self.assertEqual(occurrence.stages[0], completed_stage)
                self.assertEqual(occurrence.stages[1].status, StageStatus.PENDING)
                self.assertIsNone(occurrence.stages[1].started_at)
                self.assertEqual(app.state.room_history["study"], history)
                # Another clean, low battery and empty water are unrelated to
                # acknowledging this already-detached room episode.
                set_observation(app, "cleaning")
                app.hass.states.values["sensor.alpha_battery"].state = "5"
                result = await app.async_acknowledge_room_recovery(
                    "study", first.recovery_id
                )
                self.assertTrue(result["cleared"])
                self.assertFalse(result["dispatch_started"])
                self.assertNotIn("study", app.state.room_recoveries)
                self.assertFalse(app.state.occurrences["study"].manual_override)
                self.assertFalse(app.state.occurrences["study"].bypass_desired_window)
                app.async_evaluate.assert_not_awaited()
                app.gateway.async_start.assert_not_awaited()
                app.dispatch.async_preflight.assert_not_awaited()

    async def test_save_failure_cannot_expose_a_robot_release_or_acknowledgement(self):
        app = recovery_application()
        await reconcile(app)
        set_observation(app, "docked")
        await reconcile(app, NOW + timedelta(seconds=1))
        saved_state = app.state
        app.storage.async_save.side_effect = OSError("disk unavailable")
        with self.assertRaises(OSError):
            await reconcile(app, NOW + timedelta(seconds=11))
        self.assertIs(app.state, saved_state)
        self.assertIsNotNone(app.state.active_jobs["registry-alpha"])
        self.assertIsNone(app.state.room_recoveries["study"].detached_at)
        self.assertEqual(
            app.state.occurrences["study"].stages[1].status, StageStatus.RUNNING
        )
        app.storage.async_save.side_effect = None
        await reconcile(app, NOW + timedelta(seconds=12))
        detached_state = app.state
        app.storage.async_save.side_effect = OSError("disk unavailable")
        with self.assertRaises(OSError):
            await app.async_acknowledge_room_recovery(
                "study", app.state.room_recoveries["study"].recovery_id
            )
        self.assertIs(app.state, detached_state)
        self.assertIn("study", app.state.room_recoveries)

    async def test_initial_save_failure_does_not_lose_original_checkpoint(self):
        app = recovery_application()
        app.storage.async_save.side_effect = OSError()
        original = app.state
        with self.assertRaises(OSError):
            await reconcile(app)
        self.assertIs(app.state, original)
        self.assertFalse(app.state.room_recoveries)
        self.assertIsNotNone(app.state.active_jobs["registry-alpha"])
        app.repairs.sync_room_recoveries.assert_not_called()

    async def test_missing_ambiguous_and_current_error_diagnostics_reset_dwell(self):
        for kind in (
            "unknown",
            "unavailable",
            "robot_trapped",
            "ambiguous",
            "servicing",
            "settling",
        ):
            with self.subTest(kind=kind):
                app = recovery_application()
                await reconcile(app)
                set_observation(app, "docked")
                await reconcile(app, NOW + timedelta(seconds=1))
                robot = app.discovery.robots["vacuum.alpha"]
                if kind == "ambiguous":
                    app.discovery.robots[robot.entity_id] = replace(
                        robot,
                        adapter_capabilities=replace(
                            robot.adapter_capabilities,
                            error_entity_ids=(
                                "sensor.alpha_error",
                                "sensor.extra_error",
                            ),
                        ),
                    )
                elif kind == "servicing":
                    app.hass.states.values[
                        "sensor.alpha_status"
                    ].state = "washing_the_mop"
                elif kind == "settling":
                    app._startup_state_settle_until = NOW + timedelta(seconds=15)
                else:
                    app.hass.states.values["sensor.alpha_error"].state = kind
                await reconcile(app, NOW + timedelta(seconds=11))
                self.assertIsNone(app.state.room_recoveries["study"].detached_at)
                self.assertNotIn("registry-alpha", app._room_recovery_since)
                app.discovery.robots[robot.entity_id] = robot
                app._startup_state_settle_until = None
                set_observation(app, "docked")
                await reconcile(app, NOW + timedelta(seconds=12))
                await reconcile(app, NOW + timedelta(seconds=21))
                self.assertIsNone(app.state.room_recoveries["study"].detached_at)
                await reconcile(app, NOW + timedelta(seconds=22))
                self.assertIsNotNone(app.state.room_recoveries["study"].detached_at)

    async def test_restart_reconstructs_legacy_error_and_never_credits_elapsed_time(
        self,
    ):
        app = recovery_application()
        active = app.state.active_jobs["registry-alpha"]
        active.phase = JobPhase.ERROR_WAITING
        active.hold_reason = "robot_error"
        active.held_at = NOW - timedelta(days=2)
        active.expected_minutes = 30
        active.last_observed_at = NOW - timedelta(days=2)
        payload = app.state.encode()
        payload["schema_version"] = 16
        payload.pop("room_recoveries")
        app.state, changed = SchedulerState.from_store(payload, ENTRY_DATA)
        self.assertTrue(changed)
        set_observation(app, "docked")
        await reconcile(app, startup=True)
        self.assertIsNone(app.state.room_recoveries["study"].detached_at)
        # Restart again midway through the confirmation interval.
        restored = recovery_application()
        restored.state, changed = SchedulerState.from_store(
            app.state.encode(), ENTRY_DATA
        )
        self.assertFalse(changed)
        set_observation(restored, "docked")
        await reconcile(restored, NOW + timedelta(seconds=8), startup=True)
        await reconcile(restored, NOW + timedelta(seconds=10))
        self.assertIsNotNone(restored.state.active_jobs["registry-alpha"])
        await reconcile(restored, NOW + timedelta(seconds=18))
        self.assertIsNone(restored.state.active_jobs["registry-alpha"])
        self.assertIsNone(restored.state.room_history["study"].mop_completed_at)
        self.assertEqual(
            restored.state.occurrences["study"].stages[0].status, StageStatus.COMPLETED
        )
        final = recovery_application()
        final.state, _ = SchedulerState.from_store(restored.state.encode(), ENTRY_DATA)
        set_observation(final, "docked")
        await reconcile(final, startup=True)
        self.assertIn("study", final.state.room_recoveries)
        self.assertIsNone(final.state.active_jobs["registry-alpha"])

    async def test_physical_resume_clears_only_episode_and_next_error_is_new(self):
        app = recovery_application()
        await reconcile(app)
        first = app.state.room_recoveries["study"].recovery_id
        set_observation(app, "cleaning", status="segment_cleaning")
        await reconcile(app, NOW + timedelta(minutes=1))
        self.assertFalse(app.state.room_recoveries)
        self.assertEqual(
            app.state.active_jobs["registry-alpha"].phase, JobPhase.CLEANING
        )
        set_observation(app, "error", error="wheels_jammed", status="error")
        await reconcile(app, NOW + timedelta(minutes=2))
        second = app.state.room_recoveries["study"]
        self.assertNotEqual(second.recovery_id, first)
        self.assertEqual(second.error_category, "wheels_jammed")

    async def test_independent_faults_pause_and_unknown_start_are_preserved(self):
        app = recovery_application()
        fault = SchedulerFault(
            FaultCode.PROFILE_APPLY_FAILED, "registry-alpha", "hall", NOW, "dispatch"
        )
        room_fault = replace(fault, reason_code=FaultCode.AREA_MAPPING_STALE)
        app.state.robot_faults["registry-alpha"] = fault
        app.state.room_faults["hall"] = room_fault
        await detach(app)
        recovery_id = app.state.room_recoveries["study"].recovery_id
        await app.async_acknowledge_room_recovery("study", recovery_id)
        self.assertEqual(app.state.robot_faults["registry-alpha"], fault)
        self.assertEqual(app.state.room_faults["hall"], room_fault)
        for kind in (
            "paused",
            "native",
            "uncertain",
            "map",
            "completed",
            "closing",
            "storage",
        ):
            with self.subTest(kind=kind):
                app = recovery_application()
                active = app.state.active_jobs["registry-alpha"]
                if kind == "paused":
                    set_observation(app, "paused")
                elif kind == "native":
                    active.source = JobSource.MANUAL_HOME_ASSISTANT
                elif kind == "uncertain":
                    active.phase = JobPhase.START_OUTCOME_UNCERTAIN
                elif kind == "map":
                    app.state.robot_holds["registry-alpha"] = RobotHold(
                        "map_recovery_pending", "held"
                    )
                elif kind == "completed":
                    active.cleaning_finished_at = NOW
                elif kind == "closing":
                    app._closing = True
                else:
                    app._storage_safe_mode = True
                self.assertFalse(
                    await app._async_handle_room_error(
                        "registry-alpha",
                        app.discovery.robots["vacuum.alpha"],
                        active,
                        app.hass.states.get("vacuum.alpha").state,
                        NOW,
                    )
                )
                self.assertFalse(app.state.room_recoveries)

    async def test_stale_id_missing_target_and_repeat_acknowledgement(self):
        app = recovery_application()
        await detach(app)
        recovery = app.state.room_recoveries["study"]
        self.assertEqual(
            (await app.async_acknowledge_room_recovery("study", "older"))["reason"],
            "recovery_changed",
        )
        occurrence = app.state.occurrences.pop("study")
        self.assertEqual(
            (await app.async_acknowledge_room_recovery("study", recovery.recovery_id))[
                "reason"
            ],
            "recovery_target_unavailable",
        )
        app.state.occurrences["study"] = occurrence
        app._closing = True
        self.assertFalse(
            (await app.async_acknowledge_room_recovery("study", recovery.recovery_id))[
                "cleared"
            ]
        )
        app._closing = False
        for _ in range(2):
            self.assertTrue(
                (
                    await app.async_acknowledge_room_recovery(
                        "study", recovery.recovery_id
                    )
                )["cleared"]
            )

    async def test_unassociated_legacy_checkpoint_requires_explicit_robot_repair(self):
        app = recovery_application()
        app.state.active_jobs["registry-alpha"].stage_index = None
        await detach(app)
        self.assertFalse(app.state.room_recoveries)
        self.assertIsNotNone(app.state.active_jobs["registry-alpha"])
        app.repairs.set_robot_error_recovery.assert_called()
        self.assertEqual(
            (await app.async_acknowledge_robot_error("registry-alpha", "old"))[
                "reason"
            ],
            "recovery_changed",
        )
        hold = app.state.robot_holds["registry-alpha"]
        with patch(
            "custom_components.adaptive_robovacs.application._now",
            return_value=NOW + timedelta(seconds=12),
        ):
            self.assertTrue(
                (
                    await app.async_acknowledge_robot_error(
                        "registry-alpha", hold.held_at.isoformat()
                    )
                )["cleared"]
            )
        self.assertIsNone(app.state.active_jobs["registry-alpha"])
        self.assertIsNone(app.state.room_history["study"].mop_completed_at)
        self.assertTrue(
            (await app.async_acknowledge_robot_error("registry-alpha", "old"))[
                "cleared"
            ]
        )

    async def test_room_manual_clean_is_rejected_before_selecting_a_robot(self):
        app = recovery_application()
        await detach(app)
        app._observe_occupancy = Mock()
        app._refresh_robot_readiness = Mock()
        app._record_manual_event = Mock()
        app._manual_candidate = Mock()
        result = await app.async_manual_clean_room("study", "configured")
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "room recovery blocked pending Repair")
        app._manual_candidate.assert_not_called()

    async def test_router_and_timer_keep_acknowledgement_non_dispatching(self):
        app = recovery_application()
        app.async_acknowledge_room_recovery = AsyncMock(return_value={"cleared": True})
        app.async_acknowledge_robot_error = AsyncMock(return_value={"cleared": True})
        await app._async_execute_command(
            AcknowledgeRoomRecoveryCommand("study", "episode")
        )
        await app._async_execute_command(
            AcknowledgeRobotErrorCommand("registry-alpha", "time")
        )
        app.async_acknowledge_room_recovery.assert_awaited_once_with("study", "episode")
        app.async_acknowledge_robot_error.assert_awaited_once_with(
            "registry-alpha", "time"
        )
        set_observation(app, "docked")
        with patch(
            "custom_components.adaptive_robovacs.application._track_point",
            return_value=Mock(),
        ) as timer:
            self.assertFalse(
                app._room_recovery_dock_confirmed(
                    app.discovery.robots["vacuum.alpha"], NOW
                )
            )
        timer.call_args.args[1](NOW + timedelta(seconds=10))
        coroutine = app._async_create_task.call_args.args[0]
        coroutine.close()
        app.map_recovery = SimpleNamespace(handle_state_transition=Mock())
        await app._async_execute_command(
            StateChangedCommand("sensor.alpha_error", "none", "unknown", NOW)
        )
        self.assertFalse(app._room_recovery_since)


class RecoveryCodecTests(unittest.TestCase):
    def test_recovery_roundtrip_and_invalid_fields_fail_closed(self):
        record = RoomRecovery(
            "episode",
            "study",
            "registry-alpha",
            "occurrence",
            1,
            CleaningOperation.MOP,
            NOW,
        )
        for detached in (None, NOW + timedelta(minutes=1)):
            self.assertEqual(
                RoomRecovery.from_mapping(
                    replace(record, detached_at=detached).to_store()
                ),
                replace(record, detached_at=detached),
            )
        for key, value in (
            ("recovery_id", ""),
            ("room_area_id", 3),
            ("stage_index", -1),
            ("stage_index", True),
            ("interrupted_at", "invalid"),
            ("detached_at", "invalid"),
            ("error_category", "raw secret text"),
            ("operation", "invalid"),
        ):
            with (
                self.subTest(key=key, value=value),
                self.assertRaises(StateSchemaError),
            ):
                RoomRecovery.from_mapping({**record.to_store(), key: value})
        state = SchedulerState.create(ENTRY_DATA)
        state.room_recoveries["wrong-key"] = record
        with self.assertRaises(StateSchemaError):
            state.encode()

    def test_schema_16_retains_other_state_and_rejects_malformed_records(self):
        app = recovery_application()
        original = app.state.encode()
        original["schema_version"] = 16
        original.pop("room_recoveries")
        original["global"]["hall_start"] = {"invalid": "retired field"}
        before = deepcopy(original)
        migrated, changed = SchedulerState.from_store(original, ENTRY_DATA)
        self.assertTrue(changed)
        self.assertEqual(original, before)
        self.assertEqual(migrated.encode()["schema_version"], 17)
        self.assertEqual(
            migrated.occurrences["study"].to_store(),
            app.state.occurrences["study"].to_store(),
        )
        self.assertNotIn("hall_start", migrated.encode()["global"])
        original["active_jobs"]["registry-alpha"]["operation"] = "invalid"
        with self.assertRaises(StateSchemaError):
            SchedulerState.from_store(original, ENTRY_DATA)

    def test_matching_occurrence_rejects_wrong_room_robot_stage_and_unconfirmed_start(
        self,
    ):
        app = recovery_application()
        active = app.state.active_jobs["registry-alpha"]
        self.assertIsNotNone(matching_occurrence(app.state, "registry-alpha", active))
        for changes in (
            {"room_ids": ["study", "hall"]},
            {"stage_index": 0},
            {"seen_cleaning": False},
            {"occurrence_id": "wrong"},
            {"operation": CleaningOperation.VACUUM},
        ):
            self.assertIsNone(
                matching_occurrence(
                    app.state, "registry-alpha", replace(active, **changes)
                )
            )
        self.assertIsNone(matching_occurrence(app.state, "different", active))
