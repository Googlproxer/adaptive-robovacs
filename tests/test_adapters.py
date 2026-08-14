"""Pure adapter schema and Roborock mapping tests."""

from __future__ import annotations

import base64
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, call


PACKAGE_PATH = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs"
PACKAGE_NAME = "adaptive_robovacs_adapter_test"
package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(PACKAGE_PATH)]
sys.modules[PACKAGE_NAME] = package
adapters_package = types.ModuleType(f"{PACKAGE_NAME}.adapters")
adapters_package.__path__ = [str(PACKAGE_PATH / "adapters")]
sys.modules[adapters_package.__name__] = adapters_package

homeassistant = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
helpers = sys.modules.setdefault(
    "homeassistant.helpers", types.ModuleType("homeassistant.helpers")
)
entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
entity_registry.async_get = lambda _hass: None
sys.modules[entity_registry.__name__] = entity_registry
helpers.entity_registry = entity_registry
homeassistant.helpers = helpers


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_load(f"{PACKAGE_NAME}.models", PACKAGE_PATH / "models.py")
_load(f"{PACKAGE_NAME}.adapters.base", PACKAGE_PATH / "adapters" / "base.py")
_load(f"{PACKAGE_NAME}.adapters.generic", PACKAGE_PATH / "adapters" / "generic.py")
roborock = _load(
    f"{PACKAGE_NAME}.adapters.roborock", PACKAGE_PATH / "adapters" / "roborock.py"
)
registry = _load(
    f"{PACKAGE_NAME}.adapters.registry", PACKAGE_PATH / "adapters" / "registry.py"
)
base = sys.modules[f"{PACKAGE_NAME}.adapters.base"]


class RoborockMappingTests(unittest.TestCase):
    def test_numeric_mapping_preserves_order_and_deduplicates(self) -> None:
        result = roborock.resolve_roborock_area_mapping(
            {
                "area_mapping": {"study": [8, "8", "10"]},
                "last_seen_segments": [{"id": "8"}, {"id": "10"}],
            },
            ["study"],
        )
        self.assertEqual(result.targets, (8, 10))

    def test_single_map_compound_mapping_is_normalized(self) -> None:
        result = roborock.resolve_roborock_area_mapping(
            {
                "area_mapping": {"kitchen": ["42_6", "42_5"]},
                "last_seen_segments": [
                    {"id": "42_5"},
                    {"id": "42_6"},
                ],
            },
            ["kitchen"],
        )

        self.assertEqual(result.targets, (6, 5))

    def test_missing_stale_and_cross_map_mappings_fail_closed(self) -> None:
        cases = (
            (
                {"area_mapping": {}, "last_seen_segments": [{"id": "1"}]},
                "area_mapping_missing",
            ),
            (
                {
                    "area_mapping": {"study": ["2"]},
                    "last_seen_segments": [{"id": "1"}],
                },
                "area_mapping_stale",
            ),
            (
                {
                    "area_mapping": {"study": ["1_2", "2_3"]},
                    "last_seen_segments": [{"id": "1_2"}, {"id": "2_3"}],
                },
                "area_mapping_ambiguous",
            ),
        )
        for options, code in cases:
            with self.subTest(code=code), self.assertRaises(
                roborock.RoborockMappingError
            ) as raised:
                roborock.resolve_roborock_area_mapping(options, ["study"])
            self.assertEqual(raised.exception.code, code)

    def test_native_payload_uses_one_repeat_two_command(self) -> None:
        self.assertEqual(
            roborock.build_roborock_two_pass_payload((6, 5)),
            {
                "command": "app_segment_clean",
                "params": [{"segments": [6, 5], "repeat": 2}],
            },
        )

    def test_unprefixed_current_segments_do_not_support_legacy_two_pass(self) -> None:
        self.assertFalse(
            roborock.supports_roborock_native_two_pass(
                {"last_seen_segments": [{"id": "6"}, {"id": "10"}]}
            )
        )
        self.assertTrue(
            roborock.supports_roborock_native_two_pass(
                {"last_seen_segments": [{"id": "42_6"}, {"id": "42_10"}]}
            )
        )

    def test_q10_custom_payload_encodes_two_pass_vacuum_profile(self) -> None:
        encoded = roborock.build_q10_customer_clean_payload(
            (6, 10), fan_level=4, clean_count=2, clean_line=1
        )
        self.assertEqual(
            base64.b64decode(encoded),
            bytes((2, 6, 4, 0, 2, 2, 1, 10, 4, 0, 2, 2, 1)),
        )
        self.assertEqual(
            roborock.build_q10_start_payload((6, 10)),
            {
                "command": "dpStartClean",
                "params": {"cmd": 2, "clean_paramters": [6, 10]},
            },
        )

    def test_q10_custom_payload_rejects_non_byte_mapping_target(self) -> None:
        with self.assertRaises(roborock.Q10CustomCleanError) as raised:
            roborock.build_q10_customer_clean_payload(
                (256,), fan_level=4, clean_count=2, clean_line=1
            )
        self.assertEqual(raised.exception.code, "area_mapping_ambiguous")


class RoborockWaterTests(unittest.TestCase):
    @staticmethod
    def evidence(key: str, state: str):
        return base.AdapterEntityEvidence(
            entity_id=f"binary_sensor.{key}", domain="binary_sensor",
            platform="roborock", translation_key=key, device_class=None, state=state,
        )

    def test_complete_sensor_trio_is_authoritative(self) -> None:
        readiness, watched = roborock.resolve_roborock_water_readiness(
            (self.evidence("water_box_carriage_status", "on"),
             self.evidence("water_box_status", "on"),
             self.evidence("water_shortage", "off")), True)
        self.assertEqual(readiness.status, "sensor_ready")
        self.assertTrue(readiness.ready)
        self.assertTrue(readiness.authoritative)
        self.assertEqual(len(watched), 3)

    def test_home_assistant_translation_keys_match_the_sensor_trio(self) -> None:
        readiness, _ = roborock.resolve_roborock_water_readiness(
            (self.evidence("mop_attached", "on"),
             self.evidence("water_box_attached", "on"),
             self.evidence("water_shortage", "off")), True)
        self.assertEqual(readiness.status, "sensor_ready")

    def test_missing_or_duplicate_sensor_requires_confirmation(self) -> None:
        missing, _ = roborock.resolve_roborock_water_readiness(
            (self.evidence("water_box_status", "on"),), True)
        duplicate, _ = roborock.resolve_roborock_water_readiness(
            (self.evidence("water_box_carriage_status", "on"),
             self.evidence("water_box_status", "on"),
             self.evidence("water_box_status", "on"),
             self.evidence("water_shortage", "off")), True)
        self.assertEqual(missing.status, "confirmation_required")
        self.assertEqual(duplicate.status, "confirmation_required")

    def test_unavailable_or_empty_authoritative_sensor_blocks_only_mopping(self) -> None:
        for states in (("on", "on", "unavailable"), ("on", "on", "on")):
            readiness, _ = roborock.resolve_roborock_water_readiness(
                tuple(self.evidence(key, state)
                      for key, state in zip(roborock.WATER_ENTITY_KEYS, states)), True)
            self.assertEqual(readiness.status, "sensor_blocked")
            self.assertFalse(readiness.ready)


class AdapterResolverTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _context(platform: str, *, send_command: bool):
        profile = types.SimpleNamespace(
            supports_double_pass=False,
            supports_mopping=False,
            mode_options=(),
            mop_mode_options=(),
            mop_intensity_options=(),
            mode_select_entity_id=None,
            mop_mode_select_entity_id=None,
            mop_intensity_select_entity_id=None,
            passes_select_entity_id=None,
            passes_options=(),
        )
        return base.AdapterMatchContext(
            entity_id="vacuum.test",
            platform=platform,
            supports_area_clean=True,
            supports_send_command=send_command,
            profile=profile,
            fan_speed_options=("quiet", "max"),
        )

    async def test_unknown_vendor_gets_portable_generic_fallback(self) -> None:
        adapter, capabilities, diagnostic = await registry.async_resolve_adapter(
            None, self._context("unknown_vendor", send_command=False)
        )
        self.assertEqual(adapter.adapter_id, "generic")
        self.assertTrue(capabilities.portable_area_clean)
        self.assertEqual(capabilities.supported_pass_counts, frozenset({1}))
        self.assertIsNone(diagnostic)

    async def test_roborock_is_selected_from_platform_and_command_features(self) -> None:
        adapter, capabilities, diagnostic = await registry.async_resolve_adapter(
            None, self._context("roborock", send_command=True)
        )
        self.assertEqual(adapter.adapter_id, "roborock")
        self.assertEqual(capabilities.supported_pass_counts, frozenset({1, 2}))
        self.assertEqual(capabilities.native_area_pass_counts, frozenset({2}))
        self.assertEqual(capabilities.fan_speed_options, ("quiet", "max"))
        self.assertIsNone(diagnostic)

    async def test_unprefixed_segment_mapping_does_not_advertise_two_pass(self) -> None:
        original_async_get = roborock.er.async_get
        registry_entry = types.SimpleNamespace(
            options={"vacuum": {"last_seen_segments": [{"id": "6"}]}}
        )
        roborock.er.async_get = lambda _hass: types.SimpleNamespace(
            async_get=lambda _entity_id: registry_entry
        )
        self.addCleanup(setattr, roborock.er, "async_get", original_async_get)

        _adapter, capabilities, diagnostic = await registry.async_resolve_adapter(
            object(), self._context("roborock", send_command=True)
        )

        self.assertEqual(capabilities.supported_pass_counts, frozenset({1}))
        self.assertEqual(capabilities.native_area_pass_counts, frozenset())
        self.assertIsNone(diagnostic)

    async def test_q10_custom_clean_advertises_vacuum_two_pass_only(self) -> None:
        original_async_get = roborock.er.async_get
        registry_entry = types.SimpleNamespace(
            options={"vacuum": {"last_seen_segments": [{"id": "6"}]}}
        )
        roborock.er.async_get = lambda _hass: types.SimpleNamespace(
            async_get=lambda _entity_id: registry_entry
        )
        self.addCleanup(setattr, roborock.er, "async_get", original_async_get)
        context = self._q10_context()

        _adapter, capabilities, diagnostic = await registry.async_resolve_adapter(
            object(), context
        )

        self.assertEqual(capabilities.vacuum_pass_counts, frozenset({1, 2}))
        self.assertEqual(capabilities.native_vacuum_pass_counts, frozenset({1, 2}))
        self.assertEqual(capabilities.mop_pass_counts, frozenset({1}))
        self.assertEqual(capabilities.native_mop_pass_counts, frozenset())
        self.assertEqual(capabilities.cleaning_depth_options, ("fast", "daily", "fine"))
        self.assertIsNone(diagnostic)

    def _q10_context(self) -> base.AdapterMatchContext:
        context = self._context("roborock", send_command=True)
        return base.AdapterMatchContext(
            entity_id=context.entity_id,
            platform=context.platform,
            supports_area_clean=context.supports_area_clean,
            supports_send_command=context.supports_send_command,
            profile=context.profile,
            fan_speed_options=("quiet", "balanced", "turbo", "max", "max_plus"),
            entities=(
                base.AdapterEntityEvidence(
                    entity_id="select.test_cleaning_mode",
                    domain="select",
                    platform="roborock",
                    translation_key="cleaning_mode",
                    device_class=None,
                    state="vacuum",
                    options=("vac_and_mop", "vacuum", "mop", "customized"),
                ),
            ),
        )

    async def test_unprefixed_segment_mapping_uses_clean_area_for_one_pass(self) -> None:
        original_async_get = roborock.er.async_get
        registry_entry = types.SimpleNamespace(
            options={"vacuum": {"last_seen_segments": [{"id": "6"}]}}
        )
        roborock.er.async_get = lambda _hass: types.SimpleNamespace(
            async_get=lambda _entity_id: registry_entry
        )
        self.addCleanup(setattr, roborock.er, "async_get", original_async_get)
        service_call = AsyncMock()
        hass = types.SimpleNamespace(
            services=types.SimpleNamespace(async_call=service_call)
        )
        adapter, _capabilities, _diagnostic = await registry.async_resolve_adapter(
            hass, self._context("roborock", send_command=True)
        )

        result = await adapter.async_dispatch(
            hass,
            self._context("roborock", send_command=True),
            roborock.AdapterDispatchRequest(
                "vacuum.test", ("lego_room",), "vacuum", 1, {}
            ),
        )

        self.assertTrue(result.accepted)
        service_call.assert_awaited_once_with(
            "vacuum",
            "clean_area",
            {"entity_id": "vacuum.test", "cleaning_area_id": ["lego_room"]},
            blocking=True,
        )

    async def test_unprefixed_segment_mapping_rejects_two_pass_before_dispatch(self) -> None:
        original_async_get = roborock.er.async_get
        registry_entry = types.SimpleNamespace(
            options={"vacuum": {"last_seen_segments": [{"id": "6"}]}}
        )
        roborock.er.async_get = lambda _hass: types.SimpleNamespace(
            async_get=lambda _entity_id: registry_entry
        )
        self.addCleanup(setattr, roborock.er, "async_get", original_async_get)
        service_call = AsyncMock()
        hass = types.SimpleNamespace(
            services=types.SimpleNamespace(async_call=service_call)
        )
        adapter, _capabilities, _diagnostic = await registry.async_resolve_adapter(
            hass, self._context("roborock", send_command=True)
        )

        result = await adapter.async_dispatch(
            hass,
            self._context("roborock", send_command=True),
            roborock.AdapterDispatchRequest(
                "vacuum.test", ("lego_room",), "vacuum", 2, {}
            ),
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.code, "two_pass_no_longer_supported")
        service_call.assert_not_awaited()

    async def test_q10_dispatch_configures_customized_profile_then_starts_once(self) -> None:
        original_async_get = roborock.er.async_get
        original_sleep = roborock.asyncio.sleep
        registry_entry = types.SimpleNamespace(
            options={
                "vacuum": {
                    "area_mapping": {"test_room": ["6"]},
                    "last_seen_segments": [{"id": "6"}],
                }
            }
        )
        roborock.er.async_get = lambda _hass: types.SimpleNamespace(
            async_get=lambda _entity_id: registry_entry
        )
        sleep = AsyncMock()
        roborock.asyncio.sleep = sleep
        self.addCleanup(setattr, roborock.er, "async_get", original_async_get)
        self.addCleanup(setattr, roborock.asyncio, "sleep", original_sleep)
        service_call = AsyncMock()
        states = {
            "select.test_cleaning_mode": types.SimpleNamespace(
                state="vacuum", attributes={"options": ["vacuum", "customized"]}
            ),
            "vacuum.test": types.SimpleNamespace(
                state="docked", attributes={"fan_speed": "max"}
            ),
        }
        hass = types.SimpleNamespace(
            states=types.SimpleNamespace(get=states.get),
            services=types.SimpleNamespace(async_call=service_call),
        )
        adapter, capabilities, _diagnostic = await registry.async_resolve_adapter(
            hass, self._q10_context()
        )
        request = roborock.AdapterDispatchRequest(
            "vacuum.test",
            ("test_room",),
            "vacuum",
            2,
            {"fan_speed": "max", "cleaning_depth": "fine"},
        )

        self.assertTrue((await adapter.async_validate_profile(
            hass, self._q10_context(), request
        )).ready)
        result = await adapter.async_dispatch(hass, self._q10_context(), request)

        self.assertTrue(result.accepted)
        self.assertTrue(result.native_attempted)
        self.assertEqual(capabilities.native_vacuum_pass_counts, frozenset({1, 2}))
        service_call.assert_has_awaits(
            [
                call(
                    "vacuum",
                    "send_command",
                    {
                        "entity_id": "vacuum.test",
                        "command": "dpCommon",
                        "params": {
                            "62": roborock.build_q10_customer_clean_payload(
                                (6,), fan_level=4, clean_count=2, clean_line=2
                            )
                        },
                    },
                    blocking=True,
                ),
                call(
                    "select",
                    "select_option",
                    {"entity_id": "select.test_cleaning_mode", "option": "customized"},
                    blocking=True,
                ),
                call(
                    "vacuum",
                    "send_command",
                    {
                        "entity_id": "vacuum.test",
                        "command": "dpStartClean",
                        "params": {"cmd": 2, "clean_paramters": [6]},
                    },
                    blocking=True,
                ),
            ]
        )
        self.assertEqual(service_call.await_count, 3)
        sleep.assert_awaited_once_with(roborock.Q10_CUSTOM_CLEAN_SETTLE_SECONDS)

    async def test_q10_single_pass_depth_uses_a_custom_profile(self) -> None:
        original_async_get = roborock.er.async_get
        original_sleep = roborock.asyncio.sleep
        registry_entry = types.SimpleNamespace(
            options={
                "vacuum": {
                    "area_mapping": {"test_room": ["6"]},
                    "last_seen_segments": [{"id": "6"}],
                }
            }
        )
        roborock.er.async_get = lambda _hass: types.SimpleNamespace(
            async_get=lambda _entity_id: registry_entry
        )
        sleep = AsyncMock()
        roborock.asyncio.sleep = sleep
        self.addCleanup(setattr, roborock.er, "async_get", original_async_get)
        self.addCleanup(setattr, roborock.asyncio, "sleep", original_sleep)
        service_call = AsyncMock()
        states = {
            "select.test_cleaning_mode": types.SimpleNamespace(
                state="vacuum", attributes={"options": ["vacuum", "customized"]}
            ),
            "vacuum.test": types.SimpleNamespace(
                state="docked", attributes={"fan_speed": "max"}
            ),
        }
        hass = types.SimpleNamespace(
            states=types.SimpleNamespace(get=states.get),
            services=types.SimpleNamespace(async_call=service_call),
        )
        adapter, _capabilities, _diagnostic = await registry.async_resolve_adapter(
            hass, self._q10_context()
        )
        request = roborock.AdapterDispatchRequest(
            "vacuum.test",
            ("test_room",),
            "vacuum",
            1,
            {"fan_speed": "max", "cleaning_depth": "fine"},
        )

        self.assertTrue(
            (await adapter.async_validate_profile(hass, self._q10_context(), request)).ready
        )
        result = await adapter.async_dispatch(hass, self._q10_context(), request)

        self.assertTrue(result.accepted)
        service_call.assert_has_awaits(
            [
                call(
                    "vacuum",
                    "send_command",
                    {
                        "entity_id": "vacuum.test",
                        "command": "dpCommon",
                        "params": {
                            "62": roborock.build_q10_customer_clean_payload(
                                (6,), fan_level=4, clean_count=1, clean_line=2
                            )
                        },
                    },
                    blocking=True,
                ),
                call(
                    "select",
                    "select_option",
                    {"entity_id": "select.test_cleaning_mode", "option": "customized"},
                    blocking=True,
                ),
                call(
                    "vacuum",
                    "send_command",
                    {
                        "entity_id": "vacuum.test",
                        "command": "dpStartClean",
                        "params": {"cmd": 2, "clean_paramters": [6]},
                    },
                    blocking=True,
                ),
            ]
        )
        self.assertEqual(service_call.await_count, 3)
        sleep.assert_awaited_once_with(roborock.Q10_CUSTOM_CLEAN_SETTLE_SECONDS)

    async def test_q10_accepts_max_plus_with_the_protocol_fan_level(self) -> None:
        original_async_get = roborock.er.async_get
        registry_entry = types.SimpleNamespace(
            options={"vacuum": {"last_seen_segments": [{"id": "6"}]}}
        )
        roborock.er.async_get = lambda _hass: types.SimpleNamespace(
            async_get=lambda _entity_id: registry_entry
        )
        self.addCleanup(setattr, roborock.er, "async_get", original_async_get)
        service_call = AsyncMock()
        states = {
            "select.test_cleaning_mode": types.SimpleNamespace(
                state="vacuum", attributes={"options": ["vacuum", "customized"]}
            )
        }
        hass = types.SimpleNamespace(
            states=types.SimpleNamespace(get=states.get),
            services=types.SimpleNamespace(async_call=service_call),
        )
        adapter, _capabilities, _diagnostic = await registry.async_resolve_adapter(
            hass, self._q10_context()
        )

        request = roborock.AdapterDispatchRequest(
            "vacuum.test", ("test_room",), "vacuum", 1, {"fan_speed": "max_plus"}
        )
        result = await adapter.async_validate_profile(
            hass,
            self._q10_context(),
            request,
        )

        self.assertTrue(result.ready)
        self.assertEqual(
            roborock.build_q10_customer_clean_payload(
                (6,), fan_level=roborock.Q10_FAN_LEVELS["max_plus"], clean_count=1, clean_line=0
            ),
            "AQYIAAIBAA==",
        )
        service_call.assert_not_awaited()

    async def test_q10_max_plus_profile_write_failure_does_not_attempt_a_start(self) -> None:
        original_async_get = roborock.er.async_get
        original_sleep = roborock.asyncio.sleep
        registry_entry = types.SimpleNamespace(
            options={
                "vacuum": {
                    "area_mapping": {"test_room": ["6"]},
                    "last_seen_segments": [{"id": "6"}],
                }
            }
        )
        roborock.er.async_get = lambda _hass: types.SimpleNamespace(
            async_get=lambda _entity_id: registry_entry
        )
        sleep = AsyncMock()
        roborock.asyncio.sleep = sleep
        self.addCleanup(setattr, roborock.er, "async_get", original_async_get)
        self.addCleanup(setattr, roborock.asyncio, "sleep", original_sleep)
        service_call = AsyncMock(side_effect=RuntimeError("unsupported profile"))
        states = {
            "select.test_cleaning_mode": types.SimpleNamespace(
                state="vacuum", attributes={"options": ["vacuum", "customized"]}
            )
        }
        hass = types.SimpleNamespace(
            states=types.SimpleNamespace(get=states.get),
            services=types.SimpleNamespace(async_call=service_call),
        )
        adapter, _capabilities, _diagnostic = await registry.async_resolve_adapter(
            hass, self._q10_context()
        )

        result = await adapter.async_dispatch(
            hass,
            self._q10_context(),
            roborock.AdapterDispatchRequest(
                "vacuum.test",
                ("test_room",),
                "vacuum",
                1,
                {"fan_speed": "max_plus", "cleaning_depth": "fast"},
            ),
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.code, "q10_max_plus_profile_write_failed")
        self.assertFalse(result.native_attempted)
        service_call.assert_awaited_once()
        sleep.assert_not_awaited()

    async def test_same_device_operation_options_verify_mopping(self) -> None:
        context = self._context("roborock", send_command=True)
        context = base.AdapterMatchContext(
            entity_id=context.entity_id,
            platform=context.platform,
            supports_area_clean=context.supports_area_clean,
            supports_send_command=context.supports_send_command,
            profile=context.profile,
            fan_speed_options=context.fan_speed_options,
            entities=(
                base.AdapterEntityEvidence(
                    entity_id="select.test_cleaning_mode",
                    domain="select",
                    platform="roborock",
                    translation_key="cleaning_mode",
                    device_class=None,
                    state="vac_and_mop",
                    options=("vac_and_mop", "vacuum", "mop", "customized"),
                ),
            ),
        )
        _adapter, capabilities, _diagnostic = await registry.async_resolve_adapter(
            None, context
        )
        self.assertEqual(
            capabilities.supported_operations, frozenset({"vacuum", "mop"})
        )
        self.assertEqual(
            capabilities.water_readiness.status, "confirmation_required"
        )


if __name__ == "__main__":
    unittest.main()
