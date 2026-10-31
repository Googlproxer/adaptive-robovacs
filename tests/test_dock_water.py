"""Sparse water monitoring, cached telemetry, and physical verification contracts."""

from __future__ import annotations

import asyncio
import copy
import unittest
from dataclasses import replace
from datetime import timedelta
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from custom_components.adaptive_robovacs.adapters.base import AdapterEntityEvidence
from custom_components.adaptive_robovacs.adapters.generic import GenericVacuumAdapter
from custom_components.adaptive_robovacs.adapters.roborock import (
    RoborockVacuumAdapter,
    read_dock_water,
    resolve_roborock_dock_water,
)
from custom_components.adaptive_robovacs.application import SchedulerApplication
from custom_components.adaptive_robovacs.commands import (
    CheckDockWaterCommand,
)
from custom_components.adaptive_robovacs.discovery import (
    _adapter_evidence,
    _dock_entries,
)
from custom_components.adaptive_robovacs.dock_water_state import (
    DockWaterVerification,
    RobotWaterRecord,
    decode_robot_water,
)
from custom_components.adaptive_robovacs.gateway import HomeAssistantVacuumGateway
from custom_components.adaptive_robovacs.models import (
    AdapterCleaningProfile,
    AdapterDispatchRequest,
    CleaningOperation,
    DockWaterEntities,
    DockWaterStatus,
)
from custom_components.adaptive_robovacs.presentation import robot_water_attributes
from custom_components.adaptive_robovacs.projections import robot_water_view
from custom_components.adaptive_robovacs.run_state import CleaningRun
from custom_components.adaptive_robovacs.state import (
    SCHEMA_VERSION,
    SchedulerState,
    StateSchemaError,
)
from tests.test_application_dispatch import resolved_candidate, transaction_application
from tests.test_application_planning import occurrence
from tests.test_application_state import ENTRY_DATA, NOW

SOURCES = DockWaterEntities(
    "binary_sensor.base_supply",
    "binary_sensor.base_waste",
    "sensor.base_fault",
    "switch.base_wash",
    "binary_sensor.unit_water",
)


def evidence(key, domain="binary_sensor", entity_id=None):
    return AdapterEntityEvidence(
        entity_id or f"{domain}.renamed_{key}",
        domain,
        "roborock",
        key,
        None,
        "off",
    )


class DockWaterRegistryTests(unittest.TestCase):
    def test_pairing_uses_identifiers_and_config_entry_not_names_or_floor(self):
        robot = SimpleNamespace(identifiers={("roborock", "unit-opaque")})
        dock = SimpleNamespace(id="dock-device")
        devices = SimpleNamespace(
            async_get=lambda _: robot,
            async_get_device=Mock(return_value=dock),
        )
        vacuum = SimpleNamespace(
            platform="roborock", device_id="robot", config_entry_id="entry"
        )
        sensor = SimpleNamespace(platform="roborock", config_entry_id="entry")
        foreign = SimpleNamespace(platform="roborock", config_entry_id="different")
        self.assertEqual(
            _dock_entries(vacuum, devices, {"dock-device": [sensor, foreign]}), [sensor]
        )
        devices.async_get_device.assert_called_once_with(
            identifiers={("roborock", "unit-opaque_dock")}
        )
        vacuum.platform = "other"
        self.assertEqual(_dock_entries(vacuum, devices, {}), [])
        vacuum.platform, vacuum.device_id = "roborock", None
        self.assertEqual(_dock_entries(vacuum, devices, {}), [])
        vacuum.device_id = "robot"
        robot.identifiers = set()
        self.assertEqual(_dock_entries(vacuum, devices, {}), [])
        devices.async_get = lambda _: None
        self.assertEqual(_dock_entries(vacuum, devices, {}), [])

    def test_unknown_and_ambiguous_docks_are_not_guessed(self):
        device = SimpleNamespace(identifiers={("roborock", "one"), ("roborock", "two")})
        registry = SimpleNamespace(
            async_get=lambda _: device,
            async_get_device=Mock(
                side_effect=[SimpleNamespace(id="a"), SimpleNamespace(id="b")]
            ),
        )
        vacuum = SimpleNamespace(platform="roborock", device_id="robot")
        self.assertEqual(_dock_entries(vacuum, registry, {}), [])
        registry.async_get_device = Mock(return_value=None)
        self.assertEqual(_dock_entries(vacuum, registry, {}), [])

    def test_vendor_keys_resolve_separate_sources_without_subscriptions(self):
        dock = tuple(
            evidence(key, domain)
            for key, domain in (
                ("clean_box_empty", "binary_sensor"),
                ("dirty_box_full", "binary_sensor"),
                ("dock_error", "sensor"),
                ("mop_washing", "switch"),
            )
        )
        context = SimpleNamespace(
            dock_entities=dock, entities=(evidence("water_shortage"),)
        )
        result = resolve_roborock_dock_water(context)
        self.assertTrue(result.supported)
        self.assertFalse(result.ambiguous)
        self.assertEqual(result.clean, "binary_sensor.renamed_clean_box_empty")
        self.assertEqual(result.onboard, "binary_sensor.renamed_water_shortage")
        context.dock_entities += (
            evidence("clean_box_empty", entity_id="binary_sensor.other"),
        )
        result = resolve_roborock_dock_water(context)
        self.assertTrue(result.ambiguous)
        self.assertIsNone(result.clean)
        self.assertFalse(
            read_dock_water(
                SimpleNamespace(states=SimpleNamespace(get=lambda _: None)), result
            ).dock_ready
        )

    def test_registry_unique_keys_are_used_when_translation_key_is_absent(self):
        entry = SimpleNamespace(
            entity_id="binary_sensor.completely_renamed",
            platform="roborock",
            translation_key=None,
            original_device_class="problem",
            unique_id="clean_box_empty_vendor_slug",
        )
        result = _adapter_evidence(
            SimpleNamespace(states=SimpleNamespace(get=lambda _: None)), [entry]
        )
        self.assertEqual(result[0].translation_key, "clean_box_empty")
        entry.platform = "other"
        self.assertIsNone(
            _adapter_evidence(
                SimpleNamespace(states=SimpleNamespace(get=lambda _: None)), [entry]
            )[0].translation_key
        )


class DockWaterCodecTests(unittest.TestCase):
    def test_round_trip_and_schema_20_upgrade_keep_runs_and_existing_data(self):
        state = SchedulerState.create(ENTRY_DATA)
        state.cleaning_runs["registry-alpha"] = CleaningRun(
            "run", NOW, ["study"], {"study": "occ"}
        )
        old = state.to_store()
        old["schema_version"] = 20
        old.pop("robot_water")
        upgraded, migrated = SchedulerState.from_store(old, ENTRY_DATA)
        self.assertTrue(migrated)
        self.assertEqual(upgraded.cleaning_runs["registry-alpha"].run_id, "run")
        check = DockWaterVerification("occ", ("occ",), NOW, NOW + timedelta(minutes=2))
        upgraded.robot_water["registry-alpha"] = RobotWaterRecord(
            NOW,
            DockWaterStatus("on", "unknown", "water_empty", "off"),
            ("clean",),
            check,
        )
        encoded = upgraded.encode()
        decoded, migrated = SchedulerState.from_store(encoded, ENTRY_DATA)
        self.assertFalse(migrated)
        self.assertEqual(decoded.robot_water, upgraded.robot_water)
        self.assertEqual(encoded["schema_version"], SCHEMA_VERSION)
        self.assertTrue(decoded.robot_water["registry-alpha"].verification.pending)

    def test_malformed_current_water_records_fail_closed(self):
        record = RobotWaterRecord(NOW).to_store()
        for path, value in (
            ("status", None),
            ("status", {"clean": 1}),
            ("status", {"dirty": "bad"}),
            ("notified_faults", ["bad"]),
            ("notified_faults", ["clean", "clean"]),
            ("last_snapshot_at", 12),
            ("last_snapshot_at", "2026-01-01T00:00:00"),
            ("verification", 1),
        ):
            with self.subTest(path=path, value=value):
                invalid = copy.deepcopy(record)
                invalid[path] = value
                state = SchedulerState.create(ENTRY_DATA).to_store()
                state["robot_water"] = {"registry-alpha": invalid}
                with self.assertRaises(StateSchemaError):
                    SchedulerState.from_store(state, ENTRY_DATA)
        for invalid in (None, [], {"": record}, {"x": None}):
            with self.assertRaises(ValueError):
                decode_robot_water(invalid)
        verification = DockWaterVerification("occ", ("occ",), NOW, NOW).to_store()
        for key, value in (
            ("attempt_key", ""),
            ("phase", "bad"),
            ("requested_at", None),
            ("deadline", None),
            ("reason", 1),
            ("occurrence_ids", []),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                DockWaterVerification.from_mapping({**verification, key: value})


class DockWaterApplicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = transaction_application()
        self.now = NOW
        self.points = []
        self.tasks = []
        original = self.app.discovery.robots["vacuum.alpha"]
        self.robot = replace(
            original,
            adapter_capabilities=replace(
                original.adapter_capabilities,
                dock_water=SOURCES,
                readiness_entity_id="sensor.unit_status",
                terminal_completion_states=frozenset({"charging", "charging_complete"}),
            ),
        )
        self.app.discovery = replace(
            self.app.discovery,
            robots=MappingProxyType({self.robot.entity_id: self.robot}),
        )
        self.app.gateway = SimpleNamespace(async_start_dock_wash=AsyncMock())
        self.app._async_clear_mobile_notification = AsyncMock()
        self.app._robot_technically_ready = Mock(return_value=(True, "ready"))
        self.app._dispatch_adjacency_block_reason = Mock(return_value=None)
        self.set_state("vacuum.alpha", "docked")
        self.set_state("sensor.unit_status", "charging")
        for entity_id in (SOURCES.clean, SOURCES.dirty, SOURCES.onboard):
            self.set_state(entity_id, "off")
        self.set_state(SOURCES.error, "ok")
        self.set_state(SOURCES.washing, "off")

        def create_task(coro, *, name=None):
            task = asyncio.create_task(coro)
            self.tasks.append(task)
            return task

        def track(_hass, callback, when):
            cancel = Mock()
            self.points.append((callback, when, cancel))
            return cancel

        self.app._async_create_task = create_task
        self.clock = patch(
            "custom_components.adaptive_robovacs.application.core._now",
            side_effect=lambda: self.now,
        )
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.timer = patch(
            "custom_components.adaptive_robovacs.application.core._track_point",
            side_effect=track,
        )
        self.timer.start()
        self.addCleanup(self.timer.stop)

    async def asyncTearDown(self):
        if self.tasks:
            await asyncio.gather(*self.tasks)
        self.app._stop_dock_water_timer()

    def set_state(self, entity_id, value, *, age=None):
        if age is None:
            age = 0 if entity_id == "sensor.unit_status" else 300
        self.app.hass.states.values[entity_id] = SimpleNamespace(
            state=value,
            last_changed=self.now - timedelta(seconds=age),
            attributes={},
        )

    async def check_at(self, when):
        self.now = when
        await self.app._async_check_dock_water()

    def scheduled_mop(self):
        candidate = resolved_candidate(self.app, CleaningOperation.MOP)
        candidate = replace(candidate, source="scheduler", manual_override=False)
        item = occurrence(operation=CleaningOperation.MOP)
        self.app.state.occurrences["study"] = item
        return replace(
            candidate, occurrence=item, occurrence_id=item.occurrence_id, stage_index=0
        )

    async def test_post_dock_and_hourly_snapshots_do_not_evaluate_refresh_or_wash(self):
        self.app._sync_dock_water_checks(NOW)
        self.assertEqual(self.points[-1][1], NOW + timedelta(minutes=5))
        original_get = self.app.hass.states.get
        self.app.hass.states.get = Mock(wraps=original_get)
        await self.check_at(NOW + timedelta(minutes=4))
        self.app.hass.states.get.assert_not_called()
        self.assertEqual(self.app.state.robot_water, {})
        await self.check_at(NOW + timedelta(minutes=5))
        record = self.app.state.robot_water[self.robot.registry_id]
        self.assertEqual(record.last_snapshot_at, self.now)
        self.assertEqual(self.points[-1][1], NOW + timedelta(minutes=65))
        await self.check_at(NOW + timedelta(minutes=65))
        self.assertEqual(self.points[-1][1], NOW + timedelta(minutes=125))
        self.app.gateway.async_start_dock_wash.assert_not_awaited()
        self.app.async_execute.assert_not_awaited()
        self.app.async_refresh_discovery.assert_not_awaited()

    async def test_single_timer_coalesces_and_ignores_stale_callbacks(self):
        self.app._sync_dock_water_checks(NOW)
        old = self.points[-1]
        self.app._sync_dock_water_checks(NOW + timedelta(minutes=1))
        self.assertEqual(len(self.points), 1)
        self.set_state("vacuum.alpha", "cleaning")
        self.app._sync_dock_water_checks(NOW)
        old[2].assert_called_once()
        old[0](NOW)
        self.assertFalse(self.tasks)
        self.set_state("vacuum.alpha", "docked")
        self.app._sync_dock_water_checks(NOW)
        old[0](NOW)
        self.assertFalse(self.tasks)
        self.points[-1][0](NOW + timedelta(minutes=5))
        await asyncio.sleep(0)
        self.assertIsInstance(
            self.app.async_execute.await_args.args[0], CheckDockWaterCommand
        )

    async def test_washing_and_departure_suspend_periodic_checks(self):
        self.app._sync_dock_water_checks(NOW)
        self.set_state("sensor.unit_status", "washing_the_mop")
        self.app._sync_dock_water_checks(NOW + timedelta(minutes=1))
        self.assertIsNone(self.app._water_runtime().deadline)
        self.set_state("sensor.unit_status", "charging")
        self.app._sync_dock_water_checks(NOW + timedelta(minutes=4))
        self.assertEqual(self.app._water_runtime().deadline, NOW + timedelta(minutes=9))

    async def test_notifications_combine_dedupe_preserve_unknown_and_clear(self):
        self.app._sync_dock_water_checks(NOW)
        self.set_state(SOURCES.clean, "on")
        self.set_state(SOURCES.error, "water_empty")
        await self.check_at(NOW + timedelta(minutes=5))
        self.assertIn(
            "Refill and refit",
            self.app._async_send_mobile_notification.await_args.args[0]["message"],
        )
        await self.check_at(NOW + timedelta(minutes=65))
        self.app._async_send_mobile_notification.assert_awaited_once()
        self.set_state(SOURCES.dirty, "on")
        self.set_state(SOURCES.onboard, "on")
        await self.check_at(NOW + timedelta(minutes=125))
        payload = self.app._async_send_mobile_notification.await_args.args[0]
        self.assertIn("Empty and refit", payload["message"])
        self.assertIn("Onboard water shortage", payload["message"])
        self.set_state(SOURCES.clean, "unavailable")
        self.set_state(SOURCES.dirty, "unknown")
        await self.check_at(NOW + timedelta(minutes=185))
        self.assertEqual(self.app._async_send_mobile_notification.await_count, 2)
        for key in (SOURCES.clean, SOURCES.dirty, SOURCES.onboard):
            self.set_state(key, "off")
        self.set_state(SOURCES.error, "ok")
        await self.check_at(NOW + timedelta(minutes=245))
        self.app._async_clear_mobile_notification.assert_awaited_once()
        self.assertEqual(
            self.app.state.robot_water[self.robot.registry_id].notified_faults, ()
        )

    async def test_recent_fault_gets_one_delayed_confirmation_not_an_event_watch(self):
        self.app._sync_dock_water_checks(NOW)
        self.now = NOW + timedelta(minutes=5)
        self.set_state(SOURCES.dirty, "on", age=30)
        await self.check_at(self.now)
        self.app._async_send_mobile_notification.assert_not_awaited()
        self.assertEqual(
            self.app._water_runtime().deadline, self.now + timedelta(minutes=5)
        )
        self.set_state(SOURCES.dirty, "off")
        await self.check_at(NOW + timedelta(minutes=10))
        self.app._async_send_mobile_notification.assert_not_awaited()

    async def test_unreachable_notification_retries_on_next_hourly_snapshot(self):
        self.set_state(SOURCES.clean, "on")
        self.app._async_send_mobile_notification.return_value = (0, 0)
        self.app._sync_dock_water_checks(NOW)
        await self.check_at(NOW + timedelta(minutes=5))
        self.assertEqual(
            self.app.state.robot_water[self.robot.registry_id].notified_faults, ()
        )
        self.app._async_send_mobile_notification.return_value = (1, 1)
        await self.check_at(NOW + timedelta(minutes=65))
        self.assertEqual(
            self.app.state.robot_water[self.robot.registry_id].notified_faults,
            ("clean",),
        )

    async def test_verification_checkpoints_and_requires_observed_completion(
        self,
    ):
        candidate = self.scheduled_mop()
        self.set_state(SOURCES.clean, "on")
        snapshots = []
        self.app._async_save.side_effect = lambda **_: snapshots.append(
            self.app.state.robot_water[self.robot.registry_id].verification.phase
        )
        self.assertFalse(
            await self.app._async_prepare_dock_water(self.robot, candidate, NOW)
        )
        self.assertEqual(snapshots[0], "requested")
        self.app.gateway.async_start_dock_wash.assert_awaited_once()
        self.assertEqual(
            self.app._robot_ready(self.robot), (False, "checking dock water")
        )
        self.assertFalse(
            await self.app._async_prepare_dock_water(self.robot, candidate, NOW)
        )
        self.app.gateway.async_start_dock_wash.assert_awaited_once()
        self.now += timedelta(seconds=20)
        self.set_state("sensor.unit_status", "washing_the_mop")
        self.app._sync_dock_water_checks(self.now)
        verification = self.app.state.robot_water[self.robot.registry_id].verification
        self.assertEqual(verification.phase, "washing")
        self.now = NOW + timedelta(minutes=4)
        self.set_state("sensor.unit_status", "charging")
        self.app._sync_dock_water_checks(self.now)
        self.assertEqual(verification.phase, "settling")
        self.assertEqual(verification.deadline, NOW + timedelta(minutes=9))
        self.set_state(SOURCES.clean, "off")
        await self.check_at(NOW + timedelta(minutes=8))
        self.assertEqual(verification.phase, "settling")
        await self.check_at(NOW + timedelta(minutes=9))
        self.assertEqual(verification.phase, "succeeded")
        self.assertFalse(self.app.state.active_jobs[self.robot.registry_id])
        self.assertFalse(self.app.state.battery_samples)
        self.assertTrue(
            await self.app._async_prepare_dock_water(self.robot, candidate, self.now)
        )

    async def test_command_acceptance_without_washing_times_out_and_skips_mop(self):
        candidate = self.scheduled_mop()
        self.set_state(SOURCES.dirty, "on")
        await self.app._async_prepare_dock_water(self.robot, candidate, NOW)
        await self.check_at(NOW + timedelta(minutes=2))
        verification = self.app.state.robot_water[self.robot.registry_id].verification
        self.assertEqual(verification.phase, "failed")
        self.assertNotIn("study", self.app.state.occurrences)
        self.assertFalse(self.app.state.robot_faults)
        self.assertFalse(self.app.state.room_faults)

    async def test_native_wash_timeout_never_dispatches_or_stops_active_hardware(self):
        candidate = self.scheduled_mop()
        self.set_state(SOURCES.clean, "on")
        await self.app._async_prepare_dock_water(self.robot, candidate, NOW)
        self.set_state("sensor.unit_status", "washing_the_mop")
        self.app._sync_dock_water_checks(NOW + timedelta(seconds=10))
        await self.check_at(NOW + timedelta(minutes=10))
        self.assertEqual(
            self.app.state.robot_water[self.robot.registry_id].verification.phase,
            "failed",
        )
        self.app.gateway.async_start_dock_wash.assert_awaited_once()
        self.app.dispatch.async_dispatch.assert_not_awaited()

    async def test_rejected_wash_is_logged_safely_and_next_occurrence_can_retry(self):
        candidate = self.scheduled_mop()
        self.set_state(SOURCES.clean, "on")
        self.app.gateway.async_start_dock_wash.side_effect = RuntimeError(
            "private vendor details"
        )
        with self.assertLogs(
            "custom_components.adaptive_robovacs.application.dock_water", level="ERROR"
        ):
            await self.app._async_prepare_dock_water(self.robot, candidate, NOW)
        verification = self.app.state.robot_water[self.robot.registry_id].verification
        self.assertEqual(verification.reason, "dock_water_check_rejected")
        self.assertNotIn(
            "private",
            str(self.app.state.robot_water[self.robot.registry_id].to_store()),
        )
        candidate = self.scheduled_mop()
        candidate.occurrence.occurrence_id = "next-occurrence"
        candidate = replace(candidate, occurrence_id="next-occurrence")
        self.app.gateway.async_start_dock_wash.side_effect = None
        await self.app._async_prepare_dock_water(self.robot, candidate, NOW)
        self.assertEqual(self.app.gateway.async_start_dock_wash.await_count, 2)

    async def test_party_observe_shutdown_and_failed_gates_never_start_wash(self):
        for gate in ("party", "observe", "shutdown", "robot", "occupancy"):
            with self.subTest(gate=gate):
                candidate = self.scheduled_mop()
                self.app.state.robot_water.clear()
                self.app.state.global_settings.party_mode = gate == "party"
                self.app.state.global_settings.observe_only = gate == "observe"
                self.app._closing = gate == "shutdown"
                self.app._robot_technically_ready.return_value = (
                    gate != "robot",
                    "readiness",
                )
                self.app._dispatch_adjacency_block_reason.return_value = (
                    "occupied" if gate == "occupancy" else None
                )
                self.set_state(SOURCES.clean, "on")
                await self.app._async_prepare_dock_water(self.robot, candidate, NOW)
        self.app.gateway.async_start_dock_wash.assert_not_awaited()
        self.app._closing = False

    async def test_unavailable_telemetry_and_manual_work_skip_without_testing(self):
        for value, source in (("unavailable", "scheduler"), ("on", "manual_dashboard")):
            self.app.state.robot_water.clear()
            candidate = replace(self.scheduled_mop(), source=source)
            self.set_state(SOURCES.clean, value)
            await self.app._async_prepare_dock_water(self.robot, candidate, NOW)
            self.assertNotIn("study", self.app.state.occurrences)
        self.app.gateway.async_start_dock_wash.assert_not_awaited()

    async def test_restore_does_not_replay_missed_checks_or_resend_wash(self):
        candidate = self.scheduled_mop()
        self.set_state(SOURCES.clean, "on")
        await self.app._async_prepare_dock_water(self.robot, candidate, NOW)
        self.app._stop_dock_water_timer()
        del self.app._dock_water_runtime
        self.now = NOW + timedelta(minutes=20)
        self.app._sync_dock_water_checks(self.now)
        await self.app._async_check_dock_water()
        self.app.gateway.async_start_dock_wash.assert_awaited_once()
        self.assertEqual(
            self.app.state.robot_water[self.robot.registry_id].verification.phase,
            "failed",
        )
        self.assertEqual(
            self.app._water_runtime().due[self.robot.registry_id],
            self.now + timedelta(hours=1),
        )

    async def test_shared_run_never_repeats_a_failed_wash_for_another_room(self):
        candidate = self.scheduled_mop()
        self.app.state.cleaning_runs[self.robot.registry_id] = CleaningRun(
            "run-one", NOW, ["study"], {"study": candidate.occurrence_id}
        )
        self.set_state(SOURCES.clean, "on")
        await self.app._async_prepare_dock_water(self.robot, candidate, NOW)
        await self.check_at(NOW + timedelta(minutes=2))
        candidate = self.scheduled_mop()
        await self.app._async_prepare_dock_water(self.robot, candidate, self.now)
        self.app.gateway.async_start_dock_wash.assert_awaited_once()

    async def test_lost_dock_and_manual_interruption_end_verification(self):
        candidate = self.scheduled_mop()
        self.set_state(SOURCES.clean, "on")
        await self.app._async_prepare_dock_water(self.robot, candidate, NOW)
        self.set_state("sensor.unit_status", "segment_cleaning")
        self.app._sync_dock_water_checks(NOW)
        self.assertEqual(
            self.app.state.robot_water[self.robot.registry_id].verification.reason,
            "dock_water_check_interrupted",
        )
        candidate = self.scheduled_mop()
        self.app.state.robot_water.clear()
        self.set_state("sensor.unit_status", "charging")
        await self.app._async_prepare_dock_water(self.robot, candidate, NOW)
        self.app.discovery = replace(self.app.discovery, robots={})
        self.app._sync_dock_water_checks(NOW)
        self.assertIsNone(self.app._water_runtime().deadline)

    async def test_shutdown_command_and_fired_timer_are_inert(self):
        self.app._sync_dock_water_checks(NOW)
        callback = self.points[-1][0]
        self.app._closing = True
        callback(NOW)
        await self.app._async_check_dock_water()
        self.assertFalse(self.tasks)
        self.assertFalse(self.app.state.robot_water)

    async def test_gateway_wash_and_context_use_discovered_sources_only(self):
        hass = SimpleNamespace(
            states=self.app.hass.states,
            services=SimpleNamespace(async_call=AsyncMock()),
        )
        gateway = HomeAssistantVacuumGateway(hass, lambda: True)
        await gateway.async_start_dock_wash(self.robot)
        hass.services.async_call.assert_awaited_once_with(
            "switch", "turn_on", {"entity_id": SOURCES.washing}, blocking=True
        )
        context = gateway._adapter_context(self.robot)
        self.assertEqual(context.dock_entities, self.robot.dock_entities)
        gateway = HomeAssistantVacuumGateway(hass, lambda: False)
        with self.assertRaises(RuntimeError):
            await gateway.async_start_dock_wash(self.robot)

    async def test_final_preflight_cannot_bypass_dock_fault_with_onboard_water(self):
        adapter = RoborockVacuumAdapter(GenericVacuumAdapter())
        adapter.async_capabilities = AsyncMock(
            return_value=self.robot.adapter_capabilities
        )
        adapter._is_q10_request = Mock(return_value=False)
        context = SimpleNamespace(supports_area_clean=False)
        request = AdapterDispatchRequest(
            self.robot.entity_id,
            ("study",),
            CleaningOperation.MOP,
            1,
            AdapterCleaningProfile(ignore_water_readiness=True),
        )
        self.set_state(SOURCES.clean, "on")
        blocked = await adapter.async_preflight(self.app.hass, context, request)
        self.assertEqual(blocked.code, "dock_water_unavailable")
        self.assertTrue(blocked.blocked)
        self.set_state(SOURCES.clean, "off")
        ready = await adapter.async_preflight(self.app.hass, context, request)
        self.assertTrue(ready.ready)
        self.set_state(SOURCES.dirty, "on")
        vacuum = replace(request, operation=CleaningOperation.VACUUM)
        self.assertTrue(
            (await adapter.async_preflight(self.app.hass, context, vacuum)).ready
        )

    async def test_projection_labels_snapshot_and_never_reads_live_tank_states(self):
        self.assertIsNone(
            robot_water_view(
                self.app,
                replace(
                    self.robot,
                    adapter_capabilities=replace(
                        self.robot.adapter_capabilities, dock_water=DockWaterEntities()
                    ),
                ),
            )
        )
        waiting = robot_water_attributes(robot_water_view(self.app, self.robot))
        self.assertEqual(waiting["water_snapshot_summary"], "Not sampled yet")
        self.app._sync_dock_water_checks(NOW)
        await self.check_at(NOW + timedelta(minutes=5))
        self.set_state(SOURCES.clean, "on")
        attributes = robot_water_attributes(robot_water_view(self.app, self.robot))
        self.assertIn("Clean tank: OK", attributes["water_tank_summary"])
        self.assertEqual(
            attributes["dock_water"]["last_snapshot_at"], self.now.isoformat()
        )
        self.assertEqual(robot_water_attributes(None), {})

    async def test_dedicated_command_routes_without_whole_house_evaluation(self):
        self.app._async_check_dock_water = AsyncMock()
        await self.app._async_execute_command(CheckDockWaterCommand())
        self.app._async_check_dock_water.assert_awaited_once()
        self.app.async_evaluate.assert_not_awaited()

    async def test_late_start_or_completion_cannot_bypass_verification_deadlines(self):
        for phase, minutes in (("requested", 3), ("washing", 11)):
            with self.subTest(phase=phase):
                self.app.state.robot_water.clear()
                self.now = NOW
                self.set_state("sensor.unit_status", "charging")
                self.set_state(SOURCES.clean, "on")
                candidate = self.scheduled_mop()
                await self.app._async_prepare_dock_water(self.robot, candidate, NOW)
                verification = self.app.state.robot_water[
                    self.robot.registry_id
                ].verification
                verification.phase = phase
                self.now = NOW + timedelta(minutes=minutes)
                self.set_state(
                    "sensor.unit_status",
                    "washing_the_mop" if phase == "requested" else "charging",
                )
                self.app._sync_dock_water_checks(self.now)
                self.assertEqual(verification.phase, "failed")
                self.assertEqual(verification.reason, "dock_water_check_timed_out")
                self.assertNotIn("study", self.app.state.occurrences)

    async def test_discovery_never_subscribes_to_dock_tank_changes(self):
        self.app._migrate_runtime_robot_identity = Mock(return_value=False)
        self.app._reconcile_unresolved_robot_references = Mock(return_value=False)
        self.app._sync_two_pass_issues = Mock()
        self.app._sync_cleaning_program_issues = Mock()
        with patch(
            "custom_components.adaptive_robovacs.application.core.async_discover",
            AsyncMock(return_value=self.app.discovery),
        ):
            await SchedulerApplication.async_refresh_discovery(self.app)
        for entity_id in (SOURCES.clean, SOURCES.dirty, SOURCES.error, SOURCES.washing):
            self.assertNotIn(entity_id, self.app._watch_entity_ids)
            self.app._on_state_changed(SimpleNamespace(data={"entity_id": entity_id}))
        self.assertEqual(self.app.metrics.state_events["ignored_unwatched"], 4)
        self.app.async_execute.assert_not_awaited()

    async def test_deadline_reads_only_due_robots(self):
        second = replace(
            self.robot, entity_id="vacuum.beta", registry_id="registry-beta"
        )
        self.app.discovery = replace(
            self.app.discovery,
            robots={self.robot.entity_id: self.robot, second.entity_id: second},
        )
        self.set_state(second.entity_id, "docked")
        self.app._sync_dock_water_checks(NOW)
        self.app._water_runtime().due[second.registry_id] = NOW + timedelta(minutes=10)
        original_get = self.app.hass.states.get
        reads = []

        def read(entity_id):
            reads.append(entity_id)
            return original_get(entity_id)

        self.app.hass.states.get = read
        await self.check_at(NOW + timedelta(minutes=5))
        self.assertNotIn(second.entity_id, reads)
        self.assertNotIn(second.registry_id, self.app.state.robot_water)
        self.assertEqual(
            self.app._water_runtime().deadline, NOW + timedelta(minutes=10)
        )
