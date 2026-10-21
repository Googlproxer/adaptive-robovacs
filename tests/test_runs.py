"""Complete-program queue, energy-learning, and recovery behavior."""

from __future__ import annotations

import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from custom_components.adaptive_robovacs.application import SchedulerApplication
from custom_components.adaptive_robovacs.discovery import DiscoverySnapshot
from custom_components.adaptive_robovacs.dispatch import (
    DispatchDependencies,
    DispatchPipeline,
)
from custom_components.adaptive_robovacs.models import (
    AdjacencyMode,
    CleaningOperation,
    CleaningProgram,
    JobPhase,
    JobSource,
    OccurrenceSource,
    ResolvedCleaningProfile,
    StageStatus,
)
from custom_components.adaptive_robovacs.planner import (
    PlannedCandidateAssignment,
    PlanningInput,
    ScheduleCandidate,
    WholeSchedulePlan,
)
from custom_components.adaptive_robovacs.projections import (
    robot_view as project_robot,
)
from custom_components.adaptive_robovacs.projections import (
    room_view as project_room,
)
from custom_components.adaptive_robovacs.room_status import room_status
from custom_components.adaptive_robovacs.run_planner import (
    PlannedCleaningRun,
    RunRoomOption,
    RunVacancy,
    build_run_queue,
    run_queue_block,
)
from custom_components.adaptive_robovacs.run_state import (
    BatteryUsageSample,
    CleaningRun,
    profile_key,
)
from custom_components.adaptive_robovacs.state import (
    ActiveJob,
    SchedulerFault,
    SchedulerState,
    StateSchemaError,
)
from tests.test_application_evaluation import evaluate, evaluation_application
from tests.test_application_planning import planning_application
from tests.test_application_state import NOW, room
from tests.test_application_state import robot as discovered_robot
from tests.test_dispatch import _Gateway


def option(
    area_id="a", *, minutes=(10.0,), energy=10.0, vacant_minutes=120, fallback=True
):
    candidate = ScheduleCandidate(
        area_id,
        "ground",
        CleaningOperation.VACUUM,
        NOW,
        1,
        "due",
        minutes[0],
        3,
        1,
        None,
        NOW,
        False,
        False,
        False,
        OccurrenceSource.SCHEDULER,
    )
    vacancy = RunVacancy(
        "unoccupied",
        False,
        NOW - timedelta(minutes=vacant_minutes),
        (),
        0,
        75,
        fallback,
        0,
    )
    return RunRoomOption(
        candidate,
        minutes,
        energy,
        3,
        vacancy,
        (),
        AdjacencyMode.OFF,
        "22:00",
        "06:00",
        "00:00",
        "23:59",
        True,
    )


def queued_application(program=CleaningProgram.VACUUM_ONLY):
    app = planning_application()
    app._async_save = AsyncMock()
    app._notify_listeners = Mock()
    app._observe_occupancy = Mock()
    app.hass.states.values["vacuum.alpha"].attributes = {}
    second = room("hall")
    app.discovery = DiscoverySnapshot(
        app.discovery.robots, MappingProxyType({**app.discovery.rooms, "hall": second})
    )
    app.state.ensure_room("hall", False)
    app.state.robot_settings["registry-alpha"].cleaning_program = program
    candidates = []
    for area_id in ("study", "hall"):
        settings = app.state.room_settings[area_id]
        settings.enabled = True
        settings.expected_minutes = 10
        settings.ignore_desired_window = True
        settings.adjacency_mode = AdjacencyMode.OFF
        history = app.state.room_history[area_id]
        history.cleaning_completed_at = NOW - timedelta(days=7)
        history.occupancy = "unoccupied"
        history.occupancy_source = "radars"
        history.unoccupied_since = NOW - timedelta(hours=3)
        candidate, _ = app._room_candidate(app.discovery.rooms[area_id], NOW)
        candidate = app._candidate_for_robot(
            candidate, app.discovery.robots["vacuum.alpha"]
        )
        assert candidate is not None
        candidates.append(candidate)
    profiles = {
        profile_key(stage.cleaning_profile)
        for candidate in candidates
        for stage in candidate.new_stages
    }
    app.state.battery_samples["registry-alpha"] = [
        BatteryUsageSample(key, 1, NOW) for key in profiles for _ in range(3)
    ]
    options = tuple(
        app._run_room_option(app.discovery.robots["vacuum.alpha"], item)
        for item in candidates
    )
    return app, PlannedCleaningRun("vacuum.alpha", options)


def observed_job(app, *, stage_index=0):
    robot = app.discovery.robots["vacuum.alpha"]
    run = app.state.cleaning_runs[robot.registry_id]
    occurrence = app.state.occurrences[run.rooms[0]]
    stage = occurrence.stages[stage_index]
    stage.status = StageStatus.RUNNING
    stage.started_at = NOW
    active = ActiveJob(
        occurrence.room_id,
        [occurrence.room_id],
        stage.operation,
        JobPhase.ACCEPTED,
        JobSource.SCHEDULER,
        started_at=NOW,
        occurrence_id=occurrence.occurrence_id,
        stage_index=stage_index,
        cleaning_profile=stage.cleaning_profile,
        run_id=run.run_id,
    )
    app.state.active_jobs[robot.registry_id] = active
    app._observe_run_battery(robot, active, "cleaning")
    active.seen_cleaning = True
    active.observed_started_at = NOW
    active.phase = JobPhase.CLEANING
    return active


class OrderedRunTests(unittest.TestCase):
    def test_real_elapsed_minutes_cross_daylight_saving_boundary(self):
        start = datetime(2026, 10, 4, 1, 50, tzinfo=ZoneInfo("Australia/Sydney"))
        item = replace(
            option(), ignore_window=False, window_start="03:00", window_end="04:00"
        )
        # Twenty real minutes after 01:50 is 03:10 on this date.
        self.assertIsNone(item.block_reason(start, 20, current=True))

    def test_preview_exposes_the_admission_horizon_and_one_charge_failure(self):
        run = PlannedCleaningRun("robot", (option("a"), option("b")))
        attrs = run.as_attributes(NOW)
        self.assertEqual(attrs["timings"][1]["required_clear_minutes"], 40)
        self.assertEqual(
            attrs["timings"][1]["start_at"], (NOW + timedelta(minutes=15)).isoformat()
        )
        queue, blocks = build_run_queue((option(energy=81),), NOW, 100)
        self.assertFalse(queue)
        self.assertIn("exceeds one charge", blocks[0][1])

    def test_append_keeps_priority_and_budgets_all_program_stages(self):
        a, b = option("a", minutes=(10, 10), energy=30), option("b", energy=15)
        queue, blocks = build_run_queue((a, b), NOW, 65)
        self.assertEqual([item.candidate.room_id for item in queue], ["a", "b"])
        self.assertFalse(blocks)
        self.assertEqual(PlannedCleaningRun("robot", queue).expected_minutes, 45)
        self.assertEqual(PlannedCleaningRun("robot", queue).battery_percent, 45)

    def test_short_window_can_move_a_later_room_ahead_of_oldest(self):
        a = option("oldest", minutes=(30,))
        b = option("short_window", vacant_minutes=30)
        queue, blocks = build_run_queue((a, b), NOW, 100)
        self.assertFalse(blocks)
        self.assertEqual(
            [item.candidate.room_id for item in queue], ["short_window", "oldest"]
        )

    def test_cumulative_horizon_rejects_rooms_that_only_fit_individually(self):
        a, b = option("a", vacant_minutes=30), option("b", vacant_minutes=30)
        queue, blocks = build_run_queue((a, b), NOW, 100)
        self.assertEqual(len(queue), 1)
        self.assertIn("vacancy", blocks[0][1])
        # Waiting in a queue must never manufacture a longer observed vacancy.
        self.assertFalse(b.vacancy.forecast(NOW, 15, 20).allowed)

    def test_low_battery_and_unknown_profiles_leave_rooms_unadmitted(self):
        for battery, energy in [(29, 10), (100, None), (None, 10)]:
            with self.subTest(battery=battery, energy=energy):
                queue, blocks = build_run_queue((option(energy=energy),), NOW, battery)
                self.assertFalse(queue)
                self.assertTrue(blocks)

    def test_future_stage_windows_and_night_neighbors_are_checked(self):
        item = replace(
            option(minutes=(10, 10)),
            ignore_window=False,
            window_start="09:00",
            window_end="10:10",
        )
        self.assertIn("window", item.block_reason(NOW, 0))
        item = replace(
            option(),
            adjacency_mode=AdjacencyMode.NIGHT_ONLY,
            night_start="10:00",
            night_end="11:00",
            neighbors=(replace(option().vacancy, occupancy="occupied"),),
        )
        self.assertIn("adjacency", item.block_reason(NOW, 0))
        self.assertIsNone(item.block_reason(NOW + timedelta(hours=2), 0))

    def test_current_exception_does_not_bypass_other_gates(self):
        item = replace(
            option(), vacancy=replace(option().vacancy, occupancy="occupied")
        )
        self.assertIsNone(run_queue_block((item,), NOW, 100, current_room_id="a"))
        self.assertIsNotNone(run_queue_block((item,), NOW, 29, current_room_id="a"))
        self.assertIsNotNone(run_queue_block((item,), NOW, 100))
        self.assertTrue(
            replace(item.vacancy, no_sensor=True).forecast(NOW, 50, 50).allowed
        )


class RunLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_recharge_reconciliation_does_not_credit_the_room(self):
        app, planned = queued_application()
        await app._async_start_cleaning_run(planned, NOW)
        active = observed_job(app)
        robot = app.discovery.robots["vacuum.alpha"]
        robot = replace(
            robot,
            adapter_capabilities=replace(
                robot.adapter_capabilities, readiness_entity_id="sensor.status"
            ),
        )
        app.discovery = DiscoverySnapshot(
            MappingProxyType({robot.entity_id: robot}), app.discovery.rooms
        )
        app.hass.states.values[robot.entity_id].state = "docked"
        app.hass.states.values["sensor.status"] = SimpleNamespace(
            state="waiting_to_charge"
        )
        app._async_complete_job = AsyncMock()
        await app._async_reconcile_jobs(NOW)
        app.hass.states.values["sensor.status"].state = "charging"
        await app._async_reconcile_jobs(NOW + timedelta(minutes=10))
        app._async_complete_job.assert_not_awaited()
        self.assertIs(app.state.active_jobs[robot.registry_id], active)
        self.assertEqual(active.phase, JobPhase.RECHARGING)
        self.assertEqual(app.state.occurrences["study"].current_stage, 0)
        self.assertEqual(
            app.state.room_history["study"].cleaning_completed_at,
            NOW - timedelta(days=7),
        )

    async def test_expired_mop_prerequisite_ends_old_occupancy_permission(self):
        app, planned = queued_application(CleaningProgram.VACUUM_THEN_MOP)
        await app._async_start_cleaning_run(planned, NOW)
        active = observed_job(app)
        app._complete_job(
            "vacuum.alpha", active, NOW + timedelta(minutes=10), "observed"
        )
        self.assertTrue(app._run_occupancy_exempt("study"))
        app._discard_water_confirmation = Mock(return_value=None)
        rewound = await app._async_rewind_expired_mop_occurrences(
            NOW + timedelta(hours=13)
        )
        self.assertIn("study", rewound)
        self.assertFalse(app.state.cleaning_runs)
        self.assertFalse(app._run_occupancy_exempt("study"))
        self.assertEqual(app.state.occurrences["study"].current_stage, 0)
        app.state.room_history["study"].occupancy = "occupied"
        self.assertIsNone(
            app._room_candidate(
                app.discovery.rooms["study"], NOW + timedelta(hours=13)
            )[0]
        )

    async def test_battery_refusals_are_reported_consistently_in_preview(self):
        source, _ = queued_application()
        app = evaluation_application()
        app.discovery = source.discovery
        app.state = source.state
        app.state.battery_samples["registry-alpha"] = [
            replace(sample, percent_per_minute=10)
            for sample in app.state.battery_samples["registry-alpha"]
        ]
        result = await evaluate(app, dry_run=True)
        self.assertFalse(result["assignments"])
        for candidate in result["candidates"]:
            self.assertFalse(candidate["eligible"])
            self.assertFalse(candidate["robot_eligibility"][0]["eligible"])
            self.assertIn("exceeds one charge", result["blocks"][candidate["room"]])
        app._async_dispatch.assert_not_awaited()

    async def test_actual_evaluation_checkpoints_a_queue_and_sends_one_area_only(self):
        app, _ = queued_application()
        app.hass.bus = SimpleNamespace(async_fire=Mock())
        app._async_update_opportunities = AsyncMock()
        app._async_release_ready_room_recoveries = AsyncMock()
        app._async_queue_ready_hold_rechecks = AsyncMock()
        app._schedule_start_confirmation = Mock()
        gateway = _Gateway()
        sent = []

        async def send(_robot, request):
            # Both reservations and the physical checkpoint must be durable first.
            self.assertEqual(
                app.state.cleaning_runs["registry-alpha"].rooms, ["study", "hall"]
            )
            self.assertEqual(set(app.state.occurrences), {"study", "hall"})
            self.assertEqual(
                app.state.active_jobs["registry-alpha"].room_ids, ["study"]
            )
            sent.append(request.area_ids)
            return gateway.dispatched

        gateway.async_dispatch = send
        app.dispatch = DispatchPipeline(
            gateway,
            DispatchDependencies(
                room_for_id=app.discovery.rooms.get,
                is_closing=lambda: app._closing,
                async_latch_fault=app._async_latch_dispatch_fault,
                async_handle_mop_preflight=app._async_handle_mop_preflight_blocked,
                async_handle_mop_mode=app._async_handle_mop_mode_unconfirmed,
                async_downgrade_max_plus=app._async_downgrade_q10_max_plus,
                async_checkpoint=app._async_checkpoint_dispatch,
                async_abandon_checkpoint=app._async_abandon_dispatch_checkpoint,
                async_accept=app._async_accept_dispatch,
                start_block_reason=app._dispatch_adjacency_block_reason,
            ),
        )
        with (
            patch(
                "custom_components.adaptive_robovacs.application.core._now",
                return_value=NOW,
            ),
            patch(
                "custom_components.adaptive_robovacs.dispatch.dt_util.utcnow",
                return_value=NOW,
            ),
        ):
            preview = await SchedulerApplication.async_evaluate(app, dry_run=True)
            self.assertEqual(preview["runs"][0]["rooms"], ["study", "hall"])
            self.assertFalse(app.state.cleaning_runs)
            self.assertFalse(sent)
            started = await SchedulerApplication.async_evaluate(app)
            self.assertEqual(started["dispatches"], ["dispatched Study"])
            await SchedulerApplication.async_evaluate(app)
        self.assertEqual(sent, [("study",)])
        self.assertEqual(
            app.state.occurrences["hall"].stages[0].status, StageStatus.PENDING
        )
        self.assertEqual(
            app.state.active_jobs["registry-alpha"].phase, JobPhase.ACCEPTED
        )

    async def test_queued_rooms_have_detached_views_and_are_not_all_shown_cleaning(
        self,
    ):
        app, planned = queued_application()
        await app._async_start_cleaning_run(planned, NOW)
        app._room_evaluations = {}
        with patch(
            "custom_components.adaptive_robovacs.application.core._now",
            return_value=NOW,
        ):
            view = project_robot(app, "vacuum.alpha")
            pending = project_room(app, "hall")
        self.assertEqual(view.cleaning_run.room_names, ("Study", "Hall"))
        self.assertIsNotNone(view.cleaning_run.estimated_end_at)
        self.assertEqual(pending.queue_position, 2)
        self.assertIsNone(pending.active)
        self.assertEqual(room_status(pending), "Queued (position 2)")
        self.assertEqual(
            room_status(replace(pending, queue_current_room=True)), "Finishing program"
        )

    async def test_different_robot_seeds_are_never_stolen_by_another_queue(self):
        app, planned = queued_application()
        beta = discovered_robot("vacuum.beta", registry_id="registry-beta")
        app.discovery = DiscoverySnapshot(
            MappingProxyType({**app.discovery.robots, beta.entity_id: beta}),
            app.discovery.rooms,
        )
        app.state.ensure_robot(
            beta.registry_id, False
        ).cleaning_program = CleaningProgram.VACUUM_ONLY
        app._robot_battery.return_value = 95
        app._ready_since[beta.entity_id] = NOW - timedelta(minutes=4)
        app.hass.states.values[beta.entity_id] = SimpleNamespace(
            state="docked", last_changed=NOW
        )
        app.state.battery_samples[beta.registry_id] = list(
            app.state.battery_samples["registry-alpha"]
        )
        inputs = tuple(
            PlanningInput(
                item.candidate,
                app._candidate_robot_diagnostics(item.candidate),
                index,
                (("vacuum.alpha", 95), ("vacuum.beta", 95)),
            )
            for index, item in enumerate(planned.rooms)
        )
        plan = WholeSchedulePlan(
            tuple(item.candidate for item in planned.rooms),
            (
                PlannedCandidateAssignment("vacuum.alpha", planned.rooms[0].candidate),
                PlannedCandidateAssignment("vacuum.beta", planned.rooms[1].candidate),
            ),
            (),
        )
        result, runs = app._plan_cleaning_runs(plan, inputs, NOW)
        self.assertEqual(
            {(item.robot_id, item.candidate.room_id) for item in result.assignments},
            {("vacuum.alpha", "study"), ("vacuum.beta", "hall")},
        )
        self.assertEqual(sum(len(run.rooms) for run in runs), 2)

    async def test_cancel_and_observed_recharge_release_the_queue_without_credit(self):
        app, planned = queued_application()
        await app._async_start_cleaning_run(planned, NOW)
        active = observed_job(app)
        app._cancel_job("vacuum.alpha", active, NOW, "physical_cancelled")
        self.assertFalse(app.state.cleaning_runs)
        self.assertEqual(
            app.state.room_history["study"].cleaning_completed_at,
            NOW - timedelta(days=7),
        )
        # An autonomous recharge is a distinct, persisted unfinished stage.
        app, planned = queued_application()
        await app._async_start_cleaning_run(planned, NOW)
        active = observed_job(app)
        robot = app.discovery.robots["vacuum.alpha"]
        robot = replace(
            robot,
            adapter_capabilities=replace(
                robot.adapter_capabilities, readiness_entity_id="sensor.status"
            ),
        )
        app.hass.states.values["sensor.status"] = SimpleNamespace(
            state="waiting_to_charge"
        )
        self.assertTrue(app._run_recharge_observed(robot, active, "returning"))
        self.assertFalse(app.state.cleaning_runs)
        app.hass.states.values["sensor.status"].state = "charging"
        self.assertTrue(app._run_recharge_observed(robot, active, "docked"))
        self.assertEqual(active.phase, JobPhase.RECHARGING)
        restored, _ = SchedulerState.from_store(app.state.encode(), {})
        self.assertTrue(restored.active_jobs["registry-alpha"].recharge_pending)
        app.hass.states.values["sensor.status"].state = "cleaning"
        self.assertFalse(app._run_recharge_observed(robot, active, "cleaning"))
        self.assertFalse(active.recharge_pending)
        self.assertFalse(active.battery_sample_valid)

    async def test_profile_or_room_changes_are_revalidated_before_queue_commit(self):
        app, planned = queued_application()
        app.state.room_history["study"].occupancy = "occupied"
        app.state.room_history["hall"].occupancy = "unresolved"
        self.assertIsNone(await app._async_start_cleaning_run(planned, NOW))
        self.assertFalse(app.state.cleaning_runs)
        self.assertFalse(app.state.occurrences)

    async def test_checkpoint_reserves_rooms_without_marking_pending_stages_running(
        self,
    ):
        app, planned = queued_application(CleaningProgram.VACUUM_THEN_MOP)
        head = await app._async_start_cleaning_run(planned, NOW)
        self.assertEqual(head.room_id, "study")
        self.assertEqual(list(app.state.occurrences), ["study", "hall"])
        self.assertTrue(
            all(
                stage.status == StageStatus.PENDING
                for occurrence in app.state.occurrences.values()
                for stage in occurrence.stages
            )
        )
        app._async_save.assert_awaited_once_with(force=True)
        self.assertEqual(
            app._room_candidate(app.discovery.rooms["hall"], NOW)[1],
            "queued in managed cleaning run",
        )
        self.assertFalse(app._run_occupancy_exempt("study"))

    async def test_finish_current_vacuum_and_mop_but_remove_occupied_pending_room(self):
        app, planned = queued_application(CleaningProgram.VACUUM_THEN_MOP)
        await app._async_start_cleaning_run(planned, NOW)
        active = observed_job(app)
        app._complete_job(
            "vacuum.alpha", active, NOW + timedelta(minutes=10), "observed"
        )
        self.assertEqual(app.state.occurrences["study"].current_stage, 1)
        self.assertEqual(
            app.state.room_history["study"].cleaning_completed_at,
            NOW - timedelta(days=7),
        )
        for area_id in ("study", "hall"):
            app.state.room_history[area_id].occupancy = "occupied"
        app._reconcile_cleaning_runs(NOW + timedelta(minutes=10))
        run = app.state.cleaning_runs["registry-alpha"]
        self.assertEqual(run.rooms, ["study"])
        candidate, _ = app._room_candidate(
            app.discovery.rooms["study"], NOW + timedelta(minutes=10)
        )
        resolved = app._candidate_for_robot(
            candidate, app.discovery.robots["vacuum.alpha"]
        )
        self.assertEqual(resolved.operation, CleaningOperation.MOP)
        self.assertEqual(
            resolved.vacancy_diagnostic.reason, "current room program continuation"
        )
        self.assertFalse(resolved.manual_override)
        self.assertIsNone(
            app._dispatch_adjacency_block_reason(resolved, NOW + timedelta(minutes=10))
        )
        self.assertIsNone(
            app._room_candidate(
                app.discovery.rooms["hall"], NOW + timedelta(minutes=10)
            )[0]
        )
        active = observed_job(app, stage_index=1)
        app._complete_job(
            "vacuum.alpha", active, NOW + timedelta(minutes=20), "observed"
        )
        app._reconcile_cleaning_runs(NOW + timedelta(minutes=20))
        self.assertFalse(app.state.cleaning_runs)
        self.assertFalse(app._run_occupancy_exempt("study"))
        self.assertEqual(
            app.state.room_history["study"].cleaning_completed_at,
            NOW + timedelta(minutes=20),
        )
        self.assertEqual(
            app.state.room_history["hall"].cleaning_completed_at,
            NOW - timedelta(days=7),
        )

    async def test_battery_trims_tail_and_allows_continuation_below_start_threshold(
        self,
    ):
        app, planned = queued_application(CleaningProgram.VACUUM_THEN_MOP)
        await app._async_start_cleaning_run(planned, NOW)
        active = observed_job(app)
        app._complete_job(
            "vacuum.alpha", active, NOW + timedelta(minutes=10), "observed"
        )
        app._robot_battery.return_value = 60
        app._reconcile_cleaning_runs(NOW + timedelta(minutes=10))
        run = app.state.cleaning_runs["registry-alpha"]
        self.assertEqual(run.rooms, ["study"])
        self.assertIn("recharge", run.removed["hall"])
        with patch(
            "custom_components.adaptive_robovacs.application.core._now",
            return_value=NOW + timedelta(minutes=10),
        ):
            self.assertEqual(
                app._robot_ready(app.discovery.robots["vacuum.alpha"]), (True, "ready")
            )
        app._robot_battery.return_value = 25
        app._reconcile_cleaning_runs(NOW + timedelta(minutes=10))
        self.assertFalse(app.state.cleaning_runs)
        self.assertFalse(app._run_continuation(app.discovery.robots["vacuum.alpha"]))

    async def test_pending_sensor_loss_and_new_global_modes_end_reservations(self):
        for setting in ("observe_only", "party_mode"):
            app, planned = queued_application()
            await app._async_start_cleaning_run(planned, NOW)
            observed_job(app)
            app.state.room_history["hall"].occupancy = "unresolved"
            app._reconcile_cleaning_runs(NOW)
            self.assertEqual(app.state.cleaning_runs["registry-alpha"].rooms, ["study"])
            setattr(app.state.global_settings, setting, True)
            app._reconcile_cleaning_runs(NOW)
            self.assertFalse(app.state.cleaning_runs)
            self.assertTrue(app.state.active_jobs["registry-alpha"])

    async def test_preview_is_pure_and_preserves_other_robot_assignments(self):
        app, planned = queued_application()
        robot = app.discovery.robots["vacuum.alpha"]
        inputs = tuple(
            PlanningInput(
                item.candidate,
                app._candidate_robot_diagnostics(item.candidate),
                index,
                ((robot.entity_id, 95),),
            )
            for index, item in enumerate(planned.rooms)
        )
        plan = WholeSchedulePlan(
            tuple(item.candidate for item in planned.rooms),
            (PlannedCandidateAssignment(robot.entity_id, planned.rooms[0].candidate),),
            (),
        )
        before = deepcopy(app.state.to_store())
        result, runs = app._plan_cleaning_runs(plan, inputs, NOW)
        self.assertEqual(len(runs[0].rooms), 2)
        self.assertEqual(result.assignments[0].candidate.room_id, "study")
        self.assertEqual(before, app.state.to_store())
        app.state.battery_samples.clear()
        result, runs = app._plan_cleaning_runs(plan, inputs, NOW)
        self.assertFalse(runs)
        self.assertEqual(result.assignments, plan.assignments)

    async def test_restart_preserves_refs_and_cannot_duplicate_an_accepted_start(self):
        app, planned = queued_application()
        await app._async_start_cleaning_run(planned, NOW)
        active = observed_job(app)
        payload = app.state.encode()
        restored, migrated = SchedulerState.from_store(payload, {})
        self.assertFalse(migrated)
        self.assertEqual(restored.encode(), payload)
        app.state = restored
        active = app.state.active_jobs["registry-alpha"]
        active.recovery_crossed = True
        app._reconcile_cleaning_runs(NOW)
        self.assertEqual(
            app.state.cleaning_runs["registry-alpha"].rooms, ["study", "hall"]
        )
        self.assertIsNone(app._room_candidate(app.discovery.rooms["study"], NOW)[0])
        app.state.robot_faults["registry-alpha"] = SchedulerFault(
            "start_outcome_uncertain", "registry-alpha", "study", NOW, "dispatch"
        )
        app._reconcile_cleaning_runs(NOW)
        self.assertFalse(app.state.cleaning_runs)
        self.assertEqual(app.state.active_jobs["registry-alpha"].run_id, active.run_id)

    async def test_late_battery_or_occupancy_change_prevents_start_after_checkpoint(
        self,
    ):
        app, planned = queued_application()
        head = await app._async_start_cleaning_run(planned, NOW)
        robot = app.discovery.robots["vacuum.alpha"]
        head = app._candidate_for_robot(
            app._room_candidate(app.discovery.rooms["study"], NOW)[0], robot
        )
        active = ActiveJob(
            "study",
            ["study"],
            CleaningOperation.VACUUM,
            JobPhase.DISPATCHING,
            JobSource.SCHEDULER,
            occurrence_id=head.occurrence_id,
        )
        await app._async_checkpoint_dispatch(robot, active)
        self.assertEqual(
            active.run_id, app.state.cleaning_runs[robot.registry_id].run_id
        )
        app._robot_battery.return_value = 21
        self.assertIsNotNone(app._dispatch_adjacency_block_reason(head, NOW))
        self.assertFalse(app.state.cleaning_runs)


class BatteryLearningTests(unittest.TestCase):
    def test_profile_changes_during_work_invalidate_the_measurement(self):
        app, _ = queued_application()
        robot = app.discovery.robots["vacuum.alpha"]
        job = ActiveJob(
            "study",
            ["study"],
            CleaningOperation.VACUUM,
            JobPhase.CLEANING,
            JobSource.SCHEDULER,
            seen_cleaning=True,
            observed_started_at=NOW,
            cleaning_profile=ResolvedCleaningProfile(
                CleaningOperation.VACUUM, fan_speed="max", mode="vacuum_only"
            ),
            battery_start=95,
            battery_low=80,
            battery_last=80,
        )
        app.hass.states.values[robot.entity_id].attributes = {"fan_speed": "quiet"}
        app.state.battery_samples.clear()
        app._observe_run_battery(robot, job, "cleaning")
        app._learn_run_battery(
            robot.registry_id, job, NOW + timedelta(minutes=10), "observed"
        )
        self.assertFalse(app.state.battery_samples)

    def test_completed_stage_trains_only_its_exact_profile_and_is_bounded(self):
        app, _ = queued_application()
        robot = app.discovery.robots["vacuum.alpha"]
        profile = ResolvedCleaningProfile(CleaningOperation.VACUUM)
        job = ActiveJob(
            "study",
            ["study"],
            CleaningOperation.VACUUM,
            JobPhase.ACCEPTED,
            JobSource.SCHEDULER,
            cleaning_profile=profile,
        )
        app.state.battery_samples.clear()
        app._observe_run_battery(robot, job, "cleaning")
        job.seen_cleaning = True
        job.observed_started_at = NOW
        app._robot_battery.return_value = 85
        app._observe_run_battery(robot, job, "returning")
        app._robot_battery.return_value = 86
        app._observe_run_battery(robot, job, "docked")
        for _ in range(55):
            app._learn_run_battery(
                robot.registry_id, job, NOW + timedelta(minutes=10), "observed"
            )
        samples = app.state.battery_samples[robot.registry_id]
        self.assertEqual(len(samples), 50)
        self.assertEqual(samples[0].percent_per_minute, 1)
        self.assertEqual(samples[0].profile, profile_key(profile))

    def test_pauses_charging_missing_data_recovery_and_inference_never_train(self):
        for reason in (
            "paused",
            "error",
            "unknown",
            "unavailable",
            "increase",
            "missing",
            "recovery",
            "inferred",
        ):
            with self.subTest(reason=reason):
                app, _ = queued_application()
                robot = app.discovery.robots["vacuum.alpha"]
                job = ActiveJob(
                    "study",
                    ["study"],
                    CleaningOperation.VACUUM,
                    JobPhase.CLEANING,
                    JobSource.SCHEDULER,
                    seen_cleaning=True,
                    observed_started_at=NOW,
                    cleaning_profile=ResolvedCleaningProfile(CleaningOperation.VACUUM),
                    battery_start=95,
                    battery_low=85,
                    battery_last=85,
                )
                app.state.battery_samples.clear()
                app._robot_battery.return_value = 85
                if reason == "missing":
                    app._robot_battery.return_value = None
                if reason == "increase":
                    app._robot_battery.return_value = 90
                if reason == "recovery":
                    job.recovery_crossed = True
                app._observe_run_battery(
                    robot,
                    job,
                    reason
                    if reason in {"paused", "error", "unknown", "unavailable"}
                    else "cleaning",
                )
                app._learn_run_battery(
                    robot.registry_id,
                    job,
                    NOW + timedelta(minutes=10),
                    "inferred_dock_dwell" if reason == "inferred" else "observed",
                )
                self.assertFalse(app.state.battery_samples)


class RunCodecTests(unittest.TestCase):
    def test_schema_19_migration_retains_legacy_state_and_starts_empty(self):
        app, _ = queued_application()
        old = app.state.to_store()
        old["schema_version"] = 19
        old.pop("cleaning_runs")
        old.pop("battery_samples")
        saved = deepcopy(old)
        restored, migrated = SchedulerState.from_store(old, {})
        self.assertTrue(migrated)
        self.assertFalse(restored.cleaning_runs)
        self.assertFalse(restored.battery_samples)
        self.assertEqual(
            restored.room_history["study"].cleaning_completed_at,
            NOW - timedelta(days=7),
        )
        self.assertEqual(old, saved)

    def test_corrupt_current_records_fail_closed_without_rewriting_source(self):
        app, _ = queued_application()
        sample = BatteryUsageSample("vacuum", 1, NOW).to_store()
        run = CleaningRun("run", NOW, ["study"], {"study": "occ"}).to_store()
        mutations = [
            {"cleaning_runs": {"r": {**run, "rooms": []}}},
            {"cleaning_runs": {"r": {**run, "rooms": ["study", "study"]}}},
            {"cleaning_runs": {"r": {**run, "started_room_id": "hall"}}},
            {"cleaning_runs": {"r": {**run, "occurrences": {}}}},
            {"cleaning_runs": {"r": run, "other": run}},
            {
                "battery_samples": {
                    "r": [{**sample, "percent_per_minute": float("nan")}]
                }
            },
            {"battery_samples": {"r": [{**sample, "percent_per_minute": -1}]}},
            {"battery_samples": {"r": [{**sample, "at": "invalid"}]}},
            {"battery_samples": {"r": "wrong"}},
        ]
        for mutation in mutations:
            payload = {**app.state.to_store(), **mutation}
            with self.subTest(mutation=mutation), self.assertRaises(StateSchemaError):
                SchedulerState.from_store(payload, {})
