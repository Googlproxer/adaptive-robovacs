"""Tests for application-owned settings, identity, topology, and faults."""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from custom_components.adaptive_robovacs.adapters.base import AdapterEntityEvidence
from custom_components.adaptive_robovacs.application import SchedulerApplication
from custom_components.adaptive_robovacs.discovery import (
    DiscoveredOccupancySource,
    DiscoveredRobot,
    DiscoveredRoom,
    DiscoverySnapshot,
    RobotProfile,
)
from custom_components.adaptive_robovacs.floor_plans import FloorPlanWrite
from custom_components.adaptive_robovacs.models import (
    AdapterCapabilities,
    CleaningOperation,
    CleaningProgram,
    FaultCode,
    JobPhase,
    JobSource,
    StageStatus,
    WaterReadiness,
)
from custom_components.adaptive_robovacs.state import (
    ActiveJob,
    CleaningOccurrence,
    CleaningStage,
    FloorPlanRectangle,
    ManualAuditRecord,
    OccupancySample,
    RecoveryAuditRecord,
    RobotCooldown,
    RobotHold,
    SchedulerFault,
    SchedulerState,
    WaterNotificationEpisode,
)

NOW = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
ENTRY_DATA = {
    "observe_only": False,
    "forecast_confidence": 75,
    "unresolved_start": "01:00",
    "unresolved_end": "05:00",
}


def room(
    area_id: str = "study",
    *,
    floor_id: str = "ground",
    radar_ids: tuple[str, ...] = ("binary_sensor.study_radar",),
) -> DiscoveredRoom:
    return DiscoveredRoom(
        area_id,
        area_id.replace("_", " ").title(),
        floor_id,
        frozenset(),
        radar_entity_ids=radar_ids,
        fallback_entity_ids=(f"binary_sensor.{area_id}_motion",),
        occupancy_sources=(
            DiscoveredOccupancySource(
                f"registry-{area_id}-radar",
                radar_ids[0] if radar_ids else f"binary_sensor.{area_id}_motion",
                "radar" if radar_ids else "fallback",
            ),
        ),
    )


def robot(
    entity_id: str = "vacuum.alpha",
    *,
    registry_id: str = "registry-alpha",
    floor_id: str = "ground",
    native_mop_profile: bool = False,
) -> DiscoveredRobot:
    profile = RobotProfile(
        battery_entity_id=f"sensor.{entity_id.split('.')[1]}_battery",
        cleaning_time_entity_id=f"sensor.{entity_id.split('.')[1]}_cleaning_time",
        mode_select_entity_id=f"select.{entity_id.split('.')[1]}_mode",
        mop_mode_select_entity_id=f"select.{entity_id.split('.')[1]}_route",
        mop_intensity_select_entity_id=f"select.{entity_id.split('.')[1]}_water",
        passes_select_entity_id=f"select.{entity_id.split('.')[1]}_passes",
    )
    return DiscoveredRobot(
        entity_id=entity_id,
        name=entity_id.split(".")[1].title(),
        registry_id=registry_id,
        platform="test_vendor",
        device_id=f"device-{registry_id}",
        dock_area_id="dock",
        floor_id=floor_id,
        supports_area_clean=True,
        supports_send_command=True,
        profile=profile,
        adapter_id="fake",
        adapter_schema_version=2,
        adapter_capabilities=AdapterCapabilities(
            adapter_id="fake",
            schema_version=2,
            portable_area_clean=True,
            supported_pass_counts=frozenset({1, 2}),
            supported_operations=frozenset({"vacuum", "mop"}),
            fan_speed_options=("quiet", "max", "max_plus"),
            mode_options=("vacuum_only", "mop_only"),
            mop_mode_options=("standard",),
            mop_intensity_options=("medium",),
            cleaning_depth_options=("daily", "deep"),
            water_readiness=WaterReadiness("ready", "ready", ready=True),
            watched_entity_ids=(f"sensor.{entity_id.split('.')[1]}_status",),
            native_mop_profile=native_mop_profile,
            readiness_entity_id=f"sensor.{entity_id.split('.')[1]}_status",
            mop_start_states=frozenset({"washing_the_mop"}),
        ),
        adapter_entities=(
            AdapterEntityEvidence(
                entity_id=f"select.{entity_id.split('.')[1]}_extra",
                domain="select",
                platform="test_vendor",
                translation_key=None,
                device_class=None,
                state="ready",
            ),
            AdapterEntityEvidence(
                entity_id=f"sensor.{entity_id.split('.')[1]}_ignored",
                domain="sensor",
                platform="test_vendor",
                translation_key=None,
                device_class=None,
                state="ready",
            ),
        ),
    )


class States:
    def __init__(self) -> None:
        self.values = {}

    def get(self, entity_id):
        return self.values.get(entity_id)


def state_application() -> SchedulerApplication:
    app = SchedulerApplication.__new__(SchedulerApplication)
    app.entry = SimpleNamespace(entry_id="entry-1", data=ENTRY_DATA)
    app.hass = SimpleNamespace(states=States())
    app.state = SchedulerState.create(ENTRY_DATA)
    discovered_room = room()
    discovered_robot = robot()
    app.discovery = DiscoverySnapshot(
        MappingProxyType({discovered_robot.entity_id: discovered_robot}),
        MappingProxyType({discovered_room.area_id: discovered_room}),
    )
    app.state.ensure_room("study", is_bedroom=False)
    app.state.ensure_robot("registry-alpha", supports_mopping=True)
    app.storage = SimpleNamespace(async_save=AsyncMock())
    app.repairs = SimpleNamespace(
        sync_retired_map_holds=Mock(),
        sync_unresolved_robot_references=Mock(),
        sync_dispatch_faults=Mock(),
        sync_two_pass_issues=Mock(),
        sync_cleaning_program_issues=Mock(),
        delete_robot_dispatch_fault=Mock(),
        delete_room_dispatch_fault=Mock(),
        sync_room_recoveries=Mock(),
        set_robot_error_recovery=Mock(),
        delete_robot_error_recovery=Mock(),
    )
    app._storage_safe_mode = False
    app._closing = False
    app._identity_migrated = False
    app._discovery_signal_pending = False
    app._startup_state_settle_until = None
    app._watch_entity_ids = set()
    app._start_confirmation_timers = {}
    app._ready_confirmation_timers = {}
    app._ready_since = {}
    app._room_recovery_since = {}
    app._room_recovery_timers = {}
    app._lock = asyncio.Lock()
    app._notify_listeners = Mock()
    app.async_evaluate = AsyncMock(return_value={})
    app.async_refresh_discovery = AsyncMock()
    app._async_create_task = Mock()
    return app


def active_job(*, occurrence_id: str | None = None) -> ActiveJob:
    return ActiveJob(
        room_id="study",
        room_ids=["study"],
        operation=CleaningOperation.VACUUM,
        phase=JobPhase.DISPATCHING,
        source=JobSource.SCHEDULER,
        occurrence_id=occurrence_id,
        stage_index=0 if occurrence_id else None,
    )


class ApplicationStateTests(unittest.IsolatedAsyncioTestCase):
    def test_retired_label_does_not_add_cross_room_or_daytime_gates(self) -> None:
        app = state_application()
        target = replace(room(), labels=frozenset({"robovac_bedroom_transit"}))
        bedroom = replace(room("bedroom"), labels=frozenset({"robovac_bedroom"}))
        app.discovery = DiscoverySnapshot(
            app.discovery.robots,
            MappingProxyType({target.area_id: target, bedroom.area_id: bedroom}),
        )
        bedroom_settings, bedroom_history = app.state.ensure_room(
            bedroom.area_id, is_bedroom=bedroom.is_bedroom
        )
        self.assertFalse(bedroom_settings.enabled)
        self.assertEqual(bedroom_settings.cleaning_interval, 168)
        bedroom_history.occupancy = "occupied"
        history = app.state.room_history[target.area_id]
        history.occupancy = "unoccupied"
        settings = app.state.room_settings[target.area_id]
        settings.desired_window_start = "22:00"
        settings.desired_window_end = "05:00"

        with patch(
            "custom_components.adaptive_robovacs.application_policy._local",
            side_effect=lambda value: value,
        ):
            for hour in (23, 2):
                with self.subTest(hour=hour):
                    candidate, reason = app._room_candidate(
                        target, NOW.replace(hour=hour)
                    )
                    self.assertIsNotNone(candidate, reason)
                    self.assertEqual(reason, "ready")
            history.occupancy = "occupied"
            candidate, reason = app._room_candidate(target, NOW.replace(hour=2))
            self.assertIsNone(candidate)
            self.assertIn("occupancy occupied", reason)

    def test_retired_label_uses_ordinary_unresolved_window_and_startup_policy(
        self,
    ) -> None:
        app = state_application()
        target = replace(room(), labels=frozenset({"robovac_bedroom_transit"}))
        settings = app.state.room_settings[target.area_id]
        settings.ignore_desired_window = True
        settings.desired_window_start = "22:00"
        settings.desired_window_end = "05:00"
        app.state.room_history[target.area_id].occupancy = "unresolved"

        with patch(
            "custom_components.adaptive_robovacs.application_policy._local",
            side_effect=lambda value: value,
        ):
            for hour, allowed in ((22, True), (2, True), (5, False), (12, False)):
                with self.subTest(hour=hour):
                    candidate, reason = app._room_candidate(
                        target, NOW.replace(hour=hour)
                    )
                    self.assertEqual(candidate is not None, allowed, reason)
                    if candidate is not None:
                        self.assertTrue(candidate.unresolved_window_allowed)
                    else:
                        self.assertEqual(
                            reason,
                            "unresolved occupancy; waiting for desired cleaning window",
                        )
            app._startup_state_settle_until = NOW + timedelta(days=1)
            candidate, reason = app._room_candidate(target, NOW.replace(hour=2))
            self.assertIsNone(candidate)
            self.assertEqual(reason, "awaiting Home Assistant state restoration")

    async def test_retired_global_setting_commands_are_rejected_without_saving(
        self,
    ) -> None:
        app = state_application()
        for key in ("hall_start", "hall_end"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "Unknown global setting"):
                    await app.async_set_global(key, "12:00")
                with self.assertRaisesRegex(ValueError, "Unknown global setting"):
                    app.get_global_setting(key)
        app.storage.async_save.assert_not_awaited()

    async def test_save_is_disabled_only_in_storage_safe_mode(self) -> None:
        app = state_application()
        await app._async_save()
        app.storage.async_save.assert_awaited_once_with(app.state)

        app._storage_safe_mode = True
        await app._async_save()
        self.assertEqual(app.storage.async_save.await_count, 1)

    async def test_discovery_refresh_rebinds_watchers_and_resets_changed_sources(
        self,
    ) -> None:
        app = state_application()
        prior_robot = robot(entity_id="vacuum.old_alpha")
        app.discovery = DiscoverySnapshot(
            MappingProxyType({prior_robot.entity_id: prior_robot}),
            MappingProxyType({"study": room(radar_ids=("binary_sensor.old",))}),
        )
        history = app.state.room_history["study"]
        history.source_fingerprint = "binary_sensor.old,|,binary_sensor.study_motion"
        history.occupancy = "unoccupied"
        history.unoccupied_since = NOW - timedelta(hours=1)
        history.occupancy_samples = [OccupancySample(NOW - timedelta(days=1), 30)]
        app.state.water_notification_episodes["study"] = WaterNotificationEpisode(
            "study", "water_low", NOW, NOW
        )
        new_snapshot = DiscoverySnapshot(
            MappingProxyType({"vacuum.alpha": robot()}),
            MappingProxyType({"study": room()}),
        )
        app._migrate_runtime_robot_identity = Mock(return_value=True)
        app._reconcile_unresolved_robot_references = Mock(return_value=False)
        app._sync_two_pass_issues = Mock()
        app._sync_cleaning_program_issues = Mock()
        app._notify_listeners = Mock()
        app.hass = object()

        with (
            patch(
                "custom_components.adaptive_robovacs.application.async_discover",
                AsyncMock(return_value=new_snapshot),
            ),
            patch(
                "custom_components.adaptive_robovacs.application.async_dispatcher_send"
            ) as dispatcher,
        ):
            await SchedulerApplication.async_refresh_discovery(app)

        self.assertEqual(history.occupancy, "unresolved")
        self.assertEqual(history.occupancy_source, "sources_changed")
        self.assertIsNone(history.unoccupied_since)
        self.assertEqual(history.occupancy_samples, [])
        self.assertTrue(app._identity_migrated)
        self.assertIn("vacuum.alpha", app._watch_entity_ids)
        self.assertIn("sensor.alpha_battery", app._watch_entity_ids)
        self.assertIn("select.alpha_extra", app._watch_entity_ids)
        self.assertNotIn("sensor.alpha_ignored", app._watch_entity_ids)
        self.assertNotIn("study", app.state.water_notification_episodes)
        dispatcher.assert_not_called()
        app._notify_listeners.assert_called_once()
        self.assertTrue(app._discovery_signal_pending)

        app._notify_listeners.reset_mock()
        app._discovery_signal_pending = False
        with (
            patch(
                "custom_components.adaptive_robovacs.application.async_discover",
                AsyncMock(return_value=new_snapshot),
            ),
            patch(
                "custom_components.adaptive_robovacs.application.async_dispatcher_send"
            ) as dispatcher,
        ):
            await SchedulerApplication.async_refresh_discovery(app, notify=False)
        dispatcher.assert_not_called()
        app._notify_listeners.assert_not_called()
        self.assertFalse(app._discovery_signal_pending)

    def test_discovery_signal_follows_snapshot_publication(self) -> None:
        app = state_application()
        published_snapshot = object()
        coordinator = SimpleNamespace(data=None)
        discovery_views = []
        app._snapshot = None
        app._listeners = {lambda update: setattr(coordinator, "data", update)}
        app._discovery_signal_pending = True

        def capture_discovery(_hass, _signal, _entry_id):
            discovery_views.append(coordinator.data)

        with (
            patch(
                "custom_components.adaptive_robovacs.application.build_snapshot",
                return_value=published_snapshot,
            ),
            patch(
                "custom_components.adaptive_robovacs.application.async_dispatcher_send",
                side_effect=capture_discovery,
            ) as dispatcher,
        ):
            SchedulerApplication._notify_listeners(app)

        self.assertIs(coordinator.data, published_snapshot)
        self.assertEqual(discovery_views, [published_snapshot])
        self.assertFalse(app._discovery_signal_pending)
        dispatcher.assert_called_once_with(
            app.hass,
            "adaptive_robovacs_discovery_updated",
            "entry-1",
        )

    def test_identity_resolution_quarantines_and_restores_every_owned_record(
        self,
    ) -> None:
        app = state_application()
        legacy_job = active_job()
        legacy_settings = app.state.robot_settings.pop("registry-alpha")
        app.state.robot_settings["vacuum.legacy"] = legacy_settings
        app.state.active_jobs["vacuum.legacy"] = legacy_job
        app.state.robot_holds["vacuum.legacy"] = RobotHold("paused", "held")
        legacy_cooldown = RobotCooldown(
            until=NOW + timedelta(minutes=5),
            cancelled_at=NOW,
        )
        app.state.robot_cooldowns["vacuum.legacy"] = legacy_cooldown
        occurrence = CleaningOccurrence(
            "occurrence-1",
            "study",
            "vacuum.legacy",
            "vacuum.legacy",
            CleaningProgram.VACUUM_ONLY,
            [CleaningStage(CleaningOperation.VACUUM, 1)],
            NOW,
            NOW,
            "fake",
            2,
        )
        app.state.occurrences["study"] = occurrence
        app.state.audit.manual_events.append(
            ManualAuditRecord(
                at=NOW,
                robot_registry_id="vacuum.legacy",
                room_ids=("study",),
                operations=(CleaningOperation.VACUUM,),
                source="manual_service",
            )
        )
        app.state.audit.recovery_events.append(
            RecoveryAuditRecord(
                at=NOW,
                robot_registry_id="vacuum.legacy",
                room_ids=("study",),
                reason="waiting",
            )
        )

        changed = app._reconcile_unresolved_robot_references()
        self.assertTrue(changed)
        self.assertIn("vacuum.legacy", app.state.unresolved_robot_references)
        self.assertNotIn("vacuum.legacy", app.state.robot_settings)
        quarantined = app.state.unresolved_robot_references["vacuum.legacy"]
        self.assertIs(quarantined.settings, legacy_settings)
        self.assertIs(quarantined.active_job, legacy_job)
        self.assertIs(quarantined.cooldown, legacy_cooldown)
        app.repairs.sync_unresolved_robot_references.assert_called_with(
            ("vacuum.legacy",)
        )

        changed = app._reconcile_unresolved_robot_references()
        self.assertFalse(changed)
        self.assertIs(
            app.state.unresolved_robot_references["vacuum.legacy"],
            quarantined,
        )
        self.assertIs(quarantined.settings, legacy_settings)
        self.assertIs(quarantined.active_job, legacy_job)
        self.assertIs(quarantined.cooldown, legacy_cooldown)

        app.state.robot_entity_aliases["registry-alpha"] = "vacuum.legacy"
        changed = app._reconcile_unresolved_robot_references()
        self.assertTrue(changed)
        self.assertNotIn("vacuum.legacy", app.state.unresolved_robot_references)
        self.assertIs(app.state.active_jobs["registry-alpha"], legacy_job)
        self.assertIs(app.state.robot_cooldowns["registry-alpha"], legacy_cooldown)
        self.assertEqual(occurrence.robot_registry_id, "registry-alpha")
        self.assertEqual(occurrence.robot_entity_id, "vacuum.alpha")
        self.assertEqual(
            app.state.audit.manual_events[0].robot_registry_id, "registry-alpha"
        )
        self.assertEqual(
            app.state.audit.recovery_events[0].robot_registry_id, "registry-alpha"
        )

    def test_settings_accessors_and_runtime_identity_are_bounded(self) -> None:
        app = state_application()
        discovered_robot = app.discovery.robots["vacuum.alpha"]
        app.state.robot_entity_aliases["registry-alpha"] = "vacuum.original"

        self.assertEqual(app.robot_unique_fragment("vacuum.alpha"), "vacuum.original")
        self.assertEqual(app.robot_unique_fragment("vacuum.missing"), "vacuum.missing")
        self.assertEqual(app.robot_registry_id("vacuum.alpha"), "registry-alpha")
        self.assertEqual(app.robot_registry_id("vacuum.missing"), "vacuum.missing")
        self.assertIs(app.robot_for_registry_id("registry-alpha"), discovered_robot)
        self.assertIsNone(app.robot_for_registry_id("missing"))
        self.assertFalse(app.observe_only)
        self.assertFalse(app.party_mode)
        self.assertFalse(app.scheduler_halted)
        self.assertFalse(app.storage_safe_mode)
        self.assertEqual(app.get_global_setting("forecast_confidence"), 75)
        self.assertFalse(app.get_global_setting("observe_only"))
        self.assertEqual(app.get_room_setting("study", "vacuum_interval"), 84)
        self.assertEqual(app.get_room_setting("study", "pass_count"), None)
        self.assertEqual(app.room_cleaning_period("study"), "Default")
        self.assertEqual(app.room_cleaning_profile("study"), "Robot default")

        app._storage_safe_mode = True
        self.assertTrue(app.observe_only)
        with self.assertRaisesRegex(ValueError, "Unknown global setting"):
            app.get_global_setting("private")
        for method in (
            lambda: app.get_room_setting("missing", "enabled"),
            lambda: app.get_room_setting("study", "private"),
            lambda: app.room_cleaning_period("missing"),
            lambda: app.room_cleaning_profile("missing"),
        ):
            with self.assertRaises(ValueError):
                method()

    async def test_setting_commands_validate_and_publish_preview(self) -> None:
        app = state_application()
        await app.async_set_global("party_mode", True)
        self.assertTrue(app.state.global_settings.party_mode)
        await app.async_set_global("unresolved_start", "02:00")
        self.assertEqual(app.state.global_settings.unresolved_start, "02:00")
        with self.assertRaises(ValueError):
            await app.async_set_global("unknown", True)
        with self.assertRaises(ValueError):
            await app.async_set_global("unresolved_end", "25:00")

        await app.async_set_room_cleaning_period("study", "Morning")
        await app.async_set_room_cleaning_profile("study", "Custom")
        await app.async_set_room_setting("study", "vacuum_interval", 48)
        await app.async_set_room_setting("study", "pass_count", 2)
        await app.async_set_room_setting("study", "fan_speed", "max")
        await app.async_set_room_setting("study", "desired_window_start", "03:00")
        room_settings = app.state.room_settings["study"]
        self.assertEqual(room_settings.cleaning_interval, 48)
        self.assertEqual(room_settings.vacuum_pass_count, 2)
        self.assertTrue(room_settings.profile_custom)
        for area_id, key, value in (
            ("missing", "enabled", True),
            ("study", "private", True),
            ("study", "vacuum_pass_count", 3),
            ("study", "cleaning_program", "steam"),
            ("study", "fan_speed", 5),
            ("study", "desired_window_end", "25:00"),
        ):
            with (
                self.subTest(area_id=area_id, key=key),
                self.assertRaises(ValueError),
            ):
                await app.async_set_room_setting(area_id, key, value)
        with self.assertRaises(ValueError):
            await app.async_set_room_cleaning_period("missing", "Daily")
        with self.assertRaises(ValueError):
            await app.async_set_room_cleaning_profile("missing", "Custom")

        await app.async_set_robot_setting("vacuum.alpha", "minimum_battery", 50)
        await app.async_set_robot_setting("vacuum.alpha", "mopping_enabled", False)
        await app.async_set_robot_setting("vacuum.alpha", "cleaning_depth", "deep")
        robot_settings = app.state.robot_settings["registry-alpha"]
        self.assertEqual(robot_settings.minimum_battery, 50)
        self.assertEqual(robot_settings.cleaning_program, CleaningProgram.VACUUM_ONLY)
        self.assertTrue(robot_settings.cleaning_depth_configured)
        for entity_id, key, value in (
            ("missing", "enabled", True),
            ("vacuum.alpha", "private", True),
            ("vacuum.alpha", "cleaning_program", "steam"),
            ("vacuum.alpha", "fan_speed", 3),
        ):
            with (
                self.subTest(entity_id=entity_id, key=key),
                self.assertRaises(ValueError),
            ):
                await app.async_set_robot_setting(entity_id, key, value)
        native = robot(
            entity_id="vacuum.native",
            registry_id="registry-native",
            native_mop_profile=True,
        )
        app.discovery = DiscoverySnapshot(
            MappingProxyType({native.entity_id: native}), app.discovery.rooms
        )
        app.state.ensure_robot("registry-native", supports_mopping=True)
        with self.assertRaisesRegex(ValueError, "concrete route"):
            await app.async_set_robot_setting("vacuum.native", "mop_mode", None)

        self.assertGreaterEqual(app.storage.async_save.await_count, 10)
        self.assertGreaterEqual(app.async_evaluate.await_count, 10)

    async def test_floor_plan_writes_are_atomic_and_shutdown_safe(self) -> None:
        app = state_application()
        request = FloorPlanWrite(
            floor_id="ground",
            revision=0,
            rooms=(
                (
                    "study",
                    FloorPlanRectangle("ground", 10, 20, 100, 80),
                ),
            ),
            edges=(),
            sensors=(),
        )
        result = await app.async_save_floor_plan(request)
        self.assertEqual(result["revision"], 1)
        result = await app.async_set_room_adjacency("study", [])
        self.assertEqual(result["revision"], 2)
        self.assertEqual(app.storage.async_save.await_count, 2)

        app._closing = True
        with self.assertRaisesRegex(ValueError, "shutting down"):
            await app.async_set_room_adjacency("study", [])
        app._closing = False
        app._storage_safe_mode = True
        with self.assertRaisesRegex(ValueError, "storage is unsafe"):
            await app.async_save_floor_plan(request)

    async def test_fault_scope_views_and_checkpoint_are_redaction_safe(self) -> None:
        app = state_application()
        discovered_robot = app.discovery.robots["vacuum.alpha"]
        discovered_room = app.discovery.rooms["study"]
        occurrence = CleaningOccurrence(
            "occurrence-1",
            "study",
            "registry-alpha",
            "vacuum.alpha",
            CleaningProgram.VACUUM_ONLY,
            [CleaningStage(CleaningOperation.VACUUM, 1, started_at=NOW)],
            NOW,
            NOW,
            "fake",
            2,
        )
        app.state.occurrences["study"] = occurrence
        app.state.active_jobs["registry-alpha"] = active_job(
            occurrence_id="occurrence-1"
        )

        with patch(
            "custom_components.adaptive_robovacs.application._now", return_value=NOW
        ):
            await app._async_latch_scheduler_fault(
                discovered_robot,
                discovered_room,
                "area_mapping_missing",
                "preflight",
                native_command_may_have_started=False,
                outcome_uncertain=False,
            )

        fault = app.state.room_faults["study"]
        self.assertEqual(fault.reason_code, "area_mapping_missing")
        self.assertIsNone(app.state.active_jobs["registry-alpha"])
        self.assertEqual(occurrence.stages[0].status.value, "pending")
        self.assertIsNone(occurrence.stages[0].started_at)
        self.assertEqual(app.state.room_history["study"].map_status, "error")
        view = app.room_fault_view(discovered_room)
        self.assertEqual(view["failure_code"], "area_mapping_missing")
        self.assertNotIn("vendor", str(view).lower())
        self.assertTrue(app.fault_affects_room(discovered_room))
        self.assertFalse(app.fault_affects_robot(discovered_robot))
        self.assertEqual(app.scheduler_fault_view(), view)
        self.assertEqual(app._fault_views("room_faults"), [view])
        self.assertIsNone(app.robot_fault_view(discovered_robot))
        self.assertEqual(app.storage.async_save.await_count, 1)

        await app._async_latch_scheduler_fault(
            discovered_robot,
            discovered_room,
            "area_mapping_missing",
            "preflight",
            native_command_may_have_started=False,
            outcome_uncertain=False,
        )
        self.assertEqual(app.storage.async_save.await_count, 1)

        app.state.active_jobs["registry-alpha"] = active_job()
        with patch(
            "custom_components.adaptive_robovacs.application._now", return_value=NOW
        ):
            await app._async_latch_scheduler_fault(
                discovered_robot,
                discovered_room,
                "start_outcome_uncertain",
                "dispatch",
                native_command_may_have_started=True,
                outcome_uncertain=True,
            )
        self.assertTrue(app.fault_affects_robot(discovered_robot))
        self.assertEqual(
            app.state.active_jobs["registry-alpha"].phase,
            JobPhase.START_OUTCOME_UNCERTAIN,
        )
        self.assertIsNone(app.scheduler_fault_view())
        app._sync_dispatch_fault_issues()
        app._sync_two_pass_issues()
        app._sync_cleaning_program_issues()
        self.assertEqual(app.scheduler_summary()["room_faults"], [view])

    async def test_unknown_adapter_fault_is_normalized_before_persistence(self) -> None:
        app = state_application()
        with self.assertLogs(
            "custom_components.adaptive_robovacs.application_faults",
            level="ERROR",
        ):
            await app._async_latch_scheduler_fault(
                app.discovery.robots["vacuum.alpha"],
                app.discovery.rooms["study"],
                "vendor_internal_exception_text",
                "dispatch",
                native_command_may_have_started=False,
                outcome_uncertain=False,
            )

        fault = app.state.robot_faults["registry-alpha"]
        self.assertIs(fault.reason_code, FaultCode.UNRECOGNIZED_ADAPTER_FAILURE)
        self.assertEqual(
            app.robot_fault_view(app.discovery.robots["vacuum.alpha"])["failure_code"],
            "unrecognized_adapter_failure",
        )

    async def test_q10_max_plus_fallback_updates_only_the_effective_source(
        self,
    ) -> None:
        app = state_application()
        discovered_robot = app.discovery.robots["vacuum.alpha"]
        discovered_room = app.discovery.rooms["study"]
        robot_settings = app.state.robot_settings["registry-alpha"]
        room_settings = app.state.room_settings["study"]
        robot_settings.fan_speed = "max_plus"
        candidate = SimpleNamespace(profile_sources=(("fan_speed", "robot"),))
        await app._async_downgrade_q10_max_plus(
            discovered_robot, discovered_room, candidate
        )
        self.assertEqual(robot_settings.fan_speed, "max")

        room_settings.fan_speed = "max_plus"
        candidate.profile_sources = (("fan_speed", "room"),)
        await app._async_downgrade_q10_max_plus(
            discovered_robot, discovered_room, candidate
        )
        self.assertEqual(room_settings.fan_speed, "max")
        saves = app.storage.async_save.await_count
        await app._async_downgrade_q10_max_plus(
            discovered_robot, discovered_room, candidate
        )
        self.assertEqual(app.storage.async_save.await_count, saves)

    def test_mop_washing_requires_adapter_evidence_and_marks_once(self) -> None:
        app = state_application()
        discovered_robot = app.discovery.robots["vacuum.alpha"]
        active = ActiveJob(
            room_id="study",
            room_ids=["study"],
            operation=CleaningOperation.MOP,
            phase=JobPhase.ACCEPTED,
            source=JobSource.SCHEDULER,
        )
        app.hass.states.values["sensor.alpha_status"] = SimpleNamespace(
            state="washing_the_mop", last_changed=NOW - timedelta(seconds=5)
        )
        self.assertTrue(app._mop_washing_is_observed(discovered_robot, active))
        self.assertFalse(app._mop_washing_is_observed(None, active))
        active.seen_cleaning = True
        self.assertFalse(app._mop_washing_is_observed(discovered_robot, active))
        active.seen_cleaning = False
        app._cancel_start_confirmation = Mock()
        self.assertTrue(app._mark_mop_washing_started(discovered_robot, active, NOW))
        self.assertEqual(active.phase, JobPhase.MOP_WASHING)
        self.assertEqual(active.mop_washing_at, NOW - timedelta(seconds=5))
        self.assertFalse(app._mark_mop_washing_started(discovered_robot, active, NOW))

    async def test_robot_fault_recheck_is_observation_only_and_scope_safe(self) -> None:
        app = state_application()
        self.assertFalse((await app.async_recheck_and_resume()).cleared)

        fault = SchedulerFault(
            "start_outcome_uncertain",
            "registry-alpha",
            "study",
            NOW,
            "dispatch",
            native_command_may_have_started=True,
            outcome_uncertain=True,
        )
        app.state.robot_faults["registry-alpha"] = fault
        app.discovery = DiscoverySnapshot(MappingProxyType({}), app.discovery.rooms)
        result = await app.async_recheck_and_resume("registry-alpha")
        self.assertEqual(result.reason, "recovery_target_unavailable")

        app = state_application()
        app.state.robot_faults["registry-alpha"] = fault
        app.hass.states.values["vacuum.alpha"] = SimpleNamespace(state="idle")
        result = await app.async_recheck_and_resume()
        self.assertEqual(result.reason, "robot_not_docked_or_cleaning")

        app.hass.states.values["vacuum.alpha"].state = "docked"
        app._discard_unconfirmed_scheduler_job = Mock()
        app._async_clear_robot_fault = AsyncMock()
        result = await app.async_recheck_and_resume()
        self.assertTrue(result.cleared)
        app._discard_unconfirmed_scheduler_job.assert_called_once()
        app._async_clear_robot_fault.assert_awaited_once()

        app.hass.states.values["vacuum.alpha"].state = "cleaning"
        app._discard_unconfirmed_scheduler_job.reset_mock()
        app._async_clear_robot_fault.reset_mock()
        with patch(
            "custom_components.adaptive_robovacs.application_faults."
            "should_assume_native_app_clean",
            return_value=False,
        ):
            result = await app.async_recheck_and_resume("registry-alpha")
        self.assertTrue(result.cleared)
        app._discard_unconfirmed_scheduler_job.assert_not_called()
        app._async_clear_robot_fault.assert_awaited_once()

    async def test_discard_and_clear_fault_restore_pending_stage(self) -> None:
        app = state_application()
        discovered_robot = app.discovery.robots["vacuum.alpha"]
        discovered_room = app.discovery.rooms["study"]
        app._discard_unconfirmed_scheduler_job(discovered_robot, discovered_room)

        job = active_job(occurrence_id="occurrence-1")
        app.state.active_jobs["registry-alpha"] = job
        occurrence = CleaningOccurrence(
            "occurrence-1",
            "study",
            "registry-alpha",
            "vacuum.alpha",
            CleaningProgram.VACUUM_ONLY,
            [
                CleaningStage(
                    CleaningOperation.VACUUM,
                    1,
                    StageStatus.RUNNING,
                    started_at=NOW,
                )
            ],
            NOW,
            NOW,
            "fake",
            2,
        )
        app.state.occurrences["study"] = occurrence
        app._cancel_start_confirmation = Mock()
        app._discard_unconfirmed_scheduler_job(discovered_robot, discovered_room)
        self.assertIsNone(app.state.active_jobs["registry-alpha"])
        self.assertEqual(occurrence.stages[0].status, StageStatus.PENDING)
        self.assertIsNone(occurrence.stages[0].started_at)

        app.state.robot_faults["registry-alpha"] = SchedulerFault(
            "failed", "registry-alpha", "study", NOW, "dispatch"
        )
        app._reset_ready_confirmation = Mock()
        await app._async_clear_robot_fault(discovered_robot, discovered_room)
        self.assertNotIn("registry-alpha", app.state.robot_faults)
        app.repairs.delete_robot_dispatch_fault.assert_called_once_with(
            "registry-alpha"
        )

        job.source = JobSource.MANUAL_HOME_ASSISTANT
        app.state.active_jobs["registry-alpha"] = job
        app._discard_unconfirmed_scheduler_job(discovered_robot, discovered_room)
        self.assertIs(app.state.active_jobs["registry-alpha"], job)
        job.source = JobSource.SCHEDULER
        job.seen_cleaning = True
        app._discard_unconfirmed_scheduler_job(discovered_robot, discovered_room)
        self.assertIs(app.state.active_jobs["registry-alpha"], job)

    async def test_room_fault_recheck_requires_live_candidate_and_both_preflights(
        self,
    ) -> None:
        app = state_application()
        self.assertTrue(await app.async_recheck_room_fault("study"))
        fault = SchedulerFault(
            "area_mapping_missing", "registry-alpha", "study", NOW, "preflight"
        )
        app.state.room_faults["study"] = fault
        app.discovery = DiscoverySnapshot(app.discovery.robots, MappingProxyType({}))
        self.assertFalse(await app.async_recheck_room_fault("study"))

        app = state_application()
        app.state.room_faults["study"] = fault
        app._recheck_candidate = Mock(return_value=SimpleNamespace())
        app._candidate_for_robot = Mock(return_value=None)
        app.dispatch = SimpleNamespace(
            async_preflight=AsyncMock(), async_validate_profile=AsyncMock()
        )
        self.assertFalse(await app.async_recheck_room_fault("study"))

        app._candidate_for_robot.return_value = SimpleNamespace()
        app.dispatch.async_preflight.side_effect = RuntimeError("vendor detail")
        self.assertFalse(await app.async_recheck_room_fault("study"))
        app.dispatch.async_preflight.side_effect = None
        app.dispatch.async_preflight.return_value = SimpleNamespace(ready=False)
        app.dispatch.async_validate_profile.return_value = SimpleNamespace(ready=True)
        self.assertFalse(await app.async_recheck_room_fault("study"))
        app.dispatch.async_preflight.return_value = SimpleNamespace(ready=True)
        app.dispatch.async_validate_profile.return_value = SimpleNamespace(ready=False)
        self.assertFalse(await app.async_recheck_room_fault("study"))

        app.dispatch.async_validate_profile.return_value = SimpleNamespace(ready=True)
        history = app.state.room_history["study"]
        history.map_status = "error"
        history.map_error = "safe summary"
        self.assertTrue(await app.async_recheck_room_fault("study"))
        self.assertNotIn("study", app.state.room_faults)
        self.assertEqual(history.map_status, "mapped")
        self.assertIsNone(history.map_error)
        app.repairs.delete_room_dispatch_fault.assert_called_once_with("study")

    async def test_compatibility_rechecks_use_current_floor_capabilities(self) -> None:
        app = state_application()
        app.discovery = DiscoverySnapshot(app.discovery.robots, MappingProxyType({}))
        self.assertFalse(await app.async_recheck_room_compatibility("study"))
        self.assertFalse(
            await app.async_recheck_cleaning_program_compatibility("study")
        )

        app = state_application()
        self.assertTrue(await app.async_recheck_room_compatibility("study"))
        settings = app.state.room_settings["study"]
        settings.vacuum_pass_count = 2
        app._sync_two_pass_issues = Mock()
        self.assertTrue(await app.async_recheck_room_compatibility("study"))
        app._sync_two_pass_issues.assert_called_once()

        candidate = SimpleNamespace()
        app._recheck_candidate = Mock(return_value=candidate)
        app._candidate_for_robot = Mock(return_value=candidate)
        app._sync_cleaning_program_issues = Mock()
        self.assertTrue(await app.async_recheck_cleaning_program_compatibility("study"))
        app._sync_cleaning_program_issues.assert_called_once()

        upper_robot = robot(floor_id="upper")
        app.discovery = DiscoverySnapshot(
            MappingProxyType({upper_robot.entity_id: upper_robot}),
            app.discovery.rooms,
        )
        self.assertFalse(await app.async_recheck_room_compatibility("study"))
        self.assertFalse(
            await app.async_recheck_cleaning_program_compatibility("study")
        )

    async def test_start_confirmation_timer_reenters_through_typed_queue(self) -> None:
        app = state_application()
        app.async_execute = AsyncMock(return_value={})
        app._start_confirmation_timers["vacuum.alpha"] = Mock()
        callbacks = {}
        tasks = []

        def create_task(coro, *, name=None):
            task = asyncio.create_task(coro, name=name)
            tasks.append(task)
            return task

        def track(_hass, callback, deadline):
            callbacks[deadline] = callback
            return Mock()

        app._async_create_task = create_task
        with (
            patch(
                "custom_components.adaptive_robovacs.application._now",
                return_value=NOW,
            ),
            patch(
                "custom_components.adaptive_robovacs.application."
                "async_track_point_in_utc_time",
                side_effect=track,
            ),
        ):
            app._schedule_start_confirmation("vacuum.alpha")

        deadline = NOW + timedelta(minutes=2)
        callbacks[deadline](deadline)
        await asyncio.gather(*tasks)
        command = app.async_execute.await_args.args[0]
        self.assertEqual(command.reason, "start-confirmation:vacuum.alpha")
        self.assertNotIn("vacuum.alpha", app._start_confirmation_timers)


if __name__ == "__main__":
    unittest.main()
