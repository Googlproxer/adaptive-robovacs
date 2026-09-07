"""Home Assistant-facing config-entry, coordinator, and service tests."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.components.vacuum import VacuumEntityFeature
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import issue_registry as ir
from probatio.error import MultipleInvalid
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_test_home_assistant,
)

from custom_components.adaptive_robovacs.application import SchedulerApplication
from custom_components.adaptive_robovacs.commands import (
    AcknowledgeRoomRecoveryCommand,
    CommandResult,
    EvaluateCommand,
    ManualCleanRoomCommand,
    SetGlobalCommand,
)
from custom_components.adaptive_robovacs.const import (
    DOMAIN,
    SERVICE_EVALUATE,
    SERVICE_MANUAL_CLEAN_ROOM,
)
from custom_components.adaptive_robovacs.coordinator import (
    AdaptiveRoboVacsCoordinator,
)
from custom_components.adaptive_robovacs.integration_core import (
    async_remove_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.adaptive_robovacs.models import (
    CleaningProgram,
    EvaluationCause,
    EvaluationMode,
    WaterReadiness,
)
from custom_components.adaptive_robovacs.runtime_data import (
    AdaptiveRoboVacsRuntimeData,
)
from custom_components.adaptive_robovacs.services import async_register_services
from custom_components.adaptive_robovacs.snapshots import (
    FloorPlanView,
    FrozenJsonObject,
    IntegrationSnapshot,
    SchedulerView,
)


def empty_snapshot() -> IntegrationSnapshot:
    plan = FloorPlanView(0, (), (), (), ())
    scheduler = SchedulerView(
        observe_only=True,
        party_mode=False,
        scheduler_halted=False,
        scheduler_limited=False,
        storage_safe_mode=False,
        forecast_confidence=75,
        unresolved_start="00:00",
        unresolved_end="04:00",
        last_evaluation_at=None,
        preview=FrozenJsonObject(),
        robot_faults=(),
        room_faults=(),
        floor_plan=plan,
        failure=None,
    )
    return IntegrationSnapshot(scheduler, (), (), plan)


class _FakeApplication:
    instances: ClassVar[list[object]] = []

    def __init__(self, hass, entry) -> None:
        self.hass = hass
        self.entry = entry
        self.lifecycle = SimpleNamespace()
        self.initialized = False
        self.shutdown_started = False
        self.shutdown_cancelled = False
        self.shutdown_complete = False
        self.commands = []
        self.listener = None
        self.listener_removed = False
        self.instances.append(self)

    async def async_initialize(self) -> None:
        self.initialized = True

    def current_snapshot(self):
        return empty_snapshot()

    def async_add_listener(self, listener):
        self.listener = listener

        def remove():
            self.listener_removed = True

        return remove

    async def async_execute(self, command):
        self.commands.append(command)
        if isinstance(command, EvaluateCommand):
            return CommandResult.from_mapping(
                {"mode": command.mode.value, "reason": command.reason}
            )
        if isinstance(command, ManualCleanRoomCommand):
            return CommandResult.from_mapping(
                {
                    "accepted": True,
                    "area_id": command.area_id,
                    "mode": command.mode,
                }
            )
        return CommandResult.from_mapping({})

    def legacy_deferral_report(self):
        return [{"area_id": "study"}]

    def begin_shutdown(self) -> None:
        self.shutdown_started = True

    def cancel_shutdown(self) -> None:
        self.shutdown_cancelled = True

    async def async_shutdown(self) -> None:
        self.shutdown_complete = True


class HomeAssistantSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_room_error_repair_leaves_other_rooms_schedulable_in_real_application(
        self,
    ):
        from tests.test_room_recovery import recovery_application

        fixture = recovery_application()
        entry = MockConfigEntry(
            domain=DOMAIN, entry_id="entry-recovery", data={"observe_only": False}
        )
        entry.add_to_hass(self.hass)
        app = SchedulerApplication(self.hass, entry)
        app.state = fixture.state
        robot = fixture.discovery.robots["vacuum.alpha"]
        robot = replace(
            robot,
            adapter_capabilities=replace(
                robot.adapter_capabilities,
                mode_options=("vacuum", "mop"),
                water_readiness=WaterReadiness(
                    "sensor_blocked",
                    "water_unavailable",
                    ready=False,
                    authoritative=True,
                ),
            ),
        )
        app.discovery = type(fixture.discovery)(
            {robot.entity_id: robot}, fixture.discovery.rooms
        )
        app.async_refresh_discovery = AsyncMock()
        app._async_dispatch = AsyncMock(return_value=(True, "test boundary dispatch"))
        app._async_refresh_pending_profile_if_needed = AsyncMock(
            side_effect=lambda _robot, candidate: candidate
        )
        clock = datetime.now(UTC)
        for entity_id, value in fixture.hass.states.values.items():
            self.hass.states.async_set(entity_id, value.state)
        for area_id in ("study", "hall"):
            self.hass.states.async_set(f"binary_sensor.{area_id}_radar", "off")
            history = app.state.room_history[area_id]
            history.occupancy = "unoccupied"
            history.unoccupied_since = clock - timedelta(hours=3)
            history.cleaning_completed_at = clock - timedelta(days=5)
            app.state.room_settings[area_id].ignore_desired_window = True
        app.state.room_settings["hall"].cleaning_program = CleaningProgram.VACUUM_ONLY
        app.state.robot_settings["registry-alpha"].fan_speed = "max"
        app.state.robot_settings["registry-alpha"].mop_mode = "standard"
        app.state.robot_settings["registry-alpha"].mop_intensity = "medium"
        try:
            with patch(
                "custom_components.adaptive_robovacs.application.core._now",
                return_value=clock,
            ):
                await app.async_execute(
                    EvaluateCommand(EvaluationMode.PREVIEW, EvaluationCause.SERVICE)
                )
            issue_id = "room_recovery_entry-recovery_study"
            self.assertIsNotNone(
                ir.async_get(self.hass).async_get_issue(DOMAIN, issue_id)
            )
            for entity_id, value in {
                "vacuum.alpha": "docked",
                "sensor.alpha_status": "charging",
                "sensor.alpha_error": "none",
            }.items():
                self.hass.states.async_set(entity_id, value)
            for offset in (1, 11):
                with patch(
                    "custom_components.adaptive_robovacs.application.core._now",
                    return_value=clock + timedelta(seconds=offset),
                ):
                    await app.async_execute(
                        EvaluateCommand(EvaluationMode.PREVIEW, EvaluationCause.SERVICE)
                    )
            snapshot = app.current_snapshot()
            self.assertTrue(snapshot.scheduler.scheduler_limited)
            self.assertIsNotNone(snapshot.room("study").recovery)
            self.assertIsNone(snapshot.room("study").active)
            self.assertIsNone(snapshot.room("study").failure)
            recovery = app.state.room_recoveries["study"]
            with patch(
                "custom_components.adaptive_robovacs.application.core._now",
                return_value=clock + timedelta(seconds=22),
            ):
                result = await app.async_execute(
                    EvaluateCommand(EvaluationMode.DISPATCH, EvaluationCause.SERVICE)
                )
            self.assertTrue(result.as_response()["assignments"], result.as_response())
            self.assertEqual(result.as_response()["assignments"][0]["room"], "hall")
            app._async_dispatch.assert_awaited_once()
            self.assertEqual(app._async_dispatch.call_args.args[1].room_id, "hall")
            app._async_dispatch.reset_mock()
            self.hass.states.async_set("vacuum.alpha", "cleaning")
            result = await app.async_execute(
                AcknowledgeRoomRecoveryCommand("study", recovery.recovery_id)
            )
            self.assertTrue(result.as_response()["cleared"])
            self.assertIsNone(ir.async_get(self.hass).async_get_issue(DOMAIN, issue_id))
            app._async_dispatch.assert_not_awaited()
        finally:
            app.begin_shutdown()
            await app.async_shutdown()

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.hass_context = async_test_home_assistant(
            config_dir=self.temp_dir.name,
        )
        self.hass = await self.hass_context.__aenter__()

    async def asyncTearDown(self) -> None:
        await self.hass.async_stop(force=True)
        await self.hass_context.__aexit__(None, None, None)
        self.temp_dir.cleanup()

    async def test_public_setup_and_unload_use_only_typed_runtime_data(self) -> None:
        entry = MockConfigEntry(
            domain=DOMAIN,
            entry_id="entry-1",
            data={"observe_only": True},
        )
        entry.add_to_hass(self.hass)
        forward = AsyncMock()
        unload = AsyncMock(return_value=True)

        with (
            patch(
                "custom_components.adaptive_robovacs.integration_core."
                "SchedulerApplication",
                _FakeApplication,
            ),
            patch.object(
                self.hass.config_entries,
                "async_forward_entry_setups",
                forward,
            ),
            patch.object(
                self.hass.config_entries,
                "async_unload_platforms",
                unload,
            ),
        ):
            self.assertTrue(await async_setup_entry(self.hass, entry))
            runtime = entry.runtime_data
            self.assertIsInstance(runtime, AdaptiveRoboVacsRuntimeData)
            self.assertTrue(runtime.application.initialized)
            self.assertIsInstance(
                runtime.coordinator,
                AdaptiveRoboVacsCoordinator,
            )
            self.assertNotIn(entry.entry_id, self.hass.data.get(DOMAIN, {}))
            self.assertTrue(await async_unload_entry(self.hass, entry))

        forward.assert_awaited_once()
        unload.assert_awaited_once()
        self.assertTrue(runtime.application.shutdown_started)
        self.assertTrue(runtime.application.shutdown_complete)
        self.assertFalse(runtime.application.shutdown_cancelled)
        self.assertTrue(runtime.application.listener_removed)

    async def test_setup_retires_only_owned_controls_once_before_platform_setup(
        self,
    ) -> None:
        entry = MockConfigEntry(
            domain=DOMAIN,
            entry_id="entry-retire",
            data={"observe_only": True, "hall_start": "invalid", "hall_end": "20:00"},
            options={"hall_start": "08:00", "retained": "value"},
        )
        other = MockConfigEntry(domain=DOMAIN, entry_id="entry-other")
        entry.add_to_hass(self.hass)
        other.add_to_hass(self.hass)
        registry = er.async_get(self.hass)
        retired = [
            registry.async_get_or_create(
                "select", DOMAIN, f"{entry.entry_id}_global_{key}", config_entry=entry
            ).entity_id
            for key in ("hall_start", "hall_end")
        ]
        retired[0] = registry.async_update_entity(
            retired[0], new_entity_id="select.user_renamed_control"
        ).entity_id
        retained = [
            registry.async_get_or_create(
                domain, platform, unique_id, config_entry=owner
            ).entity_id
            for domain, platform, unique_id, owner in (
                ("select", DOMAIN, f"{entry.entry_id}_global_unresolved_start", entry),
                ("sensor", DOMAIN, f"{entry.entry_id}_global_hall_start", entry),
                (
                    "select",
                    "another_platform",
                    f"{entry.entry_id}_global_hall_start",
                    entry,
                ),
                ("select", DOMAIN, f"{other.entry_id}_global_hall_start", other),
            )
        ]

        async def forward(*_args):
            self.assertEqual(entry.data, {"observe_only": True})
            self.assertEqual(entry.options, {"retained": "value"})
            self.assertTrue(
                all(registry.async_get(entity_id) is None for entity_id in retired)
            )
            self.assertTrue(
                all(registry.async_get(entity_id) is not None for entity_id in retained)
            )

        with (
            patch(
                "custom_components.adaptive_robovacs.integration_core.SchedulerApplication",
                _FakeApplication,
            ),
            patch.object(
                self.hass.config_entries,
                "async_forward_entry_setups",
                AsyncMock(side_effect=forward),
            ),
            patch.object(
                self.hass.config_entries,
                "async_unload_platforms",
                AsyncMock(return_value=True),
            ),
            patch.object(
                self.hass.config_entries,
                "async_update_entry",
                wraps=self.hass.config_entries.async_update_entry,
            ) as update,
        ):
            for _ in range(2):
                self.assertTrue(await async_setup_entry(self.hass, entry))
                self.assertTrue(await async_unload_entry(self.hass, entry))
            update.assert_called_once()

    async def test_retired_unique_id_owned_by_another_entry_is_preserved(self) -> None:
        entry = MockConfigEntry(domain=DOMAIN, entry_id="entry-retire")
        other = MockConfigEntry(domain=DOMAIN, entry_id="entry-other")
        entry.add_to_hass(self.hass)
        other.add_to_hass(self.hass)
        registry = er.async_get(self.hass)
        mismatched = registry.async_get_or_create(
            "select", DOMAIN, f"{entry.entry_id}_global_hall_start", config_entry=other
        )
        with (
            patch(
                "custom_components.adaptive_robovacs.integration_core.SchedulerApplication",
                _FakeApplication,
            ),
            patch.object(
                self.hass.config_entries, "async_forward_entry_setups", AsyncMock()
            ),
            patch.object(
                self.hass.config_entries,
                "async_unload_platforms",
                AsyncMock(return_value=True),
            ),
        ):
            self.assertTrue(await async_setup_entry(self.hass, entry))
            self.assertIsNotNone(registry.async_get(mismatched.entity_id))
            self.assertTrue(await async_unload_entry(self.hass, entry))

    async def test_rejected_platform_unload_reopens_the_application(self) -> None:
        entry = MockConfigEntry(domain=DOMAIN, entry_id="entry-rejected")
        application = _FakeApplication(self.hass, entry)
        coordinator = SimpleNamespace(close=Mock())
        entry.runtime_data = AdaptiveRoboVacsRuntimeData(
            coordinator=coordinator,
            application=application,
            lifecycle=application.lifecycle,
        )
        entry.add_to_hass(self.hass)

        with (
            patch.object(
                self.hass.config_entries,
                "async_unload_platforms",
                AsyncMock(return_value=False),
            ),
            patch(
                "custom_components.adaptive_robovacs.integration_core."
                "async_unregister_services",
                AsyncMock(),
            ) as unregister,
        ):
            self.assertFalse(await async_unload_entry(self.hass, entry))

        self.assertTrue(application.shutdown_started)
        self.assertTrue(application.shutdown_cancelled)
        self.assertFalse(application.shutdown_complete)
        coordinator.close.assert_not_called()
        unregister.assert_not_awaited()

    async def test_remove_entry_deletes_both_stores_and_owned_repairs(self) -> None:
        entry = MockConfigEntry(domain=DOMAIN, entry_id="entry-remove")
        stores = {}

        def make_store(_hass, _version, key):
            payload = (
                {
                    "room_settings": {"study": {}},
                    "settings": {"rooms": {"legacy_room": {}}},
                }
                if key.endswith(".data.entry-remove")
                else None
            )
            store = SimpleNamespace(
                async_load=AsyncMock(return_value=payload),
                async_remove=AsyncMock(),
            )
            stores[key] = store
            return store

        issue_registry = SimpleNamespace(
            issues={
                (DOMAIN, "owned-extra"): SimpleNamespace(
                    data={"entry_id": "entry-remove"}
                ),
                (DOMAIN, "other-entry"): SimpleNamespace(data={"entry_id": "other"}),
                ("other_domain", "ignored"): SimpleNamespace(
                    data={"entry_id": "entry-remove"}
                ),
            }
        )
        with (
            patch(
                "custom_components.adaptive_robovacs.integration_core.Store",
                side_effect=make_store,
            ),
            patch(
                "custom_components.adaptive_robovacs.retired_features.Store",
                side_effect=make_store,
            ),
            patch(
                "custom_components.adaptive_robovacs.integration_core.ir.async_get",
                return_value=issue_registry,
            ),
            patch(
                "custom_components.adaptive_robovacs.integration_core."
                "ir.async_delete_issue"
            ) as delete_issue,
        ):
            await async_remove_entry(self.hass, entry)

        self.assertEqual(len(stores), 2)
        for store in stores.values():
            store.async_remove.assert_awaited_once()
        deleted = {call.args[2] for call in delete_issue.call_args_list}
        self.assertIn("owned-extra", deleted)
        self.assertIn("two_pass_no_longer_supported_entry-remove_study", deleted)
        self.assertIn("cleaning_program_incompatible_entry-remove_legacy_room", deleted)
        self.assertNotIn("other-entry", deleted)

    async def test_real_application_initializes_evaluates_and_drains(self) -> None:
        entry = MockConfigEntry(
            domain=DOMAIN,
            entry_id="entry-real",
            data={
                "observe_only": True,
                "forecast_confidence": 75,
                "unresolved_start": "00:00",
                "unresolved_end": "04:00",
            },
        )
        entry.add_to_hass(self.hass)
        application = SchedulerApplication(self.hass, entry)

        await application.async_initialize()
        await asyncio.wait_for(self.hass.async_block_till_done(), timeout=1)
        self.assertTrue(application.current_snapshot().scheduler.observe_only)
        result = await application.async_execute(
            EvaluateCommand(
                mode=EvaluationMode.PREVIEW,
                cause=EvaluationCause.SERVICE,
            )
        )
        self.assertIsNotNone(result)
        preview = result.as_response()
        self.assertEqual(preview["observe_only"], True)
        await application.async_execute(SetGlobalCommand("party_mode", True))
        self.assertTrue(application.current_snapshot().scheduler.party_mode)

        application.begin_shutdown()
        await application.async_shutdown()
        self.assertTrue(application._shutdown_started())

    async def test_real_registry_discovery_builds_and_assigns_a_preview(self) -> None:
        entry = MockConfigEntry(
            domain=DOMAIN,
            entry_id="entry-discovery",
            data={
                "observe_only": True,
                "forecast_confidence": 75,
                "unresolved_start": "00:00",
                "unresolved_end": "23:45",
            },
        )
        entry.add_to_hass(self.hass)
        floor = fr.async_get(self.hass).async_create("Ground")
        dock = ar.async_get(self.hass).async_create(
            "Dock",
            floor_id=floor.floor_id,
        )
        study = ar.async_get(self.hass).async_create(
            "Study",
            floor_id=floor.floor_id,
        )
        device = dr.async_get(self.hass).async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, "robot-device")},
            name="Alpha",
        )
        device_registry = dr.async_get(self.hass)
        device_registry.async_update_device(device.id, area_id=dock.id)
        robot_entry = er.async_get(self.hass).async_get_or_create(
            "vacuum",
            "test_vendor",
            "alpha",
            suggested_object_id="alpha",
            config_entry=entry,
            device_id=device.id,
            supported_features=int(VacuumEntityFeature.CLEAN_AREA),
        )
        battery_entry = er.async_get(self.hass).async_get_or_create(
            "sensor",
            "test_vendor",
            "alpha-battery",
            suggested_object_id="alpha_battery",
            config_entry=entry,
            device_id=device.id,
            original_device_class=SensorDeviceClass.BATTERY,
            unit_of_measurement="%",
        )
        occupancy_entry = er.async_get(self.hass).async_get_or_create(
            "binary_sensor",
            "test_vendor",
            "study-occupancy",
            suggested_object_id="study_occupancy",
            config_entry=entry,
            original_device_class=BinarySensorDeviceClass.OCCUPANCY,
        )
        occupancy_entry = er.async_get(self.hass).async_update_entity(
            occupancy_entry.entity_id,
            area_id=study.id,
        )
        self.hass.states.async_set(
            robot_entry.entity_id,
            "docked",
            {
                "friendly_name": "Alpha",
                "battery_level": 95,
                "supported_features": int(VacuumEntityFeature.CLEAN_AREA),
            },
        )
        self.hass.states.async_set(
            occupancy_entry.entity_id,
            "off",
            {"device_class": BinarySensorDeviceClass.OCCUPANCY},
        )
        self.hass.states.async_set(
            battery_entry.entity_id,
            "95",
            {
                "device_class": SensorDeviceClass.BATTERY,
                "unit_of_measurement": "%",
            },
        )
        application = SchedulerApplication(self.hass, entry)

        await application.async_initialize()
        application._startup_state_settle_until = None
        settings = application.state.room_settings[study.id]
        settings.ignore_desired_window = True
        application.state.room_history[study.id].cleaning_completed_at = datetime(
            2020, 1, 1, tzinfo=UTC
        )
        application.state.room_history[study.id].unoccupied_since = datetime.now(
            UTC
        ) - timedelta(hours=1)
        application._ready_since[robot_entry.entity_id] = datetime.now(UTC) - timedelta(
            minutes=1
        )
        result = await application.async_execute(
            EvaluateCommand(
                mode=EvaluationMode.PREVIEW,
                cause=EvaluationCause.SERVICE,
            )
        )
        self.assertIsNotNone(result)
        preview = result.as_response()

        self.assertIn(robot_entry.entity_id, application.discovery.robots)
        self.assertIn(study.id, application.discovery.rooms)
        self.assertEqual(
            application.discovery.rooms[study.id].fallback_entity_ids,
            (occupancy_entry.entity_id,),
        )
        self.assertTrue(
            any(item["room"] == study.id for item in preview["candidates"]),
            preview,
        )
        self.assertTrue(
            any(item["room"] == study.id for item in preview["assignments"]),
            preview,
        )

        application.begin_shutdown()
        await application.async_shutdown()

    async def test_registered_services_validate_and_submit_typed_commands(self) -> None:
        application = _FakeApplication(self.hass, SimpleNamespace(entry_id="entry-2"))
        entry = MockConfigEntry(
            domain=DOMAIN,
            entry_id="entry-2",
            state=ConfigEntryState.LOADED,
        )
        entry.runtime_data = AdaptiveRoboVacsRuntimeData(
            coordinator=object(),
            application=application,
            lifecycle=object(),
        )
        entry.add_to_hass(self.hass)
        await async_register_services(self.hass)

        response = await self.hass.services.async_call(
            DOMAIN,
            SERVICE_EVALUATE,
            {"dry_run": True},
            blocking=True,
            return_response=True,
        )
        self.assertEqual(response, {"mode": "preview", "reason": "service"})
        self.assertIsInstance(application.commands[-1], EvaluateCommand)

        manual = await self.hass.services.async_call(
            DOMAIN,
            SERVICE_MANUAL_CLEAN_ROOM,
            {"area_id": "study", "mode": "vacuum_only"},
            blocking=True,
            return_response=True,
        )
        self.assertEqual(
            manual,
            {"accepted": True, "area_id": "study", "mode": "vacuum_only"},
        )
        self.assertIsInstance(application.commands[-1], ManualCleanRoomCommand)

        with self.assertRaises(MultipleInvalid):
            await self.hass.services.async_call(
                DOMAIN,
                SERVICE_MANUAL_CLEAN_ROOM,
                {"area_id": "study", "mode": "unsupported"},
                blocking=True,
                return_response=True,
            )

    async def test_push_coordinator_publishes_snapshots_and_errors(self) -> None:
        entry = MockConfigEntry(domain=DOMAIN, entry_id="entry-3")
        application = _FakeApplication(self.hass, entry)
        coordinator = AdaptiveRoboVacsCoordinator(application)
        updates = []
        coordinator.async_add_listener(lambda: updates.append(coordinator.data))

        changed = empty_snapshot()
        application.listener(changed)
        self.assertEqual(coordinator.data, changed)
        self.assertEqual(updates, [changed])
        self.assertIsNone(coordinator.update_interval)

        application.listener(RuntimeError("top-level update failed"))
        self.assertFalse(coordinator.last_update_success)
        coordinator.close()
        self.assertTrue(application.listener_removed)


if __name__ == "__main__":
    unittest.main()
