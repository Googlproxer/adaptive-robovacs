"""Behavioural coverage for adjacency gates, migration, and published controls."""

from __future__ import annotations

import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, patch

from custom_components.adaptive_robovacs import select
from custom_components.adaptive_robovacs.commands import (
    SetGlobalCommand,
    SetRoomSettingCommand,
)
from custom_components.adaptive_robovacs.discovery import DiscoverySnapshot
from custom_components.adaptive_robovacs.floor_plans import FloorPlanWrite
from custom_components.adaptive_robovacs.models import (
    AdjacencyMode,
    CleaningOperation,
    CleaningProgram,
    StageStatus,
)
from custom_components.adaptive_robovacs.observations import HomeAssistantObserver
from custom_components.adaptive_robovacs.projections import build_snapshot, room_view
from custom_components.adaptive_robovacs.room_status import room_status
from custom_components.adaptive_robovacs.sensor import _RoomStatusSensor
from custom_components.adaptive_robovacs.state import (
    CleaningStage,
    RoomRecovery,
    SchedulerState,
)
from tests.test_application_evaluation import evaluate, evaluation_application
from tests.test_application_planning import occurrence, planning_application
from tests.test_application_state import ENTRY_DATA, NOW, active_job, room
from tests.test_entities import _Coordinator
from tests.test_state import populated_state
from tests.test_storage import scheduler_store


def add_neighbors(app):
    app.observer = HomeAssistantObserver(app.hass)
    app.hass.states.values["vacuum.alpha"].attributes = {}
    rooms = dict(app.discovery.rooms)
    for key in ("bedroom", "living"):
        rooms[key] = room(key, radar_ids=(f"binary_sensor.{key}_radar",))
        settings, history = app.state.ensure_room(key, is_bedroom=key == "bedroom")
        settings.enabled = False
        settings.adjacency_mode = AdjacencyMode.OFF
        history.occupancy = "occupied" if key == "bedroom" else "unoccupied"
    app.discovery = DiscoverySnapshot(app.discovery.robots, MappingProxyType(rooms))
    app.state.floor_plan.edges = {("bedroom", "study"), ("living", "study")}
    app.state.room_settings["study"].adjacency_mode = AdjacencyMode.ALWAYS


class AdjacencyApplicationTests(unittest.IsolatedAsyncioTestCase):
    def test_disabled_neighbor_blocks_and_clearance_preserves_cadence(self):
        app = planning_application()
        add_neighbors(app)
        target = app.discovery.rooms["study"]
        before = app.state.room_history["study"].to_store()
        blocked, reason = app._room_candidate(target, NOW)
        self.assertIsNone(blocked)
        self.assertIn("Bedroom (occupied)", reason)
        app.state.room_history["bedroom"].occupancy = "unresolved"
        self.assertIn(
            "Bedroom (occupancy unresolved)", app._room_candidate(target, NOW)[1]
        )
        app.state.room_history["bedroom"].occupancy = "unoccupied"
        allowed, _ = app._room_candidate(target, NOW)
        self.assertIsNotNone(allowed)
        self.assertEqual(app.state.room_history["study"].to_store(), before)
        self.assertFalse(app.state.room_faults)
        self.assertFalse(app.state.room_recoveries)

    def test_second_stage_waits_but_manual_occurrences_bypass_adjacency(self):
        app = planning_application()
        add_neighbors(app)
        target = app.discovery.rooms["study"]
        pending = occurrence(operation=CleaningOperation.MOP, current_stage=1)
        pending.program = CleaningProgram.VACUUM_THEN_MOP
        pending.stages.insert(
            0,
            CleaningStage(
                CleaningOperation.VACUUM,
                1,
                status=StageStatus.COMPLETED,
                completed_at=NOW - timedelta(minutes=1),
            ),
        )
        app.state.occurrences["study"] = pending
        before = pending.to_store()
        self.assertIsNone(app._room_candidate(target, NOW)[0])
        self.assertEqual(pending.to_store(), before)
        app.state, migrated = SchedulerState.from_store(app.state.encode(), ENTRY_DATA)
        self.assertFalse(migrated)
        pending = app.state.occurrences["study"]
        self.assertIsNone(app._room_candidate(target, NOW)[0])
        self.assertEqual(pending.to_store(), before)
        cadence_before = app.state.room_history["study"].to_store()
        app.state.room_history["bedroom"].occupancy = "unoccupied"
        candidate, _ = app._room_candidate(target, NOW)
        self.assertEqual(candidate.operation, CleaningOperation.MOP)
        self.assertEqual(candidate.due_at, pending.scheduled_at)
        self.assertEqual(pending.to_store(), before)
        self.assertEqual(app.state.room_history["study"].to_store(), cadence_before)
        app.state.room_history["bedroom"].occupancy = "occupied"
        pending.manual_override = True
        self.assertIsNotNone(app._room_candidate(target, NOW)[0])
        manual = app._manual_candidate(target, NOW, "configured", None, None)
        self.assertIsNone(app._dispatch_adjacency_block_reason(manual, NOW))
        self.assertTrue(manual.manual_override)

    async def test_scheduler_selects_unlinked_room_while_neighbor_protection_holds(
        self,
    ):
        app = evaluation_application()
        add_neighbors(app)
        living = app.state.room_settings["living"]
        living.enabled = True
        living.ignore_desired_window = True
        living.adjacency_mode = AdjacencyMode.ALWAYS
        history = app.state.room_history["living"]
        history.occupancy_source = "no_sensor"
        history.cleaning_completed_at = NOW - timedelta(days=30)
        result = await evaluate(app, dry_run=True)
        self.assertIn("Bedroom (occupied)", result["blocks"]["study"])
        self.assertEqual([item["room"] for item in result["assignments"]], ["living"])
        app._async_dispatch.assert_not_awaited()

    async def test_changed_neighbor_is_rechecked_before_preparation_and_dispatch(self):
        for change_at in (2, 3):
            with self.subTest(observation=change_at):
                app = evaluation_application()
                add_neighbors(app)
                app.state.room_history["bedroom"].occupancy = "unoccupied"
                observations = 0

                def observe(_now, app=app, change_at=change_at):
                    nonlocal observations
                    observations += 1
                    if observations == change_at:
                        app.state.room_history["bedroom"].occupancy = "occupied"

                app._observe_occupancy.side_effect = observe
                app._async_prepare_occurrence = AsyncMock(
                    side_effect=lambda _r, c, _t: (c, None)
                )
                result = await evaluate(app)
                app._async_dispatch.assert_not_awaited()
                self.assertIn("Bedroom (occupied)", result["dispatches"][0])
                self.assertEqual(
                    app._async_prepare_occurrence.await_count, change_at - 2
                )

    def test_last_dispatch_gate_reads_current_sensor_values_and_clock(self):
        app = planning_application()
        add_neighbors(app)
        target = app.discovery.rooms["study"]
        app.state.room_history["bedroom"].occupancy = "unoccupied"
        candidate, _ = app._room_candidate(target, NOW)
        app.hass.states.values["binary_sensor.bedroom_radar"] = SimpleNamespace(
            state="on"
        )
        reason = app._dispatch_adjacency_block_reason(candidate, NOW)
        self.assertIn("Bedroom (occupied)", reason)
        app.state.room_settings["study"].adjacency_mode = AdjacencyMode.NIGHT_ONLY
        with patch(
            "custom_components.adaptive_robovacs.application.policy._local",
            side_effect=lambda now: now,
        ):
            self.assertIsNone(
                app._dispatch_adjacency_block_reason(
                    candidate, NOW.replace(hour=22, minute=59)
                )
            )
            self.assertIn(
                "Bedroom (occupied)",
                app._dispatch_adjacency_block_reason(candidate, NOW.replace(hour=23)),
            )
        app.discovery = DiscoverySnapshot(app.discovery.robots, MappingProxyType({}))
        self.assertEqual(
            app._dispatch_adjacency_block_reason(candidate, NOW),
            "room is no longer discovered",
        )

    async def test_modes_times_and_topology_save_only_refresh_preview(self):
        app = planning_application()
        add_neighbors(app)
        for mode in AdjacencyMode:
            await app.async_set_room_setting("study", "adjacency_mode", mode.value)
            self.assertEqual(app.get_room_setting("study", "adjacency_mode"), mode)
        for key, value in (
            ("adjacency_night_start", "22:45"),
            ("adjacency_night_end", "08:15"),
        ):
            await app.async_set_global(key, value)
            self.assertEqual(app.get_global_setting(key), value)
        for key, value in (
            ("adjacency_night_start", "08:15"),
            ("adjacency_night_end", "22:45"),
            ("adjacency_night_start", "24:00"),
            ("adjacency_night_end", "08:17"),
            ("adjacency_night_start", 23),
        ):
            before = (
                app.state.global_settings.adjacency_night_start,
                app.state.global_settings.adjacency_night_end,
            )
            with self.assertRaises(ValueError):
                await app.async_set_global(key, value)
            self.assertEqual(
                before,
                (
                    app.state.global_settings.adjacency_night_start,
                    app.state.global_settings.adjacency_night_end,
                ),
            )
        with self.assertRaises(ValueError):
            await app.async_set_room_setting("study", "adjacency_mode", "invalid")
        await app.async_set_room_adjacency("study", ["bedroom"])
        await app.async_save_floor_plan(FloorPlanWrite("ground", 1, (), (), ()))
        self.assertFalse(app._lock.locked())
        for call in app.async_evaluate.await_args_list:
            self.assertTrue(call.kwargs["dry_run"])

    def test_snapshot_names_blockers_without_changing_occupancy_or_active_work(self):
        app = planning_application()
        add_neighbors(app)
        with patch(
            "custom_components.adaptive_robovacs.projections._now", return_value=NOW
        ):
            snapshot = build_snapshot(app)
            view = snapshot.room("study")
            self.assertEqual(view.occupancy, "unoccupied")
            self.assertEqual(view.adjacency_blockers[0].name, "Bedroom")
            self.assertIn("Bedroom (occupied)", room_status(view))
            self.assertEqual(view.robot_previews[0].status, "blocked")
            coordinator = _Coordinator()
            coordinator.data = snapshot
            sensor = _RoomStatusSensor(coordinator, "study", "Study")
            self.assertTrue(sensor.extra_state_attributes["adjacency_blocked"])
            self.assertEqual(
                sensor.extra_state_attributes["adjacency_blockers"],
                [{"area_id": "bedroom", "name": "Bedroom", "occupancy": "occupied"}],
            )
            active = active_job()
            app.state.active_jobs["registry-alpha"] = active
            self.assertEqual(room_status(room_view(app, "study")), "In Progress")
            self.assertIs(app.state.active_jobs["registry-alpha"], active)
            self.assertLessEqual(
                len(room_status(replace(view, adjacency_reason="x" * 500))), 255
            )


class AdjacencyEntityTests(unittest.IsolatedAsyncioTestCase):
    async def test_selects_publish_saved_modes_and_route_typed_commands(self):
        coordinator = _Coordinator()
        entity = select._RoomAdjacencySelect(
            coordinator, "study", "Study adjacency protection"
        )
        self.assertEqual(entity.options, ["Off", "Night only", "Always"])
        self.assertEqual(entity.current_option, "Night only")
        self.assertEqual(
            entity.extra_state_attributes["adaptive_robovacs_role"],
            "room_adjacency_mode_control",
        )
        unique_id = entity.unique_id
        coordinator.data.rooms[0].name = "Renamed study"
        for label, mode in select.ADJACENCY_OPTIONS.items():
            coordinator.data.rooms[0].adjacency_mode = mode
            self.assertEqual(entity.current_option, label)
            await entity.async_select_option(label)
            self.assertEqual(
                coordinator.commands[-1],
                SetRoomSettingCommand("study", "adjacency_mode", mode),
            )
        self.assertEqual(entity.unique_id, unique_id)
        for bound, expected in (("start", "23:00"), ("end", "09:00")):
            key = "adjacency_night_" + bound
            entity = select._TimeSelect(coordinator, key, key)
            self.assertEqual(entity.current_option, expected)
            self.assertEqual(len(entity.options), 96)
            await entity.async_select_option("08:15")
            self.assertEqual(coordinator.commands[-1], SetGlobalCommand(key, "08:15"))


class AdjacencyMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_schema_versions_use_storage_safe_handling(self):
        for version in ([], {}, "18", 18.0, True):
            with self.subTest(version=version):
                payload = populated_state().encode()
                payload["schema_version"] = version
                store = scheduler_store(payload)
                loaded = await store.async_load(ENTRY_DATA)
                self.assertTrue(loaded.safe_mode)
                self.assertTrue(loaded.state.global_settings.observe_only)
                self.assertIs(store._store.payload, payload)
                self.assertEqual(store._store.saved, [])

    def test_legacy_defaults_preserve_graph_occurrences_recoveries_and_history(self):
        original = populated_state()
        original.room_recoveries["study"] = RoomRecovery(
            "recovery",
            "study",
            "registry-alpha",
            "occurrence",
            0,
            CleaningOperation.VACUUM,
            NOW,
        )
        expected = original.encode()
        for version in (16, 17):
            payload = deepcopy(expected)
            payload["schema_version"] = version
            payload["global"].pop("adjacency_night_start")
            payload["global"].pop("adjacency_night_end")
            for settings in payload["room_settings"].values():
                settings.pop("adjacency_mode")
            if version == 16:
                payload.pop("room_recoveries")
            before = deepcopy(payload)
            migrated, changed = SchedulerState.from_store(payload, ENTRY_DATA)
            self.assertTrue(changed)
            self.assertEqual(payload, before)
            self.assertEqual(migrated.global_settings.adjacency_night_start, "23:00")
            self.assertEqual(migrated.global_settings.adjacency_night_end, "09:00")
            self.assertEqual(
                migrated.room_settings["study"].adjacency_mode, AdjacencyMode.NIGHT_ONLY
            )
            self.assertEqual(
                migrated.encode(),
                {
                    **expected,
                    "room_recoveries": {}
                    if version == 16
                    else expected["room_recoveries"],
                },
            )
            restored, changed = SchedulerState.from_store(migrated.encode(), ENTRY_DATA)
            self.assertFalse(changed)
            self.assertEqual(restored.encode(), migrated.encode())

    async def test_bad_adjacency_settings_leave_store_untouched_in_safe_mode(self):
        for version in (17, 18):
            for section, key, value in (
                ("global", "adjacency_night_start", "bad"),
                ("global", "adjacency_night_end", "23:00"),
                ("room", "adjacency_mode", "sometimes"),
                ("room", "adjacency_mode", []),
            ):
                with self.subTest(version=version, key=key, value=value):
                    payload = populated_state().encode()
                    payload["schema_version"] = version
                    target = (
                        payload["global"]
                        if section == "global"
                        else payload["room_settings"]["study"]
                    )
                    target[key] = value
                    store = scheduler_store(payload)
                    result = await store.async_load(ENTRY_DATA)
                    self.assertTrue(result.safe_mode)
                    self.assertTrue(result.state.global_settings.observe_only)
                    self.assertIs(store._store.payload, payload)
                    self.assertEqual(store._store.saved, [])
