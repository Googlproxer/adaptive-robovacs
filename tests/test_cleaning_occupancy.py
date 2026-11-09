"""Cleaning contamination boundaries, recovery, persistence and dispatch gates."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta
from types import MappingProxyType, SimpleNamespace
from unittest.mock import Mock, patch

from custom_components.adaptive_robovacs.commands import StateChangedCommand
from custom_components.adaptive_robovacs.discovery import DiscoverySnapshot
from custom_components.adaptive_robovacs.models import (
    CleaningOccupancySession,
    RobotLocation,
    RoomObservation,
)
from custom_components.adaptive_robovacs.state import (
    OccupancySample,
    SchedulerState,
    StateSchemaError,
    decode_cleaning_occupancy_sessions,
    encode_cleaning_occupancy_session,
)
from tests.test_application_planning import planning_application
from tests.test_application_state import ENTRY_DATA, NOW, active_job, robot, room


def cleaning_application():
    app = planning_application()
    rooms = dict(app.discovery.rooms)
    for area in ("dock", "other"):
        rooms[area] = room(area, radar_ids=(f"binary_sensor.{area}_radar",))
        app.state.ensure_room(area, False)
    app.discovery = DiscoverySnapshot(app.discovery.robots, MappingProxyType(rooms))
    for area, discovered in rooms.items():
        history = app.state.room_history[area]
        history.occupancy = "unoccupied"
        history.occupancy_source = "radars"
        history.unoccupied_since = NOW - timedelta(hours=2)
        for entity in discovered.radar_entity_ids:
            app.hass.states.values[entity] = SimpleNamespace(state="off")
            app.observer.occupancy_entities[entity] = (discovered,)
    app.state.active_jobs["registry-alpha"] = active_job()
    return app


def set_occupancy(app, area, raw):
    app.hass.states.values[f"binary_sensor.{area}_radar"].state = raw


def transition(app, state, at, *, entity="vacuum.alpha", areas=None):
    app.hass.states.values[entity] = SimpleNamespace(state=state)
    return app._observe_robot_cleaning_occupancy(
        app.discovery.robots[entity], state, at, areas=areas
    )


class CleaningOccupancyApplicationTests(unittest.IsolatedAsyncioTestCase):
    def test_long_known_clean_and_return_ignore_pulses_without_native_location(self):
        app = cleaning_application()
        history = app.state.room_history["study"]
        other = app.state.room_history["other"]
        old = OccupancySample(NOW - timedelta(days=1), 90)
        history.occupancy_samples = [old]
        other.occupancy_samples = [old]
        completed = history.cleaning_completed_at
        anchor = history.unoccupied_since
        transition(app, "cleaning", NOW)
        self.assertEqual(history.occupancy_learning_version, 3)
        self.assertEqual(history.occupancy_samples, [])
        for minutes in (1, 5, 30, 60):
            set_occupancy(app, "study", "on")
            app._observe_occupancy(NOW + timedelta(minutes=minutes))
            self.assertEqual(history.occupancy_attribution.classification, "cleaning")
            self.assertEqual(history.unoccupied_since, anchor)
            self.assertEqual(history.occupancy_transitions, [])
        transition(app, "returning", NOW + timedelta(minutes=65))
        app._observe_occupancy(NOW + timedelta(minutes=80))
        self.assertEqual(history.unoccupied_since, anchor)
        self.assertEqual(history.occupancy_samples, [])
        self.assertEqual(other.occupancy_samples, [old])
        self.assertEqual(history.cleaning_completed_at, completed)

    def test_occupied_docking_boundary_rebases_without_sensor_change_or_job(self):
        app = cleaning_application()
        transition(app, "cleaning", NOW)
        set_occupancy(app, "study", "on")
        app._observe_occupancy(NOW + timedelta(minutes=1))
        app.state.active_jobs["registry-alpha"] = None
        docked = NOW + timedelta(minutes=30)
        transition(app, "docked", docked)
        history = app.state.room_history["study"]
        self.assertEqual(history.occupancy, "occupied")
        self.assertEqual(history.occupancy_attribution.started_at, docked)
        self.assertEqual(
            history.occupancy_samples, [OccupancySample(NOW - timedelta(hours=2), 150)]
        )
        app._observe_occupancy(docked + timedelta(seconds=30))
        self.assertEqual(len(history.occupancy_samples), 1)
        set_occupancy(app, "study", "off")
        app._observe_occupancy(docked + timedelta(minutes=1))
        self.assertEqual(history.unoccupied_since, docked + timedelta(minutes=1))

    def test_clear_at_docking_preserves_the_original_window(self):
        app = cleaning_application()
        anchor = app.state.room_history["study"].unoccupied_since
        transition(app, "cleaning", NOW)
        set_occupancy(app, "study", "on")
        app._observe_occupancy(NOW + timedelta(minutes=1))
        set_occupancy(app, "study", "off")
        transition(app, "docked", NOW + timedelta(minutes=30))
        history = app.state.room_history["study"]
        self.assertEqual(history.unoccupied_since, anchor)
        self.assertEqual(history.occupancy_samples, [])
        self.assertIsNone(
            app._cleaning_occupancy_block("study", NOW + timedelta(minutes=30))
        )

    def test_direct_docking_cools_only_home_and_rebases_unchanged_occupied_sensor(self):
        app = cleaning_application()
        transition(app, "cleaning", NOW)
        transition(app, "returning", NOW + timedelta(minutes=20))
        set_occupancy(app, "dock", "on")
        app._observe_occupancy(NOW + timedelta(minutes=21))
        docked = NOW + timedelta(minutes=21, seconds=30)
        with patch(
            "custom_components.adaptive_robovacs.application.core._now",
            return_value=docked,
        ):
            transition(app, "docked", docked)
            history = app.state.room_history["dock"]
            self.assertEqual(
                history.occupancy_attribution.classification, "dock_cooldown"
            )
            app.lifecycle.update_occupancy_deadline.assert_called_with(
                docked + timedelta(minutes=3)
            )
        app._observe_occupancy(docked + timedelta(seconds=179))
        self.assertEqual(history.occupancy, "unoccupied")
        app._observe_occupancy(docked + timedelta(minutes=3))
        self.assertEqual(history.occupancy, "occupied")
        self.assertEqual(
            history.occupancy_attribution.started_at, docked + timedelta(minutes=3)
        )
        self.assertEqual(len(history.occupancy_samples), 1)
        self.assertEqual(app.state.room_history["study"].occupancy, "unoccupied")
        self.assertEqual(app.state.cleaning_occupancy_sessions, {})

    def test_clear_cooldown_keeps_anchor_and_unchanged_docked_never_rearms(self):
        app = cleaning_application()
        anchor = app.state.room_history["dock"].unoccupied_since
        transition(app, "cleaning", NOW)
        transition(app, "docked", NOW + timedelta(minutes=10))
        transition(app, "docked", NOW + timedelta(minutes=11))
        deadline = app.state.cleaning_occupancy_sessions[
            "registry-alpha"
        ].cooldown_until
        self.assertEqual(deadline, NOW + timedelta(minutes=13))
        app._observe_occupancy(deadline)
        app._observe_occupancy(deadline + timedelta(hours=2))
        self.assertEqual(app.state.room_history["dock"].unoccupied_since, anchor)
        self.assertEqual(app.state.cleaning_occupancy_sessions, {})

    def test_home_room_also_cleaned_waits_for_cooldown_and_blocks_manual_dispatch(self):
        app = cleaning_application()
        transition(app, "cleaning", NOW, areas=("study", "dock"))
        set_occupancy(app, "dock", "on")
        app._observe_occupancy(NOW + timedelta(minutes=1))
        docked = NOW + timedelta(minutes=10)
        transition(app, "docked", docked)
        self.assertEqual(
            app.state.room_history["dock"].occupancy_attribution.classification,
            "dock_cooldown",
        )
        candidate = SimpleNamespace(manual_override=True, room_id="dock")
        self.assertEqual(
            app._dispatch_adjacency_block_reason(candidate, docked),
            "dock room occupancy cooldown",
        )
        app._observe_occupancy(docked + timedelta(minutes=3))
        history = app.state.room_history["dock"]
        self.assertEqual(
            history.occupancy_attribution.started_at, docked + timedelta(minutes=3)
        )
        self.assertEqual(len(history.occupancy_samples), 1)

    def test_later_sensor_event_is_applied_after_exact_cooldown_boundary(self):
        app = cleaning_application()
        transition(app, "cleaning", NOW)
        transition(app, "docked", NOW + timedelta(minutes=10))
        boundary = NOW + timedelta(minutes=13)
        app._expire_cleaning_occupancy(boundary + timedelta(seconds=30))
        set_occupancy(app, "dock", "on")
        app._observe_attributed_room(
            app.discovery.rooms["dock"],
            RoomObservation("occupied", "radars"),
            boundary + timedelta(seconds=30),
        )
        self.assertEqual(
            app.state.room_history["dock"].occupancy_attribution.started_at,
            boundary + timedelta(seconds=30),
        )

    def test_return_location_refresh_keeps_external_room_but_not_transit_in_scope(self):
        app = cleaning_application()
        app.state.active_jobs["registry-alpha"] = None
        app.observer.location_sources["registry-alpha"] = Mock()
        app.observer.attribution_areas = frozenset({"study", "other"})
        transition(app, "cleaning", NOW)
        app._receive_robot_location(
            RobotLocation("registry-alpha", "study", NOW, True), NOW
        )
        set_occupancy(app, "study", "on")
        app._observe_occupancy(NOW + timedelta(seconds=1))
        transition(app, "returning", NOW + timedelta(seconds=100))
        app._receive_robot_location(
            RobotLocation(
                "registry-alpha", "other", NOW + timedelta(seconds=150), True
            ),
            NOW + timedelta(seconds=150),
        )
        app._observe_occupancy(NOW + timedelta(seconds=200))
        self.assertEqual(
            app.state.cleaning_occupancy_sessions["registry-alpha"].areas, ("study",)
        )
        self.assertEqual(
            app.state.room_history["study"].occupancy_attribution.classification,
            "cleaning",
        )
        self.assertIsNone(
            app._cleaning_occupancy_block("other", NOW + timedelta(seconds=200))
        )

    def test_late_map_frame_cannot_revive_a_vacancy_across_an_expired_location_gap(
        self,
    ):
        app = cleaning_application()
        app.state.active_jobs["registry-alpha"] = None
        app.observer.location_sources["registry-alpha"] = Mock()
        app.observer.attribution_areas = frozenset({"study"})
        transition(app, "cleaning", NOW)
        app._receive_robot_location(
            RobotLocation("registry-alpha", "study", NOW, True), NOW
        )
        set_occupancy(app, "study", "on")
        app._observe_occupancy(NOW + timedelta(seconds=1))
        late = NOW + timedelta(minutes=4)
        self.assertTrue(
            app._receive_robot_location(
                RobotLocation("registry-alpha", "study", late, True), late
            )
        )
        history = app.state.room_history["study"]
        self.assertEqual(history.occupancy, "occupied")
        self.assertIsNone(history.unoccupied_since)
        self.assertEqual(history.occupancy_samples, [])

    def test_adjacency_protects_a_neighbour_from_masked_cleaning_evidence(self):
        from custom_components.adaptive_robovacs.models import AdjacencyMode

        app = cleaning_application()
        app.state.floor_plan.edges = {("other", "study")}
        app.state.room_settings["other"].adjacency_mode = AdjacencyMode.ALWAYS
        transition(app, "cleaning", NOW)
        decision = app._adjacency_decision(app.discovery.rooms["other"], NOW)
        self.assertEqual(decision.blockers[0].area_id, "study")

    def test_preexisting_human_occupancy_is_preserved_in_cleaned_and_home_rooms(self):
        app = cleaning_application()
        for area in ("study", "dock"):
            history = app.state.room_history[area]
            history.occupancy = "occupied"
            history.unoccupied_since = None
            set_occupancy(app, area, "on")
        transition(app, "cleaning", NOW)
        transition(app, "docked", NOW + timedelta(minutes=30))
        for area in ("study", "dock"):
            history = app.state.room_history[area]
            self.assertEqual(history.occupancy, "occupied")
            self.assertEqual(history.occupancy_attribution.classification, "human")
            self.assertEqual(history.occupancy_samples, [])

    def test_pause_error_unavailability_and_idle_censor_without_manufactured_arrival(
        self,
    ):
        for state in ("paused", "error", "unavailable", "idle", None):
            with self.subTest(state=state):
                app = cleaning_application()
                transition(app, "cleaning", NOW)
                set_occupancy(app, "study", "on")
                app._observe_occupancy(NOW + timedelta(minutes=1))
                transition(app, state, NOW + timedelta(minutes=2))
                history = app.state.room_history["study"]
                self.assertEqual(history.occupancy, "occupied")
                self.assertIsNone(history.unoccupied_since)
                self.assertEqual(history.occupancy_samples, [])
                self.assertEqual(
                    app.state.cleaning_occupancy_sessions["registry-alpha"].areas, ()
                )
                transition(app, "docked", NOW + timedelta(minutes=3))
                self.assertEqual(
                    app.state.room_history["dock"].occupancy_attribution.classification,
                    "dock_cooldown",
                )

    def test_sensor_loss_and_replacement_never_restore_old_anchor(self):
        for raw, source in (("unavailable", "radars"), ("on", "motion_fallback")):
            with self.subTest(raw=raw, source=source):
                app = cleaning_application()
                transition(app, "cleaning", NOW)
                app._observe_attributed_room(
                    app.discovery.rooms["study"],
                    RoomObservation(
                        "unresolved" if raw == "unavailable" else "occupied", source
                    ),
                    NOW + timedelta(minutes=1),
                )
                set_occupancy(app, "study", "on")
                transition(app, "docked", NOW + timedelta(minutes=30))
                history = app.state.room_history["study"]
                self.assertIsNone(history.unoccupied_since)
                self.assertEqual(history.occupancy_samples, [])

    def test_overlapping_robots_resume_only_at_last_boundary(self):
        app = cleaning_application()
        beta = robot("vacuum.beta", registry_id="registry-beta")
        app.discovery = replace(
            app.discovery,
            robots=MappingProxyType({**app.discovery.robots, beta.entity_id: beta}),
        )
        transition(app, "cleaning", NOW)
        transition(
            app,
            "cleaning",
            NOW + timedelta(seconds=10),
            entity=beta.entity_id,
            areas=("study",),
        )
        set_occupancy(app, "study", "on")
        set_occupancy(app, "dock", "on")
        app._observe_occupancy(NOW + timedelta(seconds=20))
        first = NOW + timedelta(minutes=10)
        last = first + timedelta(minutes=1)
        transition(app, "docked", first)
        self.assertEqual(app.state.room_history["study"].occupancy, "unoccupied")
        transition(app, "docked", last, entity=beta.entity_id)
        self.assertEqual(
            app.state.room_history["study"].occupancy_attribution.started_at, last
        )
        app._observe_occupancy(first + timedelta(minutes=3))
        self.assertEqual(app.state.room_history["dock"].occupancy, "unoccupied")
        app._observe_occupancy(last + timedelta(minutes=3))
        self.assertEqual(
            app.state.room_history["dock"].occupancy_attribution.started_at,
            last + timedelta(minutes=3),
        )

    def test_immediate_new_departure_keeps_previous_cooldown_without_extending_it(self):
        app = cleaning_application()
        transition(app, "cleaning", NOW)
        transition(app, "docked", NOW + timedelta(minutes=10))
        transition(app, "cleaning", NOW + timedelta(minutes=10, seconds=20))
        set_occupancy(app, "dock", "on")
        app._observe_occupancy(NOW + timedelta(minutes=11))
        app._observe_occupancy(NOW + timedelta(minutes=13))
        self.assertEqual(
            app.state.room_history["dock"].occupancy_attribution.started_at,
            NOW + timedelta(minutes=13),
        )
        self.assertEqual(
            app.state.room_history["study"].occupancy_attribution.classification,
            "cleaning",
        )

    def test_external_unknown_room_does_not_mask_the_floor_but_dock_cools(self):
        app = cleaning_application()
        app.state.active_jobs["registry-alpha"] = None
        transition(app, "cleaning", NOW)
        set_occupancy(app, "study", "on")
        app._observe_occupancy(NOW + timedelta(minutes=1))
        self.assertEqual(app.state.room_history["study"].occupancy, "occupied")
        transition(app, "docked", NOW + timedelta(minutes=5))
        self.assertEqual(
            app.state.room_history["dock"].occupancy_attribution.classification,
            "dock_cooldown",
        )

    async def test_external_heartbeats_are_passive_and_stale_evidence_is_censored(
        self,
    ):
        app = cleaning_application()
        app.state.active_jobs["registry-alpha"] = None
        app.observer.location_sources["registry-alpha"] = Mock()
        app.observer.attribution_areas = frozenset({"study"})
        app.async_execute = app._async_execute_command
        transition(app, "cleaning", NOW)
        app._receive_robot_location(
            RobotLocation("registry-alpha", "study", NOW, True), NOW
        )
        set_occupancy(app, "study", "on")
        app._observe_occupancy(NOW + timedelta(seconds=1))
        with patch(
            "custom_components.adaptive_robovacs.application.core._now",
            return_value=NOW + timedelta(seconds=30),
        ):
            await app._async_handle_robot_location(
                RobotLocation(
                    "registry-alpha", "study", NOW + timedelta(seconds=30), True
                )
            )
        app.async_evaluate.assert_not_awaited()
        app._notify_listeners.assert_not_called()
        app._observe_occupancy(NOW + timedelta(seconds=211))
        history = app.state.room_history["study"]
        self.assertEqual(history.occupancy, "occupied")
        self.assertIsNone(history.unoccupied_since)
        self.assertEqual(history.occupancy_samples, [])

    def test_coverage_blocks_preview_and_final_dispatch_even_with_program_exemption(
        self,
    ):
        app = cleaning_application()
        candidate, _ = app._room_candidate(app.discovery.rooms["study"], NOW)
        transition(app, "cleaning", NOW)
        app._run_occupancy_exempt = Mock(return_value=True)
        self.assertEqual(
            app._room_candidate(app.discovery.rooms["study"], NOW)[1],
            "robot cleaning session in room",
        )
        self.assertEqual(
            app._dispatch_adjacency_block_reason(candidate, NOW),
            "robot cleaning session in room",
        )

    async def test_queued_robot_boundaries_use_frozen_sensor_inputs(self):
        app = cleaning_application()
        app._sync_dock_water_checks = Mock(return_value=False)
        app._reset_room_recovery_dock = Mock()
        # The latest live sensor is already clear; queued commands describe
        # the earlier occupied signal at docking and its subsequent clear.
        for old, new, at, raw in (
            ("docked", "cleaning", NOW, "unoccupied"),
            ("cleaning", "docked", NOW + timedelta(minutes=30), "occupied"),
        ):
            await app._async_execute_command(
                StateChangedCommand(
                    "vacuum.alpha",
                    old,
                    new,
                    at,
                    occupancy_observations=(("study", RoomObservation(raw, "radars")),),
                    cleaning_room_ids=("study",),
                )
            )
        history = app.state.room_history["study"]
        self.assertEqual(
            history.occupancy_attribution.started_at, NOW + timedelta(minutes=30)
        )
        await app._async_execute_command(
            StateChangedCommand(
                "binary_sensor.study_radar",
                "on",
                "off",
                NOW + timedelta(minutes=31),
                occupancy_observations=(
                    ("study", RoomObservation("unoccupied", "radars")),
                ),
            )
        )
        self.assertEqual(history.unoccupied_since, NOW + timedelta(minutes=31))

    def test_registry_removal_floor_or_home_changes_censor_retained_masks(self):
        for change in ("remove", "floor", "home", "room"):
            with self.subTest(change=change):
                app = cleaning_application()
                transition(app, "cleaning", NOW)
                robots = dict(app.discovery.robots)
                if change == "remove":
                    robots.clear()
                else:
                    robots["vacuum.alpha"] = replace(
                        robots["vacuum.alpha"],
                        **(
                            {"floor_id": "upper"}
                            if change == "floor"
                            else {"dock_area_id": "other"}
                            if change == "home"
                            else {}
                        ),
                    )
                rooms = dict(app.discovery.rooms)
                if change == "room":
                    rooms["study"] = replace(rooms["study"], floor_id="upper")
                app.discovery = DiscoverySnapshot(
                    MappingProxyType(robots), MappingProxyType(rooms)
                )
                app._prune_cleaning_occupancy()
                self.assertEqual(app.state.cleaning_occupancy_sessions, {})
                self.assertIsNone(app.state.room_history["study"].unoccupied_since)

    def test_restart_keeps_only_checkpointed_cooldown_and_discards_vacancy_anchor(self):
        app = cleaning_application()
        transition(app, "cleaning", NOW)
        app._restore_cleaning_occupancy(NOW + timedelta(minutes=1))
        self.assertEqual(app.state.cleaning_occupancy_sessions, {})
        self.assertIsNone(app.state.room_history["study"].unoccupied_since)
        app.state.cleaning_occupancy_sessions["registry-alpha"] = (
            CleaningOccupancySession(
                NOW,
                ("study",),
                "dock",
                NOW + timedelta(minutes=2),
                "job",
                cooldown_until=NOW + timedelta(minutes=5),
            )
        )
        app._restore_cleaning_occupancy(NOW + timedelta(minutes=3))
        self.assertIn("registry-alpha", app.state.cleaning_occupancy_sessions)
        app.hass.states.values["vacuum.alpha"].state = "docked"
        set_occupancy(app, "dock", "on")
        app._observe_occupancy(NOW + timedelta(minutes=3))
        app._observe_occupancy(NOW + timedelta(minutes=5))
        self.assertEqual(
            app.state.room_history["dock"].occupancy_attribution.started_at,
            NOW + timedelta(minutes=5),
        )
        self.assertEqual(app.state.room_history["dock"].occupancy_samples, [])


class CleaningOccupancyStoreTests(unittest.TestCase):
    def test_session_and_mask_round_trip_and_schema_22_migration_preserve_other_state(
        self,
    ):
        app = cleaning_application()
        transition(app, "cleaning", NOW)
        payload = app.state.to_store()
        restored, migrated = SchedulerState.from_store(payload, ENTRY_DATA)
        self.assertFalse(migrated)
        self.assertEqual(
            restored.cleaning_occupancy_sessions, app.state.cleaning_occupancy_sessions
        )
        self.assertEqual(
            restored.room_history["study"].occupancy_attribution,
            app.state.room_history["study"].occupancy_attribution,
        )
        payload["schema_version"] = 22
        payload.pop("cleaning_occupancy_sessions")
        restored, migrated = SchedulerState.from_store(payload, ENTRY_DATA)
        self.assertTrue(migrated)
        self.assertEqual(restored.cleaning_occupancy_sessions, {})
        self.assertEqual(restored.active_jobs, app.state.active_jobs)
        self.assertEqual(
            restored.room_history["study"].cleaning_completed_at,
            app.state.room_history["study"].cleaning_completed_at,
        )

    def test_malformed_session_checkpoints_fail_closed(self):
        record = encode_cleaning_occupancy_session(
            CleaningOccupancySession(NOW, ("study",), "dock", source="job")
        )
        for changes in (
            {"started_at": None},
            {"areas": [None]},
            {"areas": ["study", "study"]},
            {"areas": "study"},
            {"source": "guess"},
            {"dock_area_id": 3},
            {"docked_at": (NOW - timedelta(seconds=1)).isoformat()},
            {"location_expires_at": NOW.isoformat()},
            {"source": "location"},
            {"cooldown_until": NOW.isoformat(), "dock_area_id": None},
        ):
            with self.subTest(changes=changes), self.assertRaises(StateSchemaError):
                decode_cleaning_occupancy_sessions(
                    {"registry-alpha": {**record, **changes}}
                )
        for records in ([], {"": record}, {str(index): record for index in range(129)}):
            with self.assertRaises(StateSchemaError):
                decode_cleaning_occupancy_sessions(records)


if __name__ == "__main__":
    unittest.main()
