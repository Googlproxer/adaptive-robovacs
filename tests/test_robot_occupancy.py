"""Robot location contracts, attribution durability and scheduling regressions."""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from roborock.devices.traits.v1.home import HomeTrait
from roborock.devices.traits.v1.map_content import MapContentTrait

from custom_components.adaptive_robovacs.adapters.location import (
    RoborockLocationSource,
    location_source,
)
from custom_components.adaptive_robovacs.application import SchedulerApplication
from custom_components.adaptive_robovacs.commands import (
    StateChangedCommand,
)
from custom_components.adaptive_robovacs.models import (
    OccupancyAttribution,
    RobotLocation,
    RoomObservation,
    forecast_vacancy,
)
from custom_components.adaptive_robovacs.robot_presence import RobotPresenceTracker
from custom_components.adaptive_robovacs.state import (
    OccupancySample,
    RoomHistory,
    SchedulerState,
    StateSchemaError,
    decode_occupancy_attribution,
    encode_occupancy_attribution,
)
from tests.test_application_planning import occurrence, planning_application
from tests.test_application_state import ENTRY_DATA, NOW, robot


class NativeTrait:
    def __init__(self, **values):
        self.__dict__.update(values)
        self.listeners = []

    def add_update_listener(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def emit(self):
        for listener in tuple(self.listeners):
            listener()


def native_source(mapping=None):
    vacuum = replace(robot(), platform="roborock")
    # python-roborock 7.12.0 MapContentTrait has no listener API.
    content = SimpleNamespace(map_data=SimpleNamespace(vacuum_room=11))
    api = SimpleNamespace(
        map_content=content,
        maps=SimpleNamespace(current_map=9),
        home=NativeTrait(
            current_map_data=SimpleNamespace(
                map_flag=9,
                rooms=[
                    SimpleNamespace(segment_id=11, name="Duplicate"),
                    SimpleNamespace(segment_id=12, name="Duplicate"),
                ],
            )
        ),
    )
    coordinator = NativeTrait(properties_api=api)
    coordinator.async_add_listener = coordinator.add_update_listener
    entity = SimpleNamespace(coordinator=coordinator)
    source = RoborockLocationSource(
        vacuum, entity, mapping or {"study": ["9_11"], "dock": ["9_12"]}
    )
    source._frame_map = 9
    return source, api, coordinator


def emit_map(api):
    api.map_content.map_data = SimpleNamespace(
        vacuum_room=api.map_content.map_data.vacuum_room
    )
    api.home.emit()


class RobotLocationContractTests(unittest.TestCase):
    def test_real_native_home_listener_ignores_cached_replay_and_unchanged_frames(self):
        source, api, coordinator = native_source()
        content = MapContentTrait()
        self.assertFalse(hasattr(content, "add_update_listener"))
        content.map_data = SimpleNamespace(vacuum_room=11)
        home = HomeTrait(Mock(), api.maps, content, Mock(), Mock())
        home._home_map_info = {9: api.home.current_map_data}
        api.map_content, api.home = content, home
        clock, received = [NOW], []
        source.start(lambda: clock[0], lambda: "cleaning", received.append)
        self.assertFalse(received[-1].valid)
        self.assertIsNone(source._frame_at)
        clock[0] += timedelta(seconds=30)
        coordinator.emit()
        home._notify_update()
        self.assertIsNone(source._frame_at)
        content.map_data = SimpleNamespace(vacuum_room=11)
        home._notify_update()
        self.assertTrue(received[-1].valid)
        self.assertEqual(received[-1].area_id, "study")
        self.assertEqual(source._frame_at, clock[0])
        clock[0] += timedelta(seconds=180)
        coordinator.emit()
        self.assertFalse(received[-1].valid)
        source.close()
        observed_count = len(received)
        content.map_data = SimpleNamespace(vacuum_room=12)
        home._notify_update()
        self.assertEqual(len(received), observed_count)

    def test_missing_map_frame_clears_freshness_without_polling(self):
        source, api, coordinator = native_source()
        source.start(lambda: NOW, lambda: "cleaning", Mock())
        emit_map(api)
        self.assertEqual(source._frame_at, NOW)
        api.map_content.map_data = None
        coordinator.emit()
        self.assertIsNone(source._frame_at)
        self.assertFalse(source.read(NOW, "cleaning").valid)
        source.close()

    def test_numeric_registry_mapping_ignores_duplicate_native_names(self):
        source, api, _ = native_source()
        self.assertFalse(source.read(NOW, "cleaning").valid)
        source._frame_at = NOW
        self.assertEqual(source.read(NOW, "cleaning").area_id, "study")
        api.map_content.map_data.vacuum_room = 12
        self.assertEqual(source.read(NOW, "returning").area_id, "dock")
        source.close()

    def test_ambiguous_unmapped_wrong_map_stale_and_missing_data_are_not_presence(self):
        for condition in (
            "ambiguous",
            "unmapped",
            "wrong_map",
            "stale",
            "future",
            "missing_map",
            "missing_room",
            "missing_fields",
        ):
            with self.subTest(condition=condition):
                source, api, _ = native_source(
                    {"study": ["9_11"], "other": ["9_11"]}
                    if condition == "ambiguous"
                    else None
                )
                source._frame_at = NOW
                if condition == "unmapped":
                    api.map_content.map_data.vacuum_room = 90
                elif condition == "wrong_map":
                    api.maps.current_map = 10
                elif condition == "stale":
                    source._frame_at = NOW - timedelta(seconds=180)
                elif condition == "future":
                    source._frame_at = NOW + timedelta(seconds=1)
                elif condition == "missing_map":
                    api.home.current_map_data = None
                elif condition == "missing_room":
                    api.map_content.map_data = None
                elif condition == "missing_fields":
                    api.home.current_map_data = SimpleNamespace(map_flag=9)
                self.assertFalse(source.read(NOW, "cleaning").valid)
                self.assertIsNone(source.read(NOW, "cleaning").area_id)

    def test_native_frame_age_is_not_refreshed_by_status_heartbeats_or_dock_labels(
        self,
    ):
        source, api, coordinator = native_source()
        clock, state = [NOW], ["cleaning"]
        receive = Mock()
        source.start(lambda: clock[0], lambda: state[0], receive)
        emit_map(api)
        first = receive.call_args.args[0]
        clock[0] += timedelta(seconds=30)
        coordinator.emit()
        self.assertEqual(receive.call_count, 1)
        self.assertEqual(first.observed_at, NOW)
        api.home.emit()
        self.assertEqual(receive.call_count, 1)
        emit_map(api)
        self.assertEqual(receive.call_count, 2)
        state[0] = "docked"
        coordinator.emit()
        dock = receive.call_args.args[0]
        self.assertFalse(dock.moving)
        self.assertEqual(dock.area_id, "dock")
        clock[0] += timedelta(seconds=30)
        coordinator.emit()
        self.assertEqual(receive.call_count, 3)
        source.close()
        self.assertEqual(api.home.listeners, [])
        self.assertEqual(coordinator.listeners, [])

    def test_binding_requires_same_device_valid_registry_mapping_and_native_contract(
        self,
    ):
        source, _, _ = native_source()
        robot_entry = SimpleNamespace(
            device_id=source.robot.device_id,
            options={"vacuum": {"area_mapping": {"study": ["9_11"]}}},
        )
        hass = SimpleNamespace(
            data={
                "vacuum": SimpleNamespace(get_entity=Mock(return_value=source.entity))
            }
        )
        registry = SimpleNamespace(async_get=Mock(return_value=robot_entry))
        with patch(
            "custom_components.adaptive_robovacs.adapters.location.er.async_get",
            return_value=registry,
        ):
            self.assertIsNotNone(location_source(hass, source.robot))
            robot_entry.device_id = "different-device"
            self.assertIsNone(location_source(hass, source.robot))
            robot_entry.device_id = source.robot.device_id
            for options in ({}, {"vacuum": None}, {"vacuum": {"area_mapping": []}}):
                robot_entry.options = options
                self.assertIsNone(location_source(hass, source.robot))
            registry.async_get.return_value = None
            self.assertIsNone(location_source(hass, source.robot))
        self.assertIsNone(location_source(hass, robot()))
        self.assertIsNone(location_source(SimpleNamespace(data={}), source.robot))

    def test_binding_reuse_mapping_change_and_removed_robot_cleanup(
        self,
    ):
        app = planning_application()
        source, _, _ = native_source()
        observer = app.observer
        receive = Mock()
        with patch(
            "custom_components.adaptive_robovacs.observations.location_source",
            return_value=source,
        ):
            observer.bind_locations(app.discovery, receive, lambda: NOW)
            self.assertIn("study", observer.attribution_areas)
            self.assertEqual(len(source.api.home.listeners), 1)
            replacement, _, _ = native_source()
            replacement.entity = source.entity
            replacement.mapping_key = source.mapping_key
            with patch(
                "custom_components.adaptive_robovacs.observations.location_source",
                return_value=replacement,
            ):
                observer.bind_locations(app.discovery, receive, lambda: NOW)
            self.assertIs(observer.location_sources["registry-alpha"], source)
            replacement.mapping_key = "changed"
            with patch(
                "custom_components.adaptive_robovacs.observations.location_source",
                return_value=replacement,
            ):
                observer.bind_locations(app.discovery, receive, lambda: NOW)
            self.assertEqual(source.api.home.listeners, [])
        with patch(
            "custom_components.adaptive_robovacs.observations.location_source",
            return_value=None,
        ):
            observer.bind_locations(app.discovery, receive, lambda: NOW)
        self.assertEqual(observer.attribution_areas, frozenset())
        observer.close_locations()

    def test_tracker_infers_only_unavoidable_corridors_and_expires_stale_traffic(self):
        tracker = RobotPresenceTracker()
        tracker.set_edges((("a", "b"), ("b", "dock")))
        first = RobotLocation("robot-a", "a", NOW, True)
        self.assertTrue(tracker.observe(first))
        self.assertFalse(
            tracker.observe(replace(first, observed_at=NOW + timedelta(seconds=30)))
        )
        self.assertFalse(
            tracker.observe(replace(first, observed_at=NOW - timedelta(seconds=1)))
        )
        self.assertTrue(tracker.traffic("a", NOW + timedelta(seconds=60)))
        tracker.observe(
            RobotLocation("robot-a", "dock", NOW + timedelta(seconds=60), False)
        )
        visits = tracker.for_room("b", NOW + timedelta(seconds=60))
        self.assertEqual(len(visits), 1)
        self.assertIsNotNone(visits[0].left_at)
        self.assertFalse(tracker.traffic("a", NOW + timedelta(seconds=60)))
        self.assertEqual(tracker.for_room("b", NOW + timedelta(minutes=10)), ())
        tracker.set_edges((("a", "b"), ("b", "dock"), ("a", "dock")))
        tracker.observe(
            RobotLocation("robot-a", "a", NOW + timedelta(minutes=20), True)
        )
        tracker.observe(
            RobotLocation("robot-a", "dock", NOW + timedelta(minutes=21), False)
        )
        self.assertEqual(tracker.for_room("b", NOW + timedelta(minutes=21)), ())
        tracker.retain(set())
        self.assertEqual(tracker.locations, {})
        self.assertEqual(tracker.visits, {})

    def test_map_switch_invalidates_old_location_before_new_frame(self):
        source, api, coordinator = native_source({"study": ["9_11", "10_11"]})
        now = [NOW]
        receive = Mock()
        source.start(lambda: now[0], lambda: "cleaning", receive)
        emit_map(api)
        api.maps.current_map = 10
        api.home.current_map_data.map_flag = 10
        coordinator.emit()
        self.assertFalse(receive.call_args.args[0].valid)
        now[0] += timedelta(seconds=1)
        emit_map(api)
        self.assertTrue(receive.call_args.args[0].valid)
        self.assertFalse(receive.call_args_list[-2].args[0].valid)
        source.close()


class RobotOccupancyApplicationTests(unittest.IsolatedAsyncioTestCase):
    def test_human_occupancy_blocks_next_stage_even_in_a_started_managed_room(self):
        app = self.app()
        app.state.occurrences["study"] = occurrence()
        app._run_for_room = Mock(
            return_value=(
                "registry-alpha",
                SimpleNamespace(
                    started_room_id="study",
                    rooms=["study"],
                    occurrences={"study": "occurrence-1"},
                ),
            )
        )
        history = app.state.room_history["study"]
        for classification in ("human", "pending", "uncertain"):
            with self.subTest(classification=classification):
                history.occupancy_attribution = OccupancyAttribution(
                    raw="occupied",
                    classification=classification,
                    started_at=NOW,
                )
                history.occupancy = "occupied"
                self.assertFalse(app._run_occupancy_exempt("study"))
                candidate, reason = app._room_candidate(
                    app.discovery.rooms["study"], NOW
                )
                self.assertIsNone(candidate)
                self.assertIn("occupancy occupied", reason)
        history.occupancy_attribution = OccupancyAttribution(
            raw="occupied",
            classification="robot",
            started_at=NOW,
            robot_ids=("registry-alpha",),
        )
        history.occupancy = "unoccupied"
        self.assertTrue(app._run_occupancy_exempt("study"))

    async def test_first_enable_retires_only_unclassified_samples_once(self):
        app = planning_application()
        history = app.state.room_history["study"]
        history.occupancy_samples = [OccupancySample(NOW - timedelta(days=1), 10)]
        last_clean = history.cleaning_completed_at

        def bind(*_args):
            app.observer.attribution_areas = frozenset({"study"})

        with (
            patch(
                "custom_components.adaptive_robovacs.application.core.async_discover",
                AsyncMock(return_value=app.discovery),
            ),
            patch.object(app.observer, "bind_locations", side_effect=bind),
            patch(
                "custom_components.adaptive_robovacs.application.core.async_dispatcher_send"
            ),
        ):
            await SchedulerApplication.async_refresh_discovery(app)
            self.assertEqual(history.occupancy_samples, [])
            self.assertEqual(history.occupancy_learning_version, 2)
            fresh = OccupancySample(NOW - timedelta(hours=1), 20)
            history.occupancy_samples.append(fresh)
            await SchedulerApplication.async_refresh_discovery(app)
            self.assertEqual(history.occupancy_samples, [fresh])
            self.assertEqual(history.cleaning_completed_at, last_clean)
            with patch.object(
                app.observer,
                "bind_locations",
                side_effect=lambda *_args: setattr(
                    app.observer, "attribution_areas", frozenset()
                ),
            ):
                await SchedulerApplication.async_refresh_discovery(app)
            self.assertEqual(history.occupancy_learning_version, 1)
            self.assertIsNone(history.unoccupied_since)
            self.assertEqual(history.occupancy, "unresolved")

    async def test_native_location_events_use_fifo_and_skip_unchanged_heartbeats(self):
        app = self.app()
        app.async_execute = app._async_execute_command
        tasks = []
        app._async_create_task = lambda coro: tasks.append(asyncio.create_task(coro))
        with patch(
            "custom_components.adaptive_robovacs.application.core._now",
            return_value=NOW,
        ):
            app._on_robot_location(RobotLocation("registry-alpha", "study", NOW, True))
            await tasks.pop()
            app.async_evaluate.assert_awaited_once()
            app._on_robot_location(
                RobotLocation(
                    "registry-alpha", "study", NOW + timedelta(seconds=30), True
                )
            )
            await tasks.pop()
            app.async_evaluate.assert_awaited_once()
            app._closing = True
            app._on_robot_location(RobotLocation("registry-alpha", "study", NOW, True))
            self.assertEqual(tasks, [])

    def test_final_dispatch_revalidates_robot_traffic(self):
        app = self.app()
        self.observe(app, "off", NOW)
        candidate, reason = app._room_candidate(app.discovery.rooms["study"], NOW)
        self.assertEqual(reason, "ready")
        app._receive_robot_location(
            RobotLocation("registry-alpha", "study", NOW, True), NOW
        )
        self.assertEqual(
            app._dispatch_adjacency_block_reason(candidate, NOW),
            "robot passing through room",
        )

    def app(self):
        app = planning_application()
        app.observer.attribution_areas = frozenset({"study"})
        app.observer.location_sources = {"registry-alpha": Mock()}
        app.observer.occupancy_entities = {
            "binary_sensor.study_radar": (app.discovery.rooms["study"],)
        }
        app.hass.states.values["binary_sensor.study_radar"] = SimpleNamespace(
            state="off"
        )
        app._occupancy_deadlines = {}
        return app

    def observe(self, app, raw, now):
        app.hass.states.values["binary_sensor.study_radar"].state = raw
        app._observe_occupancy(now)
        return app.state.room_history["study"]

    def test_transit_preserves_vacancy_and_blocks_only_until_robot_has_left(self):
        app = self.app()
        history = self.observe(app, "off", NOW)
        anchor = history.unoccupied_since
        history = self.observe(app, "on", NOW + timedelta(seconds=1))
        self.assertEqual(history.occupancy, "occupied")
        app._receive_robot_location(
            RobotLocation("registry-alpha", "study", NOW + timedelta(seconds=2), True),
            NOW + timedelta(seconds=2),
        )
        self.assertEqual(history.occupancy, "unoccupied")
        self.assertEqual(
            app._room_candidate(
                app.discovery.rooms["study"], NOW + timedelta(seconds=3)
            )[1],
            "robot passing through room",
        )
        self.observe(app, "off", NOW + timedelta(seconds=30))
        app._receive_robot_location(
            RobotLocation("registry-alpha", "dock", NOW + timedelta(seconds=32), False),
            NOW + timedelta(seconds=32),
        )
        self.assertEqual(history.unoccupied_since, anchor)
        self.assertEqual(history.occupancy_samples, [])
        self.assertTrue(
            forecast_vacancy([], NOW + timedelta(minutes=1), anchor, 107, 75, 6).allowed
        )

    def test_human_source_changes_missing_locations_and_foreign_robot_stay_blocked(
        self,
    ):
        app = self.app()
        self.observe(app, "off", NOW)
        history = self.observe(app, "on", NOW + timedelta(seconds=1))
        self.observe(app, "on", NOW + timedelta(seconds=121))
        self.assertEqual(history.occupancy_attribution.classification, "human")
        app._receive_robot_location(
            RobotLocation(
                "registry-alpha", "study", NOW + timedelta(seconds=122), True
            ),
            NOW + timedelta(seconds=122),
        )
        self.assertEqual(history.occupancy, "occupied")
        self.assertFalse(
            app._receive_robot_location(
                RobotLocation("unrelated", "study", NOW, True), NOW
            )
        )
        app._observe_attributed_room(
            app.discovery.rooms["study"],
            RoomObservation("occupied", "fallback"),
            NOW + timedelta(minutes=3),
        )
        self.assertEqual(history.occupancy, "occupied")
        self.assertIsNone(history.unoccupied_since)
        app._receive_robot_location(
            RobotLocation(
                "registry-alpha", "missing", NOW + timedelta(minutes=4), True
            ),
            NOW + timedelta(minutes=4),
        )
        self.assertFalse(app.observer.locations.locations["registry-alpha"].valid)

    async def test_location_heartbeats_do_not_evaluate_save_or_decode_history(self):
        app = self.app()
        self.observe(app, "off", NOW)
        app.async_execute = app._async_execute_command
        app._receive_robot_location(
            RobotLocation("registry-alpha", "study", NOW, True), NOW
        )
        self.observe(app, "on", NOW + timedelta(seconds=1))
        history = app.state.room_history["study"]
        sample_list = history.occupancy_samples
        for offset in range(30, 121, 30):
            with patch(
                "custom_components.adaptive_robovacs.application.core._now",
                return_value=NOW + timedelta(seconds=offset),
            ):
                await app._async_handle_robot_location(
                    RobotLocation(
                        "registry-alpha", "study", NOW + timedelta(seconds=offset), True
                    )
                )
        app.async_evaluate.assert_not_awaited()
        app.storage.async_save.assert_not_awaited()
        self.assertIs(history.occupancy_samples, sample_list)
        self.assertEqual(history.occupancy, "unoccupied")

    async def test_queued_snapshots_preserve_short_occupancy_pulses(
        self,
    ):
        app = self.app()
        self.observe(app, "off", NOW)
        app.hass.states.values["binary_sensor.study_radar"].state = "off"
        for raw, seconds in (("occupied", 1), ("unoccupied", 31)):
            await app._async_execute_command(
                StateChangedCommand(
                    "binary_sensor.study_radar",
                    None,
                    raw,
                    NOW + timedelta(seconds=seconds),
                    occupancy_observations=(("study", RoomObservation(raw, "radars")),),
                )
            )
        history = app.state.room_history["study"]
        self.assertEqual(
            history.occupancy_attribution.started_at, NOW + timedelta(seconds=1)
        )
        self.assertEqual(
            history.occupancy_attribution.cleared_at, NOW + timedelta(seconds=31)
        )
        self.assertEqual(history.occupancy_samples, [])

    def test_pending_checkpoint_round_trip_and_schema_21_migration_keep_other_state(
        self,
    ):
        app = self.app()
        history = self.observe(app, "off", NOW)
        history.cleaning_completed_at = NOW - timedelta(days=1)
        self.observe(app, "on", NOW + timedelta(seconds=1))
        payload = app.state.to_store()
        restored, migrated = SchedulerState.from_store(payload, ENTRY_DATA)
        self.assertFalse(migrated)
        self.assertEqual(
            restored.room_history["study"].occupancy_attribution,
            history.occupancy_attribution,
        )
        payload["schema_version"] = 21
        del payload["room_history"]["study"]["occupancy_attribution"]
        del payload["room_history"]["study"]["occupancy_learning_version"]
        restored, migrated = SchedulerState.from_store(payload, ENTRY_DATA)
        self.assertTrue(migrated)
        self.assertEqual(
            restored.room_history["study"].cleaning_completed_at,
            history.cleaning_completed_at,
        )
        self.assertIsNone(restored.room_history["study"].occupancy_attribution)

    def test_bad_checkpoint_values_fail_closed(self):
        valid = encode_occupancy_attribution(
            OccupancyAttribution(
                raw="occupied",
                classification="pending",
                started_at=NOW,
                vacancy_since=NOW - timedelta(minutes=10),
            )
        )
        self.assertEqual(decode_occupancy_attribution(valid).classification, "pending")
        for changes in (
            {"raw": []},
            {"classification": {}},
            {"robot_ids": [None]},
            {"robot_ids": "wrong"},
            {"started_at": None},
            {"cleared_at": (NOW - timedelta(seconds=1)).isoformat()},
            {"vacancy_since": (NOW + timedelta(seconds=1)).isoformat()},
            {"classification": "robot"},
            {"classification": "clear"},
        ):
            with self.subTest(changes=changes), self.assertRaises(StateSchemaError):
                decode_occupancy_attribution({**valid, **changes})
        for version in (0, 3, True, "2"):
            with self.subTest(version=version), self.assertRaises(StateSchemaError):
                RoomHistory.from_mapping({"occupancy_learning_version": version})


if __name__ == "__main__":
    unittest.main()
