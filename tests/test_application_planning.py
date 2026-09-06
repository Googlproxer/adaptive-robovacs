"""Application policy tests for readiness, room gates, and candidate resolution."""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from datetime import timedelta
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from custom_components.adaptive_robovacs.application import SchedulerApplication
from custom_components.adaptive_robovacs.commands import EvaluateCommand
from custom_components.adaptive_robovacs.discovery import DiscoverySnapshot
from custom_components.adaptive_robovacs.models import (
    AdapterCapabilities,
    CleaningOperation,
    CleaningProgram,
    JobPhase,
    JobSource,
    OccurrenceSource,
    ResolvedCleaningProfile,
)
from custom_components.adaptive_robovacs.state import (
    ActiveJob,
    CleaningOccurrence,
    CleaningStage,
    Deferral,
    DurationSample,
    RobotCooldown,
    RobotHold,
    SchedulerFault,
    WaterConfirmation,
)
from tests.test_application_state import NOW, active_job, robot, state_application


def planning_application() -> SchedulerApplication:
    app = state_application()
    original = app.discovery.robots["vacuum.alpha"]
    capabilities = replace(
        original.adapter_capabilities,
        fan_speed_options=(),
        mode_options=(),
        mop_mode_options=(),
        mop_intensity_options=(),
        cleaning_depth_options=(),
        readiness_entity_id=None,
        readiness_states=frozenset(),
        completion_status_entity_id=None,
        terminal_completion_states=frozenset(),
    )
    simple_robot = replace(original, adapter_capabilities=capabilities)
    app.discovery = DiscoverySnapshot(
        MappingProxyType({simple_robot.entity_id: simple_robot}),
        app.discovery.rooms,
    )
    app.hass.states.values["vacuum.alpha"] = SimpleNamespace(
        state="docked", last_changed=NOW
    )
    app._robot_battery = Mock(return_value=95.0)
    app._ready_since = {"vacuum.alpha": NOW - timedelta(minutes=1)}
    app._ready_confirmation_timers = {}
    app._recovery_timers = {}
    app._start_confirmation_timers = {}
    app.state.first_scheduler_online_at = NOW - timedelta(days=30)
    settings = app.state.room_settings["study"]
    settings.enabled = True
    settings.cleaning_interval = 24
    settings.ignore_desired_window = True
    history = app.state.room_history["study"]
    history.cleaning_completed_at = NOW - timedelta(days=7)
    history.occupancy = "unoccupied"
    history.occupancy_source = "radars"
    history.unoccupied_since = NOW - timedelta(hours=2)
    return app


def occurrence(
    *,
    robot_registry_id: str = "registry-alpha",
    operation: CleaningOperation = CleaningOperation.VACUUM,
    passes: int = 1,
    current_stage: int = 0,
    manual_override: bool = False,
    profile: ResolvedCleaningProfile | None = None,
) -> CleaningOccurrence:
    return CleaningOccurrence(
        occurrence_id="occurrence-1",
        room_id="study",
        robot_registry_id=robot_registry_id,
        robot_entity_id="vacuum.alpha",
        program=(
            CleaningProgram.MOP_ONLY
            if operation is CleaningOperation.MOP
            else CleaningProgram.VACUUM_ONLY
        ),
        stages=[CleaningStage(operation, passes, cleaning_profile=profile)],
        scheduled_at=NOW - timedelta(minutes=1),
        created_at=NOW - timedelta(minutes=2),
        adapter_id="fake",
        adapter_schema_version=2,
        current_stage=current_stage,
        source=(
            OccurrenceSource.MANUAL_DASHBOARD
            if manual_override
            else OccurrenceSource.SCHEDULER
        ),
        manual_override=manual_override,
    )


class RobotReadinessTests(unittest.IsolatedAsyncioTestCase):
    def test_every_scheduled_readiness_gate_has_a_stable_reason(self) -> None:
        def evaluate(configure):
            app = planning_application()
            candidate_robot = app.discovery.robots["vacuum.alpha"]
            configured_robot = configure(app, candidate_robot)
            with patch(
                "custom_components.adaptive_robovacs.application._now",
                return_value=NOW,
            ):
                return app._robot_technically_ready(configured_robot or candidate_robot)

        cases = (
            (
                lambda app, item: app.state.robot_faults.__setitem__(
                    item.registry_id,
                    SchedulerFault(
                        "failed", item.registry_id, "study", NOW, "dispatch"
                    ),
                ),
                "scheduler held after robot dispatch fault",
            ),
            (
                lambda app, item: setattr(
                    app.state.robot_settings[item.registry_id], "enabled", False
                ),
                "robot disabled",
            ),
            (
                lambda _app, item: replace(item, supports_area_clean=False),
                "does not support Home Assistant area cleaning",
            ),
            (
                lambda app, item: app.state.robot_cooldowns.__setitem__(
                    item.registry_id,
                    RobotCooldown(NOW + timedelta(minutes=1), NOW),
                ),
                "cooling down after physical cancellation",
            ),
            (
                lambda app, item: app.state.robot_holds.__setitem__(
                    item.registry_id, RobotHold("return", "cancelling")
                ),
                "held clean returning to dock",
            ),
            (
                lambda app, item: app.state.robot_holds.__setitem__(
                    item.registry_id, RobotHold("done", "completion_pending")
                ),
                "held clean awaiting physical completion",
            ),
            (
                lambda app, item: app.state.robot_holds.__setitem__(
                    item.registry_id, RobotHold("robot_error", "held")
                ),
                "scheduler held after robot error",
            ),
            (
                lambda app, item: app.state.robot_holds.__setitem__(
                    item.registry_id, RobotHold("map_recovery_pending", "held")
                ),
                "map selection confirmation pending",
            ),
            (
                lambda app, item: app.state.robot_holds.__setitem__(
                    item.registry_id, RobotHold("paused", "held")
                ),
                "scheduler held while robot is paused",
            ),
            (
                lambda app, item: app.state.active_jobs.__setitem__(
                    item.registry_id,
                    ActiveJob(
                        "study",
                        ["study"],
                        CleaningOperation.VACUUM,
                        JobPhase.CANCELLING,
                        JobSource.SCHEDULER,
                    ),
                ),
                "active clean returning to dock",
            ),
            (
                lambda app, item: app.state.active_jobs.__setitem__(
                    item.registry_id,
                    ActiveJob(
                        "study",
                        ["study"],
                        CleaningOperation.VACUUM,
                        JobPhase.COMPLETION_HELD,
                        JobSource.SCHEDULER,
                    ),
                ),
                "active clean held after completion",
            ),
            (
                lambda app, item: app.state.active_jobs.__setitem__(
                    item.registry_id,
                    ActiveJob(
                        "study",
                        ["study"],
                        CleaningOperation.VACUUM,
                        JobPhase.ERROR_WAITING,
                        JobSource.SCHEDULER,
                    ),
                ),
                "active job held after robot error",
            ),
            (
                lambda app, item: app.state.active_jobs.__setitem__(
                    item.registry_id,
                    ActiveJob(
                        "study",
                        ["study"],
                        CleaningOperation.VACUUM,
                        JobPhase.PAUSED,
                        JobSource.SCHEDULER,
                    ),
                ),
                "active job held while robot is paused",
            ),
            (
                lambda app, item: app.hass.states.values.__setitem__(
                    item.entity_id, SimpleNamespace(state="cleaning")
                ),
                "robot is cleaning",
            ),
            (
                lambda app, _item: setattr(
                    app, "_robot_battery", Mock(return_value=None)
                ),
                "battery unavailable",
            ),
            (
                lambda app, _item: setattr(app, "_robot_battery", Mock(return_value=5)),
                "battery below minimum",
            ),
        )
        for configure, reason in cases:
            with self.subTest(reason=reason):
                ready, actual = evaluate(configure)
                self.assertFalse(ready)
                self.assertEqual(actual, reason)

        app = planning_application()
        candidate_robot = app.discovery.robots["vacuum.alpha"]
        app.state.robot_faults[candidate_robot.registry_id] = SchedulerFault(
            "failed", candidate_robot.registry_id, "study", NOW, "dispatch"
        )
        self.assertEqual(
            app._robot_technically_ready(candidate_robot, ignore_scheduler_fault=True),
            (True, "dispatchable"),
        )

    def test_detailed_servicing_battery_and_readiness_dwell_are_revalidated(
        self,
    ) -> None:
        app = planning_application()
        original = app.discovery.robots["vacuum.alpha"]
        capabilities = replace(
            original.adapter_capabilities,
            readiness_entity_id="sensor.alpha_status",
            readiness_states=frozenset({"ready"}),
        )
        candidate_robot = replace(original, adapter_capabilities=capabilities)
        app.hass.states.values["sensor.alpha_status"] = SimpleNamespace(state="washing")
        self.assertEqual(
            app._robot_technically_ready(candidate_robot),
            (False, "awaiting robot servicing"),
        )
        app.hass.states.values["sensor.alpha_status"].state = "ready"
        self.assertEqual(
            app._robot_technically_ready(candidate_robot), (True, "dispatchable")
        )

        with patch(
            "custom_components.adaptive_robovacs.application._now", return_value=NOW
        ):
            app._ready_since.clear()
            self.assertEqual(
                app._robot_ready(candidate_robot),
                (False, "confirming robot readiness"),
            )
            app._ready_since[candidate_robot.entity_id] = NOW - timedelta(seconds=10)
            self.assertEqual(app._robot_ready(candidate_robot), (True, "ready"))
        app.hass.states.values["vacuum.alpha"].state = "cleaning"
        self.assertEqual(
            app._robot_ready(candidate_robot), (False, "robot is cleaning")
        )
        app.hass.states.values["vacuum.alpha"].state = "docked"
        self.assertEqual(app._manual_robot_ready(candidate_robot), (True, "docked"))
        app.hass.states.values["vacuum.alpha"].state = "idle"
        self.assertEqual(
            app._manual_robot_ready(candidate_robot), (False, "robot is not docked")
        )

    async def test_readiness_and_completion_timers_enqueue_revalidation(self) -> None:
        app = planning_application()
        app.async_execute = AsyncMock(return_value={})
        created = []

        def create_task(coro, *, name=None):
            task = asyncio.create_task(coro, name=name)
            created.append(task)
            return task

        app._async_create_task = create_task
        callbacks = {}

        def track(_hass, callback, deadline):
            callbacks[deadline] = callback
            return Mock()

        old = Mock()
        app._ready_confirmation_timers["vacuum.alpha"] = old
        with patch(
            "custom_components.adaptive_robovacs.application."
            "async_track_point_in_utc_time",
            side_effect=track,
        ):
            app._schedule_ready_confirmation("vacuum.alpha", NOW)
        old.assert_called_once()
        deadline = NOW + timedelta(seconds=10)
        callbacks[deadline](deadline)
        await asyncio.gather(*created)
        self.assertIsInstance(app.async_execute.await_args.args[0], EvaluateCommand)
        self.assertNotIn("vacuum.alpha", app._ready_confirmation_timers)

        app._ready_since["vacuum.alpha"] = NOW
        unsubscribe = Mock()
        app._ready_confirmation_timers["vacuum.alpha"] = unsubscribe
        app._reset_ready_confirmation("vacuum.alpha")
        unsubscribe.assert_called_once()
        self.assertNotIn("vacuum.alpha", app._ready_since)

        app._robot_technically_ready = Mock(return_value=(False, "blocked"))
        app._reset_ready_confirmation = Mock()
        app._refresh_robot_readiness(NOW)
        app._reset_ready_confirmation.assert_called_once_with("vacuum.alpha")
        app._robot_technically_ready.return_value = (True, "ready")
        app._ready_since.clear()
        app._schedule_ready_confirmation = Mock()
        app._refresh_robot_readiness(NOW)
        self.assertEqual(app._ready_since["vacuum.alpha"], NOW)
        app._schedule_ready_confirmation.assert_called_once()
        app._refresh_robot_readiness(NOW)
        app._schedule_ready_confirmation.assert_called_once()

    def test_terminal_completion_and_dock_deadline_use_fresh_status(self) -> None:
        app = planning_application()
        self.assertFalse(app._terminal_completion_is_observed(None))
        original = app.discovery.robots["vacuum.alpha"]
        capabilities = replace(
            original.adapter_capabilities,
            completion_status_entity_id="sensor.alpha_status",
            terminal_completion_states=frozenset({"charging"}),
        )
        candidate_robot = replace(original, adapter_capabilities=capabilities)
        app.hass.states.values["sensor.alpha_status"] = SimpleNamespace(
            state="cleaning"
        )
        self.assertFalse(app._terminal_completion_is_observed(candidate_robot))
        app.hass.states.values["sensor.alpha_status"].state = "charging"
        self.assertTrue(app._terminal_completion_is_observed(candidate_robot))

        job = active_job()
        job.expected_end = NOW + timedelta(minutes=30)
        app._schedule_recovery_completion = Mock()
        app._set_dock_completion_pending("vacuum.alpha", job, NOW)
        self.assertEqual(job.phase, JobPhase.DOCK_COMPLETION_PENDING)
        self.assertEqual(job.docked_at, NOW)
        app._schedule_recovery_completion.assert_called_once_with(
            "vacuum.alpha", NOW + timedelta(minutes=30)
        )


class RoomPolicyTests(unittest.TestCase):
    def test_deferrals_duration_forecasts_and_audits_remain_room_scoped(self) -> None:
        app = planning_application()
        discovered_room = app.discovery.rooms["study"]
        app._set_room_deferral(
            discovered_room,
            "cleaning",
            NOW + timedelta(hours=2),
            "manual_clean",
            NOW,
        )
        self.assertEqual(
            app._room_deferral(discovered_room, "cleaning"),
            NOW + timedelta(hours=2),
        )
        history = app.state.room_history["study"]
        history.deferrals["cleaning"].room_area_id = "another"
        self.assertIsNone(app._room_deferral(discovered_room, "cleaning"))
        history.deferrals["cleaning"] = Deferral(NOW, "invalid_source", NOW, "study")
        self.assertIsNone(app._room_deferral(discovered_room, "cleaning"))
        history.deferrals.clear()

        history.duration_samples = [
            DurationSample(
                minutes=value,
                operation=CleaningOperation.VACUUM,
                passes=1,
                robot_registry_id="registry-alpha",
                source="elapsed_total_v2",
                measurement_version=2,
            )
            for value in (10, 12, 14)
        ] + [
            DurationSample(
                99,
                CleaningOperation.MOP,
                1,
                "registry-alpha",
                "elapsed_total_v2",
                measurement_version=2,
            ),
            DurationSample(
                99,
                CleaningOperation.VACUUM,
                2,
                "registry-alpha",
                "legacy",
            ),
        ]
        estimate = app._duration_estimate(
            discovered_room, "vacuum", 1, "registry-alpha"
        )
        self.assertTrue(estimate.learned)
        self.assertEqual(app._effective_duration(discovered_room, "vacuum", 1), (14, 3))

        history.occupancy_source = "no_sensor"
        self.assertEqual(
            app._forecast(discovered_room, NOW, 30).reason, "no-sensor policy"
        )
        history.occupancy_source = "radars"
        history.unoccupied_since = NOW - timedelta(hours=2)
        diagnostic = app._vacancy_diagnostic(discovered_room, NOW, 30)
        self.assertTrue(diagnostic.allowed)
        app._record_room_decision(discovered_room, "eligible", NOW, 30)
        app._record_room_decision(discovered_room, "eligible", NOW, 30)
        self.assertEqual(len(app.state.audit.room_decisions), 1)
        app._record_room_decision(discovered_room, "blocked", NOW, 30)
        self.assertEqual(len(app.state.audit.room_decisions), 2)

    def test_room_candidate_reports_each_safety_gate(self) -> None:
        def reason(configure) -> str:
            app = planning_application()
            discovered_room = app.discovery.rooms["study"]
            configure(app, discovered_room)
            return app._room_candidate(discovered_room, NOW)[1]

        cases = (
            (
                lambda app, item: app.state.room_faults.__setitem__(
                    item.area_id,
                    SchedulerFault(
                        "mapping", "registry-alpha", item.area_id, NOW, "preflight"
                    ),
                ),
                "room dispatch blocked pending Repair",
            ),
            (
                lambda app, item: setattr(
                    app.state.room_settings[item.area_id], "enabled", False
                ),
                "room disabled",
            ),
            (
                lambda app, _item: setattr(
                    app, "_startup_state_settle_until", NOW + timedelta(minutes=1)
                ),
                "awaiting Home Assistant state restoration",
            ),
            (
                lambda app, item: setattr(
                    app.state.room_history[item.area_id],
                    "cleaning_completed_at",
                    NOW,
                ),
                "not due",
            ),
            (
                lambda app, item: setattr(
                    app.state.room_history[item.area_id], "occupancy", "occupied"
                ),
                "occupancy occupied (radars)",
            ),
            (
                lambda app, item: (
                    setattr(
                        app.state.room_settings[item.area_id],
                        "ignore_desired_window",
                        False,
                    ),
                    setattr(
                        app.state.room_settings[item.area_id],
                        "desired_window_start",
                        "12:00",
                    ),
                    setattr(
                        app.state.room_settings[item.area_id],
                        "desired_window_end",
                        "13:00",
                    ),
                ),
                "waiting for desired cleaning window",
            ),
        )
        for configure, expected in cases:
            with self.subTest(reason=expected):
                self.assertEqual(reason(configure), expected)

        app = planning_application()
        discovered_room = app.discovery.rooms["study"]
        candidate, ready_reason = app._room_candidate(discovered_room, NOW)
        self.assertEqual(ready_reason, "ready")
        self.assertIsNotNone(candidate)

    def test_room_candidate_preserves_occurrence_and_water_state(self) -> None:
        app = planning_application()
        discovered_room = app.discovery.rooms["study"]
        active_occurrence = occurrence(operation=CleaningOperation.MOP, passes=2)
        app.state.occurrences["study"] = active_occurrence
        request = WaterConfirmation(
            "request-1",
            active_occurrence.occurrence_id,
            "study",
            "registry-alpha",
            0,
            "confirm",
            "cancel",
            "tag",
            NOW,
            NOW + timedelta(minutes=5),
        )
        app.state.water_confirmations[active_occurrence.occurrence_id] = request
        self.assertEqual(
            app._room_candidate(discovered_room, NOW)[1],
            "waiting for water confirmation",
        )
        request.status = "confirmed"
        candidate, _ = app._room_candidate(discovered_room, NOW)
        self.assertEqual(candidate.operation, CleaningOperation.MOP)
        self.assertEqual(candidate.passes, 2)

        active_occurrence.current_stage = 1
        self.assertEqual(
            app._room_candidate(discovered_room, NOW)[1], "occurrence is complete"
        )


class CandidateResolutionTests(unittest.TestCase):
    def test_new_occurrence_resolves_an_ordered_program_and_manual_override(
        self,
    ) -> None:
        app = planning_application()
        candidate = app._manual_candidate(
            app.discovery.rooms["study"], NOW, "configured", "ctx", "user"
        )
        resolved, reason = app._resolve_candidate_for_robot(
            candidate, app.discovery.robots["vacuum.alpha"]
        )
        self.assertEqual(reason, "eligible")
        self.assertEqual(
            [stage.operation for stage in resolved.new_stages],
            [CleaningOperation.VACUUM, CleaningOperation.MOP],
        )
        self.assertTrue(resolved.manual_override)
        self.assertEqual(resolved.confidence, 1.0)
        self.assertIsNotNone(resolved.vacancy_diagnostic)

        vacuum = replace(candidate, manual_mode="vacuum_only")
        resolved, _ = app._resolve_candidate_for_robot(
            vacuum, app.discovery.robots["vacuum.alpha"]
        )
        self.assertEqual(len(resolved.new_stages), 1)
        self.assertEqual(resolved.operation, CleaningOperation.VACUUM)

    def test_occurrence_resolution_rejects_stale_identity_stage_and_profile(
        self,
    ) -> None:
        app = planning_application()
        discovered_room = app.discovery.rooms["study"]
        base = app._recheck_candidate(discovered_room, NOW)
        missing = replace(base, room_id="missing")
        self.assertEqual(
            app._resolve_candidate_for_robot(
                missing, app.discovery.robots["vacuum.alpha"]
            )[1],
            "room is no longer discovered",
        )

        another = occurrence(robot_registry_id="registry-other")
        app.state.occurrences["study"] = another
        base = app._recheck_candidate(discovered_room, NOW)
        self.assertEqual(
            app._resolve_candidate_for_robot(
                base, app.discovery.robots["vacuum.alpha"]
            )[1],
            "occurrence assigned to another robot",
        )

        another.robot_registry_id = "registry-alpha"
        another.current_stage = 1
        self.assertEqual(
            app._resolve_candidate_for_robot(
                base, app.discovery.robots["vacuum.alpha"]
            )[1],
            "occurrence is complete",
        )

        unsupported = occurrence(passes=2)
        app.state.occurrences["study"] = unsupported
        base = app._recheck_candidate(discovered_room, NOW)
        original = app.discovery.robots["vacuum.alpha"]
        limited = replace(
            original,
            adapter_capabilities=AdapterCapabilities(
                "limited", 1, True, frozenset({1})
            ),
        )
        self.assertEqual(
            app._resolve_candidate_for_robot(base, limited)[1],
            "robot does not support the scheduled stage",
        )

        stale_profile = ResolvedCleaningProfile(
            CleaningOperation.VACUUM, fan_speed="obsolete"
        )
        app.state.occurrences["study"] = occurrence(profile=stale_profile)
        base = app._recheck_candidate(discovered_room, NOW)
        profile_robot = replace(
            original,
            adapter_capabilities=replace(
                original.adapter_capabilities,
                fan_speed_options=("quiet",),
            ),
        )
        self.assertEqual(
            app._resolve_candidate_for_robot(base, profile_robot)[1],
            "stored cleaning profile is not compatible",
        )

    def test_candidate_diagnostics_keep_every_same_floor_reason(self) -> None:
        app = planning_application()
        other_floor = robot(
            entity_id="vacuum.upstairs",
            registry_id="registry-upstairs",
            floor_id="upper",
        )
        app.discovery = DiscoverySnapshot(
            MappingProxyType(
                {
                    **dict(app.discovery.robots),
                    other_floor.entity_id: other_floor,
                }
            ),
            app.discovery.rooms,
        )
        candidate = app._manual_candidate(
            app.discovery.rooms["study"], NOW, "vacuum_only", None, None
        )
        app._manual_robot_ready = Mock(return_value=(False, "robot is not docked"))
        decisions = app._candidate_robot_diagnostics(candidate)
        self.assertEqual(len(decisions), 1)
        self.assertFalse(decisions[0].eligibility.eligible)
        self.assertEqual(decisions[0].eligibility.reason, "robot is not docked")

        scheduled = replace(candidate, manual_override=False)
        app._robot_ready = Mock(return_value=(True, "ready"))
        app._resolve_candidate_for_robot = Mock(return_value=(scheduled, "eligible"))
        decisions = app._candidate_robot_diagnostics(
            scheduled, {"vacuum.alpha": (True, "ready")}
        )
        self.assertTrue(decisions[0].eligibility.eligible)
        self.assertIs(decisions[0].candidate, scheduled)
        self.assertEqual(
            app._candidate_robot_diagnostics(replace(candidate, room_id="missing")), ()
        )


if __name__ == "__main__":
    unittest.main()
