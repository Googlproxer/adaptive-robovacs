"""Behavioral tests for typed map recovery, storage, and cached previews."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from custom_components.adaptive_robovacs import map_recovery_store as map_store
from custom_components.adaptive_robovacs.adapters.roborock import (
    RoborockMappingError,
)
from custom_components.adaptive_robovacs.const import MAP_RECOVERY_RETENTION
from custom_components.adaptive_robovacs.discovery import (
    DiscoveredRobot,
    RobotProfile,
)
from custom_components.adaptive_robovacs.map_recovery import (
    MapRecoveryDependencies,
    MapRecoveryService,
)
from custom_components.adaptive_robovacs.map_recovery_models import (
    ArchivedMap,
    ArchivedRoomSummary,
    MapCaptureSet,
    MapRecoveryArchive,
    MapRecoveryError,
    MapRecoveryUnavailable,
    RetainedMap,
    RobotMapArchive,
)
from custom_components.adaptive_robovacs.map_recovery_store import (
    MapRecoveryLoadResult,
    MapRecoveryStorageError,
    decode_map_archive,
    encode_map_archive,
)
from custom_components.adaptive_robovacs.models import AdapterCapabilities
from custom_components.adaptive_robovacs.q10_map_frame import (
    Q10MapFrame,
    Q10MapRoom,
)
from custom_components.adaptive_robovacs.state import RobotHold

WHEN = datetime(2026, 9, 2, 11, 30, tzinfo=UTC)


def robot() -> DiscoveredRobot:
    return DiscoveredRobot(
        entity_id="vacuum.alpha",
        name="Alpha",
        registry_id="registry-alpha",
        platform="roborock",
        device_id="device-alpha",
        dock_area_id="dock",
        floor_id="ground",
        supports_area_clean=True,
        supports_send_command=True,
        profile=RobotProfile(),
        adapter_id="roborock",
        adapter_schema_version=2,
        adapter_capabilities=AdapterCapabilities(
            adapter_id="roborock",
            schema_version=2,
            portable_area_clean=True,
            supported_pass_counts=frozenset({1, 2}),
        ),
    )


def archived_map(map_id: str = "1", payload: bytes = b"packet") -> ArchivedMap:
    return ArchivedMap(
        map_id=map_id,
        name=f"Map {map_id}",
        robot_timestamp="2026-09-02T11:00:00Z",
        packet_sha256=hashlib.sha256(payload).hexdigest(),
        packet=payload,
        preview_png=b"png-data",
        width=2,
        height=2,
        rooms=(ArchivedRoomSummary(1, "Study", 0, 4),),
    )


def capture(snapshot_id: str = "snapshot-1") -> MapCaptureSet:
    record = archived_map()
    return MapCaptureSet(
        snapshot_id=snapshot_id,
        captured_at=WHEN,
        trigger="manual",
        combined_sha256=hashlib.sha256(record.packet_sha256.encode()).hexdigest(),
        maps=(record,),
        active_map_id="1",
    )


class MapRecoveryCodecTests(unittest.TestCase):
    def test_archive_round_trip_preserves_packets_previews_and_room_metadata(
        self,
    ) -> None:
        original = MapRecoveryArchive(
            robots={
                "registry-alpha": RobotMapArchive(
                    capture_sets=[capture()],
                    last_error="safe public error",
                )
            }
        )

        restored = decode_map_archive(encode_map_archive(original))

        self.assertEqual(encode_map_archive(restored), encode_map_archive(original))
        record = restored.robots["registry-alpha"].capture_sets[0].maps[0]
        self.assertEqual(record.packet, b"packet")
        self.assertEqual(record.preview_png, b"png-data")
        self.assertEqual(record.rooms[0].name, "Study")

    def test_empty_store_is_valid_but_malformed_or_newer_data_is_rejected(
        self,
    ) -> None:
        self.assertEqual(decode_map_archive(None), MapRecoveryArchive())
        invalid_payloads = (
            {},
            {"schema_version": 2, "robots": {}},
            {"schema_version": 1, "robots": []},
            {
                "schema_version": 1,
                "robots": {"registry-alpha": {"capture_sets": "invalid"}},
            },
        )
        for payload in invalid_payloads:
            with (
                self.subTest(payload=payload),
                self.assertRaises(MapRecoveryStorageError),
            ):
                decode_map_archive(payload)

    def test_boundary_scalar_codecs_reject_ambiguous_or_oversized_values(self) -> None:
        self.assertIsNone(map_store._string(None, "value", nullable=True))
        with self.assertRaisesRegex(MapRecoveryStorageError, "object"):
            map_store._mapping([], "value")
        for value in (None, "", 1):
            with self.subTest(string=value), self.assertRaises(MapRecoveryStorageError):
                map_store._string(value, "value")
        for value in (True, "1", -1):
            with (
                self.subTest(integer=value),
                self.assertRaises(MapRecoveryStorageError),
            ):
                map_store._integer(value, "value")
        with self.assertRaisesRegex(MapRecoveryStorageError, "ISO"):
            map_store._timestamp("not-a-date", "value")
        self.assertEqual(
            map_store._timestamp("2026-09-02T11:00:00", "value").tzinfo,
            UTC,
        )
        with self.assertRaisesRegex(MapRecoveryStorageError, "base64"):
            map_store._bytes("not base64!", "value", 10)
        oversized = base64.b64encode(b"123").decode()
        with self.assertRaisesRegex(MapRecoveryStorageError, "size limit"):
            map_store._bytes(oversized, "value", 2)

    def test_every_nested_archive_invariant_is_validated_before_use(self) -> None:
        valid = encode_map_archive(
            MapRecoveryArchive(robots={"registry-alpha": RobotMapArchive([capture()])})
        )

        def changed(mutator):
            value = copy.deepcopy(valid)
            mutator(value)
            return value

        variants = (
            changed(lambda value: value["robots"].update({"": {"capture_sets": []}})),
            changed(
                lambda value: value["robots"]["registry-alpha"].update(last_error="")
            ),
            changed(
                lambda value: value["robots"]["registry-alpha"]["capture_sets"][
                    0
                ].update(maps=[])
            ),
            changed(
                lambda value: value["robots"]["registry-alpha"]["capture_sets"][
                    0
                ].update(combined_sha256="short")
            ),
            changed(
                lambda value: value["robots"]["registry-alpha"]["capture_sets"][
                    0
                ].update(captured_at="bad")
            ),
            changed(
                lambda value: value["robots"]["registry-alpha"]["capture_sets"][0][
                    "maps"
                ][0].update(packet_sha256="short")
            ),
            changed(
                lambda value: value["robots"]["registry-alpha"]["capture_sets"][0][
                    "maps"
                ][0]["decoded_summary"].update(rooms="bad")
            ),
            changed(
                lambda value: value["robots"]["registry-alpha"]["capture_sets"][0][
                    "maps"
                ][0]["decoded_summary"].update(width=0)
            ),
            changed(
                lambda value: value["robots"]["registry-alpha"]["capture_sets"][0][
                    "maps"
                ][0]["decoded_summary"]["rooms"][0].update(name="")
            ),
        )
        for payload in variants:
            with (
                self.subTest(payload=payload),
                self.assertRaises(MapRecoveryStorageError),
            ):
                decode_map_archive(payload)


class MapRecoveryStoreWrapperTests(unittest.IsolatedAsyncioTestCase):
    async def test_load_is_safe_and_save_serializes_typed_archive(self) -> None:
        store = map_store.MapRecoveryStore.__new__(map_store.MapRecoveryStore)
        store._store = SimpleNamespace(
            async_load=AsyncMock(return_value=encode_map_archive(MapRecoveryArchive())),
            async_save=AsyncMock(),
        )
        loaded = await store.async_load()
        self.assertFalse(loaded.safe_mode)
        await store.async_save(loaded.archive)
        store._store.async_save.assert_awaited_once_with(
            encode_map_archive(loaded.archive)
        )

        store._store.async_load.side_effect = RuntimeError("private")
        loaded = await store.async_load()
        self.assertTrue(loaded.safe_mode)
        self.assertIn("malformed", loaded.error or "")


class _Store:
    def __init__(self, loaded: MapRecoveryLoadResult | None = None) -> None:
        self.loaded = loaded or MapRecoveryLoadResult(MapRecoveryArchive())
        self.saved: list[MapRecoveryArchive] = []

    async def async_load(self):
        return self.loaded

    async def async_save(self, archive):
        self.saved.append(archive)


class _Bridge:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.digest_seed = b"packet"
        self.maps = (RetainedMap("1", "Ground", "2026-09-02T11:00:00Z"),)

    async def async_list_maps(self):
        self.events.append("list")
        return self.maps

    async def async_get_map(self, map_id):
        self.events.append(f"get:{map_id}")
        packet = self.digest_seed + str(map_id).encode()
        return Q10MapFrame(
            map_id=str(map_id),
            width=2,
            height=2,
            grid=b"\x01\x01\x01\x01",
            rooms=(Q10MapRoom(1, "Study", 0, 4),),
            packet=packet,
            sha256=hashlib.sha256(packet).hexdigest(),
        )

    async def async_apply_map(self, map_id):
        self.events.append(f"apply:{map_id}")


class _Resolver:
    def __init__(self, bridge: _Bridge) -> None:
        self.bridge = bridge
        self.resolved: list[str] = []

    def async_resolve(self, discovered_robot):
        self.resolved.append(discovered_robot.registry_id)
        return self.bridge


class _States:
    def __init__(self) -> None:
        self.state = "docked"

    def get(self, _entity_id):
        return SimpleNamespace(state=self.state)


class MapRecoveryServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.robot = robot()
        self.hold = None
        self.active = None
        self.published = 0
        self.dependency_events: list[str] = []
        self.store = _Store()
        self.bridge = _Bridge()
        self.resolver = _Resolver(self.bridge)
        self.states = _States()
        self.hass = SimpleNamespace(
            states=self.states,
            async_create_task=lambda coroutine: __import__("asyncio").create_task(
                coroutine
            ),
        )

        async def set_hold(_registry_id, value):
            self.dependency_events.append("hold" if value else "release")
            self.bridge.events.append("hold" if value else "release")
            self.hold = value

        async def refresh():
            self.dependency_events.append("refresh")

        def publish():
            self.published += 1

        dependencies = MapRecoveryDependencies(
            robot_for_entity_id=(
                lambda entity_id: (
                    self.robot if entity_id == self.robot.entity_id else None
                )
            ),
            robot_for_registry_id=(
                lambda registry_id: (
                    self.robot if registry_id == self.robot.registry_id else None
                )
            ),
            hold_for_registry_id=lambda _registry_id: self.hold,
            active_job_for_registry_id=lambda _registry_id: self.active,
            dispatch_block_reason=lambda: None,
            async_set_hold=set_hold,
            async_refresh_discovery=refresh,
            publish_snapshot=publish,
        )
        self.service = MapRecoveryService(
            self.hass,
            "entry-1",
            dependencies,
            store=self.store,
            resolver=self.resolver,
        )
        await self.service.async_initialize()

    async def asyncTearDown(self) -> None:
        await self.service.async_shutdown()

    async def test_list_is_read_only_and_uses_the_existing_runtime_bridge(self) -> None:
        result = await self.service.async_list_maps("vacuum.alpha")

        self.assertEqual(result.retained_maps, self.bridge.maps)
        self.assertEqual(result.as_response()["retained_maps"][0]["map_id"], "1")
        self.assertEqual(self.store.saved, [])
        self.assertEqual(
            self.resolver.resolved,
            ["registry-alpha", "registry-alpha"],
        )

    async def test_capture_persists_before_publish_and_cached_preview_is_offline(
        self,
    ) -> None:
        with (
            patch(
                "custom_components.adaptive_robovacs.map_recovery."
                "render_q10_map_preview",
                return_value=b"rendered-preview",
            ),
            patch(
                "custom_components.adaptive_robovacs.map_recovery.async_dispatcher_send"
            ),
        ):
            result = await self.service.async_capture("vacuum.alpha")
            self.assertFalse(result.deduplicated)
            self.assertEqual(result.map_count, 1)
            self.assertEqual(len(self.store.saved), 1)
            self.assertEqual(self.published, 1)
            before = tuple(self.bridge.events)
            option = self.service.preview_options("vacuum.alpha")[0]
            self.service.select_preview_option("vacuum.alpha", option)
            self.assertEqual(
                self.service.selected_preview("vacuum.alpha"), b"rendered-preview"
            )
            self.assertEqual(tuple(self.bridge.events), before)

    async def test_passive_capture_deduplicates_and_history_is_bounded(self) -> None:
        with (
            patch(
                "custom_components.adaptive_robovacs.map_recovery."
                "render_q10_map_preview",
                return_value=b"preview",
            ),
            patch(
                "custom_components.adaptive_robovacs.map_recovery.async_dispatcher_send"
            ),
        ):
            first = await self.service.async_capture(
                "vacuum.alpha", trigger="post_clean"
            )
            duplicate = await self.service.async_capture(
                "vacuum.alpha", trigger="post_clean"
            )
            for index in range(MAP_RECOVERY_RETENTION + 2):
                self.bridge.digest_seed = f"packet-{index}".encode()
                await self.service.async_capture("vacuum.alpha", trigger="post_clean")

        self.assertEqual(duplicate.snapshot_id, first.snapshot_id)
        self.assertTrue(duplicate.deduplicated)
        self.assertEqual(
            self.service.summary("vacuum.alpha").capture_count,
            MAP_RECOVERY_RETENTION,
        )

    async def test_activation_checkpoints_hold_before_map_selection(self) -> None:
        with (
            patch(
                "custom_components.adaptive_robovacs.map_recovery."
                "render_q10_map_preview",
                return_value=b"preview",
            ),
            patch(
                "custom_components.adaptive_robovacs.map_recovery.async_dispatcher_send"
            ),
        ):
            result = await self.service.async_activate(
                "vacuum.alpha", "1", confirm=True
            )

        self.assertEqual(self.dependency_events[0], "hold")
        self.assertLess(
            self.bridge.events.index("hold"),
            self.bridge.events.index("apply:1"),
        )
        self.assertTrue(result.confirmed)
        self.assertEqual(self.hold.reason, "map_recovery_pending")
        self.assertTrue(result.as_response()["map_selection_pending"])

    async def test_verification_refreshes_then_releases_the_hold(self) -> None:
        self.hold = RobotHold(
            reason="map_recovery_pending",
            phase="manual_verification",
            requested_map_id="1",
            held_at=WHEN,
        )
        with (
            patch.object(self.service, "_preflight_room_mapping"),
            patch(
                "custom_components.adaptive_robovacs.map_recovery.async_dispatcher_send"
            ),
        ):
            result = await self.service.async_verify("vacuum.alpha", confirm=True)

        self.assertTrue(result.verified)
        self.assertEqual(self.dependency_events, ["refresh", "release"])
        self.assertIsNone(self.hold)

    async def test_safety_rejections_make_no_outbound_map_call(self) -> None:
        for state, active, expected in (
            ("cleaning", None, "docked or idle"),
            ("docked", object(), "active scheduler job"),
        ):
            with self.subTest(state=state, active=active):
                self.states.state = state
                self.active = active
                self.bridge.events.clear()
                with self.assertRaisesRegex(MapRecoveryError, expected):
                    await self.service.async_activate("vacuum.alpha", "1", confirm=True)
                self.assertEqual(self.bridge.events, [])

    async def test_malformed_archive_stays_read_only_and_unavailable(self) -> None:
        service = MapRecoveryService(
            self.hass,
            "entry-bad",
            self.service._dependencies,
            store=_Store(
                MapRecoveryLoadResult(
                    MapRecoveryArchive(),
                    "map capture storage is malformed or unavailable",
                )
            ),
            resolver=self.resolver,
        )
        await service.async_initialize()

        self.assertFalse(service.capability("vacuum.alpha").available)
        with self.assertRaisesRegex(MapRecoveryError, "malformed"):
            await service.async_capture("vacuum.alpha")

    def test_capability_summary_and_archive_guards_have_safe_diagnostics(self) -> None:
        self.assertFalse(self.service.capability("vacuum.missing").available)
        missing = self.service.summary("vacuum.missing")
        self.assertEqual(missing.state, "unavailable")
        self.assertEqual(missing.capture_count, 0)

        self.service._resolver.async_resolve = Mock(
            side_effect=MapRecoveryError("unsupported")
        )
        with self.assertRaises(MapRecoveryError):
            self.service.capability("vacuum.alpha")
        self.service._resolver.async_resolve = Mock(
            side_effect=MapRecoveryUnavailable("unsupported runtime")
        )
        capability = self.service.capability("vacuum.alpha")
        self.assertFalse(capability.available)
        self.assertEqual(capability.reason, "unsupported runtime")

        self.service._archive = None
        summary = self.service.summary("vacuum.alpha")
        self.assertEqual(summary.reason, "map capture storage is not initialized")
        with self.assertRaisesRegex(MapRecoveryError, "not initialized"):
            self.service._require_archive()

    async def test_lookup_lock_and_storage_guards_are_identity_scoped(self) -> None:
        self.assertIs(
            self.service._lock("registry-alpha"),
            self.service._lock("registry-alpha"),
        )
        self.assertFalse(self.service._is_held("registry-alpha"))
        self.assertTrue(self.service._terminal(self.robot))
        self.states.state = "cleaning"
        self.assertFalse(self.service._terminal(self.robot))
        with self.assertRaisesRegex(MapRecoveryError, "not discovered"):
            self.service._robot("vacuum.missing")
        with self.assertRaisesRegex(MapRecoveryError, "not discovered"):
            self.service._current_robot("registry-missing")

        self.service._storage_error = "storage unsafe"
        with self.assertRaisesRegex(MapRecoveryError, "storage unsafe"):
            await self.service._async_save()
        with self.assertRaisesRegex(MapRecoveryError, "storage unsafe"):
            self.service._robot("vacuum.alpha")

    async def test_list_wraps_unexpected_runtime_errors_but_preserves_safe_ones(
        self,
    ) -> None:
        self.bridge.async_list_maps = AsyncMock(
            side_effect=RuntimeError("private runtime detail")
        )
        with self.assertRaisesRegex(MapRecoveryError, "Could not retrieve"):
            await self.service.async_list_maps("vacuum.alpha")

        self.bridge.async_list_maps.side_effect = MapRecoveryError("safe")
        with self.assertRaisesRegex(MapRecoveryError, "safe"):
            await self.service.async_list_maps("vacuum.alpha")

    async def test_capture_rejects_physical_and_retained_map_invariants(self) -> None:
        self.states.state = "cleaning"
        with self.assertRaisesRegex(MapRecoveryError, "docked or idle"):
            await self.service.async_capture("vacuum.alpha")
        self.assertEqual(
            self.service.summary("vacuum.alpha").last_error,
            "robot must be docked or idle to capture maps",
        )

        self.states.state = "docked"
        for maps in ((), tuple(RetainedMap(str(i), f"Map {i}") for i in range(9))):
            with self.subTest(map_count=len(maps)):
                self.bridge.maps = maps
                with self.assertRaisesRegex(MapRecoveryError, "safe retained-map"):
                    await self.service.async_capture("vacuum.alpha")

    async def test_activation_rejects_each_precondition_before_map_mutation(
        self,
    ) -> None:
        with self.assertRaisesRegex(MapRecoveryError, "confirm"):
            await self.service.async_activate("vacuum.alpha", "1", confirm=False)

        original = self.service._dependencies
        self.service._dependencies = replace(
            original, dispatch_block_reason=lambda: "storage-safe mode"
        )
        with self.assertRaisesRegex(MapRecoveryError, "storage-safe"):
            await self.service.async_activate("vacuum.alpha", "1", confirm=True)
        self.service._dependencies = original

        self.hold = RobotHold("map_recovery_pending", "manual_verification")
        with self.assertRaisesRegex(MapRecoveryError, "already pending"):
            await self.service.async_activate("vacuum.alpha", "1", confirm=True)
        self.hold = None

        lock = self.service._lock("registry-alpha")
        await lock.acquire()
        try:
            with self.assertRaisesRegex(MapRecoveryError, "already running"):
                await self.service.async_activate("vacuum.alpha", "1", confirm=True)
        finally:
            lock.release()

        self.bridge.maps = (RetainedMap("2", "Other"),)
        with self.assertRaisesRegex(MapRecoveryError, "no longer retained"):
            await self.service.async_activate("vacuum.alpha", "1", confirm=True)

    async def test_activation_releases_checkpoint_if_pre_capture_fails(self) -> None:
        self.service._async_capture = AsyncMock(
            side_effect=MapRecoveryError("capture failed")
        )
        with self.assertRaisesRegex(MapRecoveryError, "capture failed"):
            await self.service.async_activate("vacuum.alpha", "1", confirm=True)
        self.assertEqual(self.dependency_events, ["hold", "release"])
        self.assertIsNone(self.hold)

    async def test_activation_confirmation_is_best_effort(self) -> None:
        with (
            patch.object(
                self.service,
                "_async_capture",
                AsyncMock(return_value=SimpleNamespace(snapshot_id="before")),
            ),
            patch(
                "custom_components.adaptive_robovacs.map_recovery.async_dispatcher_send"
            ),
        ):
            self.bridge.async_get_map = AsyncMock(
                return_value=SimpleNamespace(map_id="different")
            )
            result = await self.service.async_activate(
                "vacuum.alpha", "1", confirm=True
            )
            self.assertFalse(result.confirmed)

        self.hold = None
        self.dependency_events.clear()
        self.bridge.async_list_maps = AsyncMock(
            side_effect=(self.bridge.maps, MapRecoveryError("refresh failed"))
        )
        with (
            patch.object(
                self.service,
                "_async_capture",
                AsyncMock(return_value=SimpleNamespace(snapshot_id="before")),
            ),
            patch(
                "custom_components.adaptive_robovacs.map_recovery.async_dispatcher_send"
            ),
        ):
            result = await self.service.async_activate(
                "vacuum.alpha", "1", confirm=True
            )
        self.assertFalse(result.confirmed)

    async def test_verification_rejects_each_stale_or_unsafe_checkpoint(self) -> None:
        with self.assertRaisesRegex(MapRecoveryError, "confirm"):
            await self.service.async_verify("vacuum.alpha", confirm=False)
        with self.assertRaisesRegex(MapRecoveryError, "no map selection"):
            await self.service.async_verify("vacuum.alpha", confirm=True)

        self.hold = RobotHold("map_recovery_pending", "manual_verification")
        self.states.state = "cleaning"
        with self.assertRaisesRegex(MapRecoveryError, "docked or idle"):
            await self.service.async_verify("vacuum.alpha", confirm=True)
        self.states.state = "docked"

        lock = self.service._lock("registry-alpha")
        await lock.acquire()
        try:
            with self.assertRaisesRegex(MapRecoveryError, "already running"):
                await self.service.async_verify("vacuum.alpha", confirm=True)
        finally:
            lock.release()

        with self.assertRaisesRegex(MapRecoveryError, "no selected map"):
            await self.service.async_verify("vacuum.alpha", confirm=True)
        self.hold.requested_map_id = "missing"
        with self.assertRaisesRegex(MapRecoveryError, "no longer retained"):
            await self.service.async_verify("vacuum.alpha", confirm=True)
        self.hold.requested_map_id = "1"
        self.bridge.async_get_map = AsyncMock(
            return_value=SimpleNamespace(map_id="different")
        )
        with self.assertRaisesRegex(MapRecoveryError, "could not be verified"):
            await self.service.async_verify("vacuum.alpha", confirm=True)

    def test_mapping_preflight_normalizes_registry_failures(self) -> None:
        unsupported = SimpleNamespace(
            supports_area_clean=False, entity_id="vacuum.alpha"
        )
        with self.assertRaisesRegex(MapRecoveryError, "mapping is unavailable"):
            self.service._preflight_room_mapping(unsupported)

        registry = SimpleNamespace(async_get=Mock(return_value=None))
        with patch(
            "custom_components.adaptive_robovacs.map_recovery.er.async_get",
            return_value=registry,
        ):
            with self.assertRaisesRegex(MapRecoveryError, "mapping is unavailable"):
                self.service._preflight_room_mapping(self.robot)
            registry.async_get.return_value = SimpleNamespace(
                options={"vacuum": {"area_mapping": {"study": 1}}}
            )
            with (
                patch(
                    "custom_components.adaptive_robovacs.map_recovery."
                    "resolve_roborock_area_mapping",
                    side_effect=RoborockMappingError("mapping_bad", "bad"),
                ),
                self.assertRaisesRegex(MapRecoveryError, "refreshed"),
            ):
                self.service._preflight_room_mapping(self.robot)
            with patch(
                "custom_components.adaptive_robovacs.map_recovery."
                "resolve_roborock_area_mapping"
            ) as resolve:
                self.service._preflight_room_mapping(self.robot)
            resolve.assert_called_once()

    async def test_state_transitions_schedule_cancel_and_complete_passive_capture(
        self,
    ) -> None:
        self.service.async_capture = AsyncMock()
        with patch(
            "custom_components.adaptive_robovacs.map_recovery._SETTLE_DELAY",
            timedelta(0),
        ):
            self.service.handle_state_transition("vacuum.missing", None, "cleaning")
            self.service.handle_state_transition("vacuum.alpha", "docked", "cleaning")
            self.assertIn("registry-alpha", self.service._seen_cleaning)
            self.service.handle_state_transition("vacuum.alpha", "cleaning", "docked")
            await asyncio.gather(*self.service._settle_tasks.values())
        self.service.async_capture.assert_awaited_once_with(
            "vacuum.alpha", trigger="post_clean"
        )

        self.service._seen_cleaning.add("registry-alpha")
        task = asyncio.create_task(asyncio.sleep(10))
        self.service._settle_tasks["registry-alpha"] = task
        self.service.handle_state_transition("vacuum.alpha", "cleaning", "unavailable")
        await asyncio.gather(task, return_exceptions=True)
        self.assertNotIn("registry-alpha", self.service._seen_cleaning)

    async def test_preview_selection_handles_empty_stale_and_explicit_choices(
        self,
    ) -> None:
        self.assertIsNone(self.service.preview("vacuum.missing"))
        self.assertIsNone(self.service.preview("vacuum.alpha"))
        self.assertEqual(self.service.preview_options("vacuum.missing"), ())
        self.assertIsNone(self.service.selected_preview_option("vacuum.alpha"))
        self.assertIsNone(self.service.selected_preview("vacuum.alpha"))
        with self.assertRaisesRegex(MapRecoveryError, "no longer available"):
            self.service.select_preview_option("vacuum.alpha", "missing")

        self.service._robot_archive("registry-alpha").capture_sets = [capture()]
        options = self.service.preview_options("vacuum.alpha")
        self.assertEqual(len(options), 1)
        self.assertEqual(self.service.preview("vacuum.alpha", "missing", "1"), None)
        self.assertEqual(
            self.service.preview("vacuum.alpha", "snapshot-1", "missing"), None
        )
        with patch(
            "custom_components.adaptive_robovacs.map_recovery.async_dispatcher_send"
        ):
            self.service.select_preview_option("vacuum.alpha", options[0])
        self.assertEqual(
            self.service.selected_preview_option("vacuum.alpha"), options[0]
        )
        self.assertEqual(self.service.selected_preview("vacuum.alpha"), b"png-data")


if __name__ == "__main__":
    unittest.main()
