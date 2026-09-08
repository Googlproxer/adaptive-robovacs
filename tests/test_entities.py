"""Behavioral tests for CoordinatorEntity presentation and typed actions."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from custom_components.adaptive_robovacs import button, number, select, sensor
from custom_components.adaptive_robovacs import switch as switch_platform
from custom_components.adaptive_robovacs.commands import (
    EvaluateCommand,
    ManualCleanRoomCommand,
    SetGlobalCommand,
    SetRobotSettingCommand,
    SetRoomSettingCommand,
)
from custom_components.adaptive_robovacs.entity import async_setup_dynamic_entities
from custom_components.adaptive_robovacs.models import (
    AdapterCapabilities,
    AdjacencyMode,
    CleaningOperation,
    CleaningProgram,
    JobPhase,
    OccurrenceSource,
    WaterReadiness,
)
from custom_components.adaptive_robovacs.planner import VacancyDiagnostic
from custom_components.adaptive_robovacs.snapshots import (
    CandidateView,
    FaultView,
    FloorPlanView,
    FrozenJsonObject,
    ObservedProfileView,
    RobotSettingsView,
    RoomRecoveryView,
    SchedulerView,
)

WHEN = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def scheduler_view() -> SchedulerView:
    plan = FloorPlanView(0, (), (), (), ())
    return SchedulerView(
        observe_only=True,
        party_mode=False,
        scheduler_halted=False,
        scheduler_limited=False,
        storage_safe_mode=False,
        forecast_confidence=75,
        unresolved_start="00:00",
        unresolved_end="04:00",
        last_evaluation_at=WHEN,
        preview=FrozenJsonObject.from_mapping({"mode": "preview"}),
        robot_faults=(),
        room_faults=(),
        floor_plan=plan,
        failure=None,
        room_recoveries=(),
    )


def robot_view(
    entity_id: str = "vacuum.alpha",
    name: str = "Alpha",
):
    capabilities = AdapterCapabilities(
        adapter_id="fake",
        schema_version=2,
        portable_area_clean=True,
        supported_pass_counts=frozenset({1, 2}),
        native_area_pass_counts=frozenset({2}),
        supported_operations=frozenset({"vacuum", "mop"}),
        fan_speed_options=("quiet", "max"),
        mode_options=("vacuum", "mop_only"),
        mop_mode_options=("standard",),
        mop_intensity_options=("medium",),
        cleaning_depth_options=("daily",),
        water_readiness=WaterReadiness(
            "ready", "Water is ready", ready=True, authoritative=True
        ),
        vacuum_pass_counts=frozenset({1, 2}),
        mop_pass_counts=frozenset({1, 2}),
        native_mop_profile=True,
    )
    settings = RobotSettingsView(
        enabled=True,
        minimum_battery=80,
        cleaning_program=CleaningProgram.VACUUM_THEN_MOP,
        double_pass=True,
        mop_double_pass=False,
        mode="vacuum",
        mop_mode="standard",
        mop_intensity="medium",
        fan_speed="max",
        cleaning_depth="daily",
        cleaning_depth_configured=True,
        direct_custom_mop_migrated=True,
    )
    return SimpleNamespace(
        registry_id="registry-alpha",
        entity_id=entity_id,
        unique_fragment="legacy-alpha",
        name=name,
        floor_id="ground",
        state="docked",
        battery=95,
        ready=True,
        reason="ready",
        active=None,
        scheduler_hold=None,
        active_room=None,
        active_rooms=(),
        adapter_id="fake",
        adapter_schema_version=2,
        adapter_capabilities=capabilities,
        adapter_diagnostic=None,
        settings=settings,
        observed_profile=ObservedProfileView(
            "max", "vacuum", "standard", "medium", "2"
        ),
        mop_profile_summary="native mop-only profile",
        failure=None,
        supported_operations=("mop", "vacuum"),
        fan_speed_options=capabilities.fan_speed_options,
        mode_options=capabilities.mode_options,
        mop_mode_options=capabilities.mop_mode_options,
        mop_intensity_options=capabilities.mop_intensity_options,
        cleaning_depth_options=capabilities.cleaning_depth_options,
        native_mop_profile=True,
        mode_select_available=True,
        mop_mode_select_available=True,
        mop_intensity_select_available=True,
    )


def room_view(area_id: str = "study", name: str = "Study"):
    candidate = CandidateView(
        area_id,
        CleaningOperation.VACUUM,
        WHEN,
        0.9,
        "due",
        20,
        2,
        False,
        OccurrenceSource.SCHEDULER,
    )
    return SimpleNamespace(
        area_id=area_id,
        name=name,
        floor_id="ground",
        bedroom=False,
        radar_entity_ids=("binary_sensor.study_radar",),
        fallback_entity_ids=("binary_sensor.study_motion",),
        cleaning_period="Default",
        cleaning_profile="Custom",
        adjacency_mode=AdjacencyMode.NIGHT_ONLY,
        adjacency_active=False,
        adjacent_area_ids=(),
        adjacency_blockers=(),
        adjacency_reason=None,
        enabled=True,
        cleaning_interval=72,
        expected_minutes=20,
        ignore_desired_window=False,
        desired_window_configured_start=None,
        desired_window_configured_end="18:00",
        desired_window_effective_start="08:00",
        desired_window_effective_end="18:00",
        desired_window_start_inherited=True,
        desired_window_end_inherited=False,
        desired_window_valid=True,
        vacuum_pass_count=2,
        mop_pass_count=1,
        cleaning_program=CleaningProgram.VACUUM_THEN_MOP,
        fan_speed="max",
        mode="vacuum",
        mop_mode="standard",
        mop_intensity="medium",
        cleaning_depth="daily",
        effective_profiles=(),
        latest_manual_request=None,
        occupancy="unoccupied",
        occupancy_source="radar",
        unavailable_radars=0,
        last_cleaned=WHEN - timedelta(days=2),
        last_cleaned_display="2 days ago",
        using_initial_cadence_baseline=False,
        last_vacuum=WHEN - timedelta(days=2),
        last_mop=WHEN - timedelta(days=3),
        next_due=WHEN,
        next_clean_at=WHEN,
        robot_previews=(),
        desired_window_start=WHEN,
        next_candidate=candidate,
        assignment_available=True,
        robot_eligibility=(),
        active=None,
        active_robot=None,
        active_robot_state=None,
        effective_duration_minutes=20,
        duration_sample_count=4,
        predicted_total_minutes=22,
        required_vacancy_minutes=22,
        duration_model_version=2,
        duration_model_learned=True,
        duration_estimates_by_robot=(),
        block_reason="ready",
        vacancy_diagnostic=VacancyDiagnostic(
            "radar", WHEN - timedelta(hours=1), 22, 60, 0.9, 4, 4, "clear", True
        ),
        latest_scheduler_decision=None,
        legacy_deferral_review_needed=False,
        map_status="ready",
        map_error=None,
        occurrence=None,
        water_confirmation=None,
        last_stage_outcome="completed",
        last_stage_reason="observed",
        last_stage_at=WHEN,
        last_stage_summary="Vacuum completed",
        water_notification_episode=None,
        failure=None,
        recovery=None,
    )


class _Snapshot:
    def __init__(self) -> None:
        self.scheduler = scheduler_view()
        self.rooms = (room_view(),)
        self.robots = (robot_view(),)

    def room(self, area_id):
        return next((item for item in self.rooms if item.area_id == area_id), None)

    def robot_by_entity_id(self, entity_id):
        return next(
            (item for item in self.robots if item.entity_id == entity_id),
            None,
        )

    def robot_by_registry_id(self, registry_id):
        return next(
            (item for item in self.robots if item.registry_id == registry_id),
            None,
        )


class _Coordinator:
    def __init__(self) -> None:
        self.entry = SimpleNamespace(entry_id="entry-1")
        self.data = _Snapshot()
        self.last_update_success = True
        self.commands = []
        self.hass = SimpleNamespace()

    async def async_execute(self, command):
        self.commands.append(command)
        return None


class EntityPresentationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.coordinator = _Coordinator()

    def test_every_platform_builds_coordinator_entities_with_stable_ids(self) -> None:
        groups = (
            button._entities(self.coordinator),
            number._entities(self.coordinator),
            select._entities(self.coordinator),
            sensor._entities(self.coordinator),
            switch_platform._entities(self.coordinator),
        )
        entities = [entity for group in groups for entity in group]

        self.assertTrue(entities)
        self.assertTrue(all(isinstance(item, CoordinatorEntity) for item in entities))
        for group in groups:
            self.assertEqual(
                len({item.unique_id for item in group}),
                len(group),
            )
        unique_ids = {item.unique_id for item in entities}
        self.assertNotIn("entry-1_global_hall_start", unique_ids)
        self.assertNotIn("entry-1_global_hall_end", unique_ids)
        self.assertIn("entry-1_robot_legacy-alpha_status", unique_ids)
        self.assertIn("entry-1_room_study_next_clean", unique_ids)

    def test_entity_follows_registry_rename_and_missing_objects_are_unavailable(
        self,
    ) -> None:
        entity = sensor._RobotStatusSensor(self.coordinator, "vacuum.alpha")
        self.coordinator.data.robots = (robot_view("vacuum.renamed", "Renamed"),)

        self.assertTrue(entity.available)
        self.assertEqual(entity.name, "Renamed status")
        self.assertEqual(
            entity.extra_state_attributes["robot_entity_id"],
            "vacuum.renamed",
        )

        self.coordinator.data.robots = ()
        self.assertFalse(entity.available)
        room_entity = sensor._RoomOccupancySensor(self.coordinator, "study", "Study")
        self.coordinator.data.rooms = ()
        self.assertFalse(room_entity.available)

    async def test_values_attributes_and_actions_come_from_typed_snapshot(self) -> None:
        scheduler_entity = sensor._SchedulerSensor(self.coordinator)
        robot_entity = sensor._RobotStatusSensor(self.coordinator, "vacuum.alpha")
        room_entity = sensor._RoomStatusSensor(self.coordinator, "study", "Study")
        occupancy = sensor._RoomOccupancySensor(self.coordinator, "study", "Study")
        last_cleaned = sensor._RoomLastCleanedSensor(self.coordinator, "study", "Study")

        self.assertEqual(scheduler_entity.native_value, "observe-only")
        self.assertEqual(robot_entity.native_value, "docked")
        self.assertEqual(robot_entity.extra_state_attributes["battery"], 95)
        self.assertEqual(room_entity.native_value, "Observe-only mode")
        self.assertEqual(room_entity.extra_state_attributes["operation"], "vacuum")
        self.assertEqual(occupancy.native_value, "unoccupied")
        self.assertEqual(last_cleaned.native_value, WHEN - timedelta(days=2))

        party = switch_platform._GlobalSwitch(
            self.coordinator, "party_mode", "Party mode"
        )
        robot_enabled = switch_platform._RobotSwitch(
            self.coordinator, "vacuum.alpha", "enabled", "enabled"
        )
        room_enabled = switch_platform._RoomSwitch(
            self.coordinator, "study", "enabled", "Study enabled"
        )
        self.assertFalse(party.is_on)
        self.assertTrue(robot_enabled.is_on)
        self.assertTrue(room_enabled.is_on)
        await party.async_turn_on()
        await robot_enabled.async_turn_off()
        await room_enabled.async_turn_off()
        self.assertIsInstance(self.coordinator.commands[-3], SetGlobalCommand)
        self.assertIsInstance(self.coordinator.commands[-2], SetRobotSettingCommand)
        self.assertIsInstance(self.coordinator.commands[-1], SetRoomSettingCommand)

        global_number = number._GlobalNumber(self.coordinator)
        robot_number = number._RobotNumber(self.coordinator, "vacuum.alpha")
        room_number = number._RoomNumber(
            self.coordinator, "study", "expected_minutes", "expected"
        )
        self.assertEqual(global_number.native_value, 75)
        self.assertEqual(robot_number.native_value, 80)
        self.assertEqual(room_number.native_value, 20)
        await global_number.async_set_native_value(85)
        self.assertEqual(self.coordinator.commands[-1].value, 85)

        global_time = select._TimeSelect(
            self.coordinator, "unresolved_start", "Desired window start"
        )
        room_time = select._RoomTimeSelect(
            self.coordinator, "study", "desired_window_start", "start"
        )
        program = select._RobotProgramSelect(self.coordinator, "vacuum.alpha")
        self.assertEqual(global_time.current_option, "00:00")
        self.assertEqual(room_time.current_option, "Use global")
        self.assertIn("Mop only", program.options)
        self.assertEqual(program.current_option, "Vacuum then mop")
        await global_time.async_select_option("09:00")
        self.assertIsInstance(self.coordinator.commands[-1], SetGlobalCommand)

        await button._PreviewButton(self.coordinator).async_press()
        await button._RoomManualCleanButton(
            self.coordinator,
            "study",
            "Study",
            "vacuum_only",
            "manual vacuum only",
        ).async_press()
        self.assertIsInstance(self.coordinator.commands[-2], EvaluateCommand)
        self.assertIsInstance(self.coordinator.commands[-1], ManualCleanRoomCommand)

    def test_dynamic_addition_deduplicates_existing_entities(self) -> None:
        added = []
        unloaders = []
        callbacks = []
        entry = SimpleNamespace(async_on_unload=unloaders.append)

        def connect(_hass, _signal, callback):
            callbacks.append(callback)
            return lambda: None

        with patch(
            "custom_components.adaptive_robovacs.entity.async_dispatcher_connect",
            side_effect=connect,
        ):
            async_setup_dynamic_entities(
                entry,
                lambda entities: added.extend(entities),
                self.coordinator,
                lambda: sensor._entities(self.coordinator),
            )

        initial_count = len(added)
        callbacks[0]("entry-1")
        self.assertEqual(len(added), initial_count)
        self.coordinator.data.rooms = (
            *self.coordinator.data.rooms,
            room_view("hall", "Hall"),
        )
        callbacks[0]("entry-1")
        self.assertEqual(len(added), initial_count + 5)
        self.assertEqual(len(unloaders), 1)

    async def test_selects_translate_every_option_and_submit_typed_values(self) -> None:
        room = self.coordinator.data.rooms[0]
        vacuum_passes = select._RoomPassSelect(
            self.coordinator, "study", "vacuum", "vacuum passes"
        )
        mop_passes = select._RoomPassSelect(
            self.coordinator, "study", "mop", "mop passes"
        )
        self.assertEqual(vacuum_passes.current_option, "2 passes")
        self.assertEqual(mop_passes.current_option, "1 pass")
        room.vacuum_pass_count = None
        self.assertEqual(vacuum_passes.current_option, "Robot default")
        for option, value in (
            ("Robot default", None),
            ("1 pass", 1),
            ("2 passes", 2),
        ):
            await vacuum_passes.async_select_option(option)
            self.assertEqual(self.coordinator.commands[-1].value, value)

        room_period = select._RoomCleaningPeriodSelect(
            self.coordinator, "study", "period"
        )
        room_profile = select._RoomCleaningProfileSelect(
            self.coordinator, "study", "profile"
        )
        self.assertEqual(room_period.current_option, "Default")
        self.assertEqual(room_profile.current_option, "Custom")
        await room_period.async_select_option("Weekly")
        await room_profile.async_select_option("Robot default")

        room_program = select._RoomProgramSelect(self.coordinator, "study", "program")
        self.assertIn("Mop then vacuum", room_program.options)
        self.assertEqual(room_program.current_option, "Vacuum then mop")
        await room_program.async_select_option("Robot default")
        self.assertIsNone(self.coordinator.commands[-1].value)

        robot = self.coordinator.data.robots[0]
        robot.adapter_capabilities = replace(
            robot.adapter_capabilities,
            supported_operations=frozenset({"vacuum"}),
        )
        robot.supported_operations = ("vacuum",)
        room.cleaning_program = CleaningProgram.MOP_ONLY
        self.assertEqual(room_program.options, ["Robot default", "Vacuum only"])
        self.assertEqual(room_program.current_option, "Robot default")
        robot_program = select._RobotProgramSelect(self.coordinator, "vacuum.alpha")
        self.assertEqual(robot_program.options, ["Vacuum only"])
        self.assertEqual(robot_program.current_option, "Vacuum only")
        await robot_program.async_select_option("Vacuum only")

    async def test_profile_selects_preserve_stale_values_and_filter_native_mop(
        self,
    ) -> None:
        robot = self.coordinator.data.robots[0]
        robot.settings = replace(robot.settings, mop_mode="smart_mode")
        mop_route = select._RobotSelect(
            self.coordinator,
            "vacuum.alpha",
            "mop_mode",
            robot.mop_mode_options,
            "route",
        )
        self.assertNotIn("smart_mode", mop_route.options)
        self.assertEqual(mop_route.current_option, "Not configured")

        robot.native_mop_profile = False
        robot.settings = replace(robot.settings, fan_speed="removed-but-saved")
        fan = select._RobotSelect(
            self.coordinator,
            "vacuum.alpha",
            "fan_speed",
            robot.fan_speed_options,
            "fan",
        )
        self.assertIn("removed-but-saved", fan.options)
        self.assertEqual(fan.current_option, "removed-but-saved")
        await fan.async_select_option("Not configured")
        self.assertIsNone(self.coordinator.commands[-1].value)
        await fan.async_select_option("max")
        self.assertEqual(self.coordinator.commands[-1].value, "max")

        room = self.coordinator.data.rooms[0]
        room.fan_speed = "legacy-room-value"
        for key in (
            "fan_speed",
            "mode",
            "mop_mode",
            "mop_intensity",
            "cleaning_depth",
        ):
            entity = select._RoomProfileSelect(
                self.coordinator, "study", "Study", key, key
            )
            self.assertGreater(len(entity.options), 1)
            self.assertIsNotNone(entity.current_option)
            await entity.async_select_option("Robot default")
            self.assertIsNone(self.coordinator.commands[-1].value)

    def test_sensor_states_cover_global_active_and_waiting_presentations(self) -> None:
        scheduler_entity = sensor._SchedulerSensor(self.coordinator)
        scheduler = self.coordinator.data.scheduler
        self.coordinator.data.scheduler = replace(scheduler, scheduler_limited=True)
        self.assertEqual(scheduler_entity.native_value, "Scheduler limited")
        self.coordinator.data.scheduler = replace(
            scheduler, observe_only=False, party_mode=True
        )
        self.assertEqual(scheduler_entity.native_value, "party mode")
        self.coordinator.data.scheduler = replace(
            scheduler, observe_only=False, party_mode=False
        )
        self.assertEqual(scheduler_entity.native_value, "ready")
        self.assertIn("last_evaluation", scheduler_entity.extra_state_attributes)

        self.coordinator.data.scheduler = replace(
            self.coordinator.data.scheduler, observe_only=False
        )
        room = self.coordinator.data.rooms[0]
        room_entity = sensor._RoomStatusSensor(self.coordinator, "study", "Study")
        room.next_candidate = None
        phases = (
            (JobPhase.RECOVERY_WAITING, "Completion pending"),
            (JobPhase.COMPLETION_HELD, "Completion pending"),
            (JobPhase.DOCK_COMPLETION_PENDING, "Dock servicing"),
            (JobPhase.CANCELLING, "Returning to dock"),
            (JobPhase.ERROR_WAITING, "Scheduler held"),
            (JobPhase.PAUSED, "Paused"),
            (JobPhase.CLEANING, "In Progress"),
        )
        for phase, expected in phases:
            room.active = SimpleNamespace(phase=phase)
            room.active_robot_state = "cleaning"
            self.assertEqual(room_entity.native_value, expected)
        room.active_robot_state = "returning"
        self.assertEqual(room_entity.native_value, "Returning")

        room.active = None
        room.enabled = False
        self.assertEqual(room_entity.native_value, "disabled")
        room.enabled = True
        room.block_reason = "not due"
        self.assertIsInstance(room_entity.native_value, str)
        room.block_reason = "waiting for desired cleaning window"
        self.assertIsInstance(room_entity.native_value, str)
        room.block_reason = "custom block"
        self.assertEqual(room_entity.native_value, "custom block")

    def test_sensor_attributes_serialize_optional_public_values(self) -> None:
        robot = self.coordinator.data.robots[0]
        robot.failure = FaultView(
            "blocked", "safe summary", WHEN, "dispatch", "Alpha", "Study"
        )
        status = sensor._RobotStatusSensor(self.coordinator, "vacuum.alpha")
        self.assertEqual(status.native_value, "Scheduler held")
        self.assertTrue(status.extra_state_attributes["repair_active"])
        robot.failure = None
        robot.adapter_capabilities = replace(
            robot.adapter_capabilities, water_readiness="legacy"
        )
        self.assertEqual(
            status.extra_state_attributes["water_readiness"]["status"],
            "legacy",
        )

        room = self.coordinator.data.rooms[0]
        cleaned = sensor._RoomLastCleanedSensor(self.coordinator, "study", "Study")
        room.last_vacuum = None
        room.last_mop = None
        self.assertIsNone(cleaned.extra_state_attributes["last_vacuum"])
        occupancy = sensor._RoomOccupancySensor(self.coordinator, "study", "Study")
        self.assertEqual(occupancy.extra_state_attributes["unavailable_radars"], 0)
        manual = sensor._RoomManualStatusSensor(self.coordinator, "study", "Study")
        self.assertEqual(manual.native_value, "never requested")
        room.latest_manual_request = SimpleNamespace(outcome="awaiting_confirmation")
        self.assertEqual(manual.native_value, "awaiting confirmation")

    def test_room_recovery_presentation_preserves_independent_mapping_fault(
        self,
    ) -> None:
        room = self.coordinator.data.rooms[0]
        room.recovery = RoomRecoveryView(
            "episode",
            "occurrence",
            1,
            CleaningOperation.MOP,
            WHEN,
            FaultView(
                "room_error_recovery",
                "The robot became trapped.",
                WHEN,
                "awaiting_confirmation",
                "Alpha",
                "Study",
            ),
        )
        room_entity = sensor._RoomStatusSensor(self.coordinator, "study", "Study")
        self.assertEqual(
            room_entity.native_value, "Room blocked — recovery confirmation required"
        )
        attributes = room_entity.extra_state_attributes
        self.assertTrue(attributes["repair_active"])
        self.assertEqual(
            attributes["room_recovery"]["failure"]["failure_phase"],
            "awaiting_confirmation",
        )
        room.failure = FaultView(
            "area_mapping_stale",
            "Check the mapping.",
            WHEN,
            "mapping",
            "Alpha",
            "Study",
        )
        attributes = room_entity.extra_state_attributes
        self.assertEqual(attributes["failure_code"], "area_mapping_stale")
        self.assertEqual(
            attributes["room_recovery"]["failure"]["failure_code"],
            "room_error_recovery",
        )

    async def test_each_platform_setup_uses_runtime_coordinator(self) -> None:
        entry = SimpleNamespace(
            runtime_data=SimpleNamespace(coordinator=self.coordinator)
        )
        for module in (button, number, select, sensor, switch_platform):
            with (
                self.subTest(module=module.__name__),
                patch.object(module, "async_setup_dynamic_entities") as setup,
            ):
                await module.async_setup_entry(SimpleNamespace(), entry, list)
                setup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
