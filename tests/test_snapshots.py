"""Tests for immutable, typed integration projections."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from types import MappingProxyType, SimpleNamespace

from custom_components.adaptive_robovacs import projections
from custom_components.adaptive_robovacs.discovery import (
    DiscoveredOccupancySource,
    DiscoveredRoom,
    DiscoverySnapshot,
    RobotProfile,
)
from custom_components.adaptive_robovacs.models import (
    AdapterCapabilities,
    CleaningProgram,
)
from custom_components.adaptive_robovacs.projections import (
    build_snapshot,
    floor_plan_view,
)
from custom_components.adaptive_robovacs.snapshots import (
    FrozenJsonObject,
    ObservedProfileView,
    RobotSettingsView,
    RobotView,
    thaw_json,
)
from custom_components.adaptive_robovacs.state import (
    ActiveJob,
    CleaningOccurrence,
    CleaningStage,
    FloorPlanRectangle,
    FloorPlanSensorMarker,
    ManualAuditRecord,
    RobotHold,
    SchedulerFault,
    SchedulerState,
)

ENTRY_DATA = {
    "observe_only": True,
    "forecast_confidence": 75,
    "unresolved_start": "00:00",
    "unresolved_end": "04:00",
}


class _States:
    @staticmethod
    def get(entity_id):
        values = {"binary_sensor.z": "off", "binary_sensor.a": "on"}
        value = values.get(entity_id)
        return SimpleNamespace(state=value) if value else None


class _Source:
    def __init__(self) -> None:
        self.hass = SimpleNamespace(states=_States())
        self.state = SchedulerState.create(ENTRY_DATA)
        self.discovery = DiscoverySnapshot.empty()
        self.observe_only = True
        self.party_mode = False
        self.scheduler_halted = False
        self.scheduler_limited = False
        self.storage_safe_mode = False

    def get_global_setting(self, key):
        return getattr(self.state.global_settings, key)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_is_frozen_equality_comparable_and_mapping_free(self) -> None:
        source = _Source()
        source.state.evaluation.last_preview = (
            source.state.evaluation.last_preview.from_mapping(
                {"nested": {"items": [1, 2]}, "mode": "preview"}
            )
        )

        first = build_snapshot(source)
        second = build_snapshot(source)

        self.assertEqual(first, second)
        self.assertEqual(
            thaw_json(first.scheduler.preview),
            {"mode": "preview", "nested": {"items": [1, 2]}},
        )
        with self.assertRaises(FrozenInstanceError):
            first.scheduler.party_mode = True
        with self.assertRaises(TypeError):
            first.scheduler.preview["new"] = "value"

    def test_floor_plan_projection_has_stable_sorting_and_orphans(self) -> None:
        source = _Source()
        alpha_sensor = DiscoveredOccupancySource(
            "sensor-alpha", "binary_sensor.a", "radar"
        )
        zulu_sensor = DiscoveredOccupancySource(
            "sensor-zulu", "binary_sensor.z", "fallback"
        )
        rooms = {
            "zulu": DiscoveredRoom(
                "zulu",
                "Zulu",
                "upper",
                frozenset(),
                occupancy_sources=(zulu_sensor,),
            ),
            "alpha": DiscoveredRoom(
                "alpha",
                "Alpha",
                "ground",
                frozenset(),
                occupancy_sources=(alpha_sensor,),
            ),
        }
        source.discovery = DiscoverySnapshot(
            MappingProxyType({}), MappingProxyType(rooms)
        )
        source.state.floor_plan.revision = 3
        source.state.floor_plan.rooms = {
            "alpha": FloorPlanRectangle("ground", 0, 0, 10, 10),
            "removed": FloorPlanRectangle("ground", 20, 0, 10, 10),
        }
        source.state.floor_plan.sensors = {
            "sensor-alpha": FloorPlanSensorMarker("alpha", 5, 5),
            "sensor-removed": FloorPlanSensorMarker("removed", 25, 5),
        }

        view = floor_plan_view(source)

        self.assertEqual(
            tuple(floor.floor_id for floor in view.floors), ("ground", "upper")
        )
        self.assertEqual(view.floors[0].rooms[0].area_id, "alpha")
        self.assertEqual(view.floors[0].rooms[0].sensors[0].state, "active")
        self.assertEqual(view.orphaned_rooms, ("removed",))
        self.assertEqual(view.orphaned_sensors, ("sensor-removed",))

    def test_frozen_json_order_does_not_depend_on_input_order(self) -> None:
        left = FrozenJsonObject.from_mapping({"z": 1, "a": 2})
        right = FrozenJsonObject.from_mapping({"a": 2, "z": 1})
        self.assertEqual(left, right)
        self.assertEqual(tuple(left), ("a", "z"))

    def test_frozen_json_mapping_and_scalar_boundaries_are_total(self) -> None:
        when = datetime(2026, 9, 5, 10, tzinfo=UTC)
        value = FrozenJsonObject.from_mapping(
            {
                "enum": CleaningProgram.VACUUM_ONLY,
                "when": when,
                "custom": SimpleNamespace(name="opaque"),
            }
        )

        self.assertEqual(len(value), 3)
        self.assertEqual(value["enum"], "vacuum_only")
        self.assertEqual(value["when"], when.isoformat())
        self.assertIn("opaque", value["custom"])
        with self.assertRaises(KeyError):
            value["missing"]

    def test_robot_capability_properties_remain_typed_and_sorted(self) -> None:
        capabilities = AdapterCapabilities(
            adapter_id="fake",
            schema_version=2,
            portable_area_clean=True,
            supported_pass_counts=frozenset({1, 2}),
            supported_operations=frozenset({"mop", "vacuum"}),
            fan_speed_options=("quiet", "max"),
            mode_options=("vacuum", "mop"),
            mop_mode_options=("standard", "deep"),
            mop_intensity_options=("low", "high"),
            cleaning_depth_options=("daily", "fine"),
            vacuum_pass_counts=frozenset({2, 1}),
            mop_pass_counts=frozenset({2, 1}),
            native_mop_profile=True,
        )
        profile = RobotProfile(
            mode_select_entity_id="select.mode",
            mop_mode_select_entity_id="select.route",
            mop_intensity_select_entity_id="select.water",
        )
        robot = RobotView(
            registry_id="registry-alpha",
            entity_id="vacuum.alpha",
            unique_fragment="vacuum.alpha",
            name="Alpha",
            floor_id="ground",
            state="docked",
            battery=90,
            ready=True,
            reason="ready",
            active=None,
            scheduler_hold=None,
            active_room=None,
            active_rooms=(),
            profile=profile,
            adapter_id="fake",
            adapter_schema_version=2,
            adapter_capabilities=capabilities,
            adapter_diagnostic=None,
            failure=None,
            settings=RobotSettingsView(
                enabled=True,
                minimum_battery=80,
                cleaning_program=CleaningProgram.VACUUM_THEN_MOP,
                double_pass=False,
                mop_double_pass=False,
                mode=None,
                mop_mode=None,
                mop_intensity=None,
                fan_speed=None,
                cleaning_depth=None,
                cleaning_depth_configured=False,
                direct_custom_mop_migrated=False,
            ),
            observed_profile=ObservedProfileView(None, None, None, None, None),
            mop_profile_summary=None,
        )

        self.assertEqual(robot.supported_operations, ("mop", "vacuum"))
        self.assertEqual(robot.vacuum_pass_counts, (1, 2))
        self.assertEqual(robot.mop_pass_counts, (1, 2))
        self.assertEqual(robot.fan_speed_options, ("quiet", "max"))
        self.assertEqual(robot.mode_options, ("vacuum", "mop"))
        self.assertEqual(robot.mop_mode_options, ("standard", "deep"))
        self.assertEqual(robot.mop_intensity_options, ("low", "high"))
        self.assertEqual(robot.cleaning_depth_options, ("daily", "fine"))
        self.assertTrue(robot.native_mop_profile)
        self.assertTrue(robot.mode_select_available)
        self.assertTrue(robot.mop_mode_select_available)
        self.assertTrue(robot.mop_intensity_select_available)
        self.assertTrue(robot.settings.mopping_enabled)

    def test_snapshot_lookups_and_global_settings_cover_hits_and_misses(self) -> None:
        snapshot = build_snapshot(_Source())
        scheduler = snapshot.scheduler
        expected = {
            "observe_only": True,
            "party_mode": False,
            "forecast_confidence": 75,
            "unresolved_start": "00:00",
            "unresolved_end": "04:00",
        }
        for key, value in expected.items():
            with self.subTest(key=key):
                self.assertEqual(scheduler.global_setting(key), value)
        with self.assertRaises(KeyError):
            scheduler.global_setting("private")

        room = SimpleNamespace(area_id="study")
        robot = SimpleNamespace(entity_id="vacuum.alpha", registry_id="registry-alpha")
        populated = replace(snapshot, rooms=(room,), robots=(robot,))
        self.assertIs(populated.room("study"), room)
        self.assertIsNone(populated.room("missing"))
        self.assertIs(populated.robot_by_entity_id("vacuum.alpha"), robot)
        self.assertIsNone(populated.robot_by_entity_id("vacuum.missing"))
        self.assertIs(populated.robot_by_registry_id("registry-alpha"), robot)
        self.assertIsNone(populated.robot_by_registry_id("registry-missing"))

    def test_projection_helpers_resolve_only_current_registry_objects(self) -> None:
        when = datetime(2026, 9, 5, 10, tzinfo=UTC)
        active = ActiveJob(
            room_id="study",
            room_ids=["study"],
            operation="vacuum",
            phase="cleaning",
            source="scheduler",
        )
        hold = RobotHold("paused", "held", held_at=when)
        stage = CleaningStage("vacuum", 1, started_at=when)
        occurrence = CleaningOccurrence(
            "occurrence-1",
            "study",
            "registry-alpha",
            "vacuum.old",
            CleaningProgram.VACUUM_ONLY,
            [stage],
            when,
            when,
            "fake",
            1,
        )
        robot = SimpleNamespace(
            registry_id="registry-alpha", entity_id="vacuum.alpha", name="Alpha"
        )
        room = SimpleNamespace(area_id="study", name="Study")
        source = SimpleNamespace(
            robot_for_registry_id=lambda registry_id: (
                robot if registry_id == "registry-alpha" else None
            ),
            discovery=SimpleNamespace(rooms={"study": room}),
        )

        self.assertIsNone(projections._active_job_view(None))
        self.assertEqual(projections._active_job_view(active).room_id, "study")
        self.assertIsNone(projections._hold_view(None))
        self.assertEqual(projections._hold_view(hold).reason, "paused")
        self.assertEqual(projections._stage_view(stage).operation, "vacuum")
        self.assertIsNone(projections._occurrence_view(source, None))
        self.assertEqual(
            projections._occurrence_view(source, occurrence).robot_entity_id,
            "vacuum.alpha",
        )

        audit = ManualAuditRecord(
            at=when,
            robot_registry_id="registry-alpha",
            room_ids=("study",),
            operations=("vacuum",),
        )
        self.assertIsNone(projections._manual_audit_view(source, None))
        self.assertEqual(
            projections._manual_audit_view(source, audit).robot_entity_id,
            "vacuum.alpha",
        )
        without_robot = replace(audit, robot_registry_id=None)
        self.assertIsNone(
            projections._manual_audit_view(source, without_robot).robot_entity_id
        )

        fault = SchedulerFault("failed", "registry-alpha", "study", when, "dispatch")
        projected_fault = projections._fault_view(source, fault)
        self.assertEqual(projected_fault.robot_name, "Alpha")
        self.assertEqual(projected_fault.room_name, "Study")

    def test_snapshot_rejects_non_numeric_forecast_confidence(self) -> None:
        source = _Source()
        source.get_global_setting = lambda key: (
            "high" if key == "forecast_confidence" else "00:00"
        )
        with self.assertRaisesRegex(TypeError, "forecast_confidence"):
            build_snapshot(source)


if __name__ == "__main__":
    unittest.main()
