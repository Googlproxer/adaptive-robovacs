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


models = _load(f"{PACKAGE_NAME}.models", PACKAGE_PATH / "models.py")
_load(f"{PACKAGE_NAME}.adapters.base", PACKAGE_PATH / "adapters" / "base.py")
_load(f"{PACKAGE_NAME}.adapters.generic", PACKAGE_PATH / "adapters" / "generic.py")
roborock = _load(
    f"{PACKAGE_NAME}.adapters.roborock", PACKAGE_PATH / "adapters" / "roborock.py"
)
registry = _load(
    f"{PACKAGE_NAME}.adapters.registry", PACKAGE_PATH / "adapters" / "registry.py"
)
base = sys.modules[f"{PACKAGE_NAME}.adapters.base"]
generic = sys.modules[f"{PACKAGE_NAME}.adapters.generic"]


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

    def test_reconciliation_prunes_only_segments_absent_from_live_response(self) -> None:
        reconciliation = roborock.reconcile_roborock_area_mapping(
            {
                "area_mapping": {
                    "upper_dunny": ["11", "4"],
                    "office": ["6"],
                    "unsupported": ["not-a-segment"],
                },
                "last_seen_segments": [{"id": "1"}, {"id": "4"}, {"id": "6"}],
            },
            (
                types.SimpleNamespace(id="1", name="Bedroom"),
                types.SimpleNamespace(id="4", name="Dunny"),
                types.SimpleNamespace(id="6", name="Office"),
            ),
        )

        self.assertIsNotNone(reconciliation)
        assert reconciliation is not None
        self.assertEqual(
            reconciliation.area_mapping,
            {
                "upper_dunny": ["4"],
                "office": ["6"],
                "unsupported": ["not-a-segment"],
            },
        )
        self.assertEqual(
            reconciliation.last_seen_segments,
            [
                {"id": "1", "name": "Bedroom"},
                {"id": "4", "name": "Dunny"},
                {"id": "6", "name": "Office"},
            ],
        )

    def test_reconciliation_never_changes_mapping_without_complete_live_evidence(self) -> None:
        options = {
            "area_mapping": {"upper_dunny": ["11", "4"]},
            "last_seen_segments": [{"id": "4"}],
        }

        self.assertIsNone(roborock.reconcile_roborock_area_mapping(options, ()))
        self.assertIsNone(
            roborock.reconcile_roborock_area_mapping(
                options, (types.SimpleNamespace(id="4", name=None),)
            )
        )

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

    def test_q10_cleaning_depth_transport_mapping_preserves_display_order(self) -> None:
        self.assertEqual(
            roborock.Q10_CLEANING_DEPTH_LINES,
            {"fast": 1, "daily": 0, "fine": 2},
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

    def test_only_attached_shortage_is_eligible_for_scheduled_revalidation(self) -> None:
        eligible, _ = roborock.resolve_roborock_water_readiness(
            (
                self.evidence("water_box_carriage_status", "on"),
                self.evidence("water_box_status", "on"),
                self.evidence("water_shortage", "on"),
            ),
            True,
        )
        self.assertTrue(eligible.revalidation_eligible)

        for states in (("off", "on", "on"), ("on", "off", "on"), ("on", "on", "unavailable")):
            readiness, _ = roborock.resolve_roborock_water_readiness(
                tuple(
                    self.evidence(key, state)
                    for key, state in zip(roborock.WATER_ENTITY_KEYS, states)
                ),
                True,
            )
            self.assertFalse(readiness.revalidation_eligible)


class RoborockWaterPreflightTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _capabilities(water):
        return models.AdapterCapabilities(
            adapter_id="roborock",
            schema_version=10,
            portable_area_clean=True,
            supported_pass_counts=frozenset({1}),
            supported_operations=frozenset({"vacuum", "mop"}),
            water_readiness=water,
        )

    @staticmethod
    def _context():
        return base.AdapterMatchContext(
            entity_id="vacuum.test",
            platform="roborock",
            supports_area_clean=True,
            supports_send_command=False,
            profile=types.SimpleNamespace(),
        )

    async def test_revalidation_bypass_requires_a_fresh_eligible_snapshot(self) -> None:
        adapter = roborock.RoborockVacuumAdapter(generic.GenericVacuumAdapter())
        request = base.AdapterDispatchRequest(
            "vacuum.test", ("room",), "mop", 1, {"ignore_water_readiness": True}
        )
        hass = types.SimpleNamespace()
        context = self._context()

        adapter.async_capabilities = AsyncMock(
            return_value=self._capabilities(
                models.WaterReadiness(
                    "sensor_blocked",
                    "water_unavailable",
                    authoritative=True,
                    revalidation_eligible=True,
                )
            )
        )
        self.assertTrue((await adapter.async_preflight(hass, context, request)).ready)

        adapter.async_capabilities.return_value = self._capabilities(
            models.WaterReadiness(
                "sensor_blocked", "water_telemetry_unavailable", authoritative=True
            )
        )
        blocked = await adapter.async_preflight(hass, context, request)
        self.assertTrue(blocked.blocked)
        self.assertEqual(blocked.code, "water_telemetry_unavailable")

    async def test_revalidation_bypass_never_skips_manual_water_confirmation(self) -> None:
        adapter = roborock.RoborockVacuumAdapter(generic.GenericVacuumAdapter())
        adapter.async_capabilities = AsyncMock(
            return_value=self._capabilities(models.WaterReadiness.confirmation_required())
        )
        result = await adapter.async_preflight(
            types.SimpleNamespace(),
            self._context(),
            base.AdapterDispatchRequest(
                "vacuum.test",
                ("room",),
                "mop",
                1,
                {"ignore_water_readiness": True},
            ),
        )
        self.assertTrue(result.blocked)
        self.assertEqual(result.code, "water_confirmation_required")

    async def test_mapping_reconciliation_requires_recheck_without_dispatching(self) -> None:
        adapter = roborock.RoborockVacuumAdapter(generic.GenericVacuumAdapter())
        adapter.async_capabilities = AsyncMock(
            return_value=self._capabilities(models.WaterReadiness.unsupported())
        )
        entry = types.SimpleNamespace(
            options={
                "vacuum": {
                    "area_mapping": {"upper_dunny": ["11", "4"]},
                    "last_seen_segments": [{"id": "1"}, {"id": "4"}, {"id": "11"}],
                }
            }
        )

        class Registry:
            def __init__(self) -> None:
                self.updated_options = None

            def async_get(self, _entity_id):
                return entry

            def async_update_entity_options(self, _entity_id, domain, options):
                self.updated_options = (domain, dict(options))
                entry.options = {domain: dict(options)}

        registry_instance = Registry()
        original_async_get = roborock.er.async_get
        roborock.er.async_get = lambda _hass: registry_instance
        self.addCleanup(setattr, roborock.er, "async_get", original_async_get)
        service_call = AsyncMock()
        vacuum_entity = types.SimpleNamespace(
            async_get_segments=AsyncMock(
                return_value=(
                    types.SimpleNamespace(id="1", name="Bedroom"),
                    types.SimpleNamespace(id="4", name="Dunny"),
                )
            )
        )
        hass = types.SimpleNamespace(
            data={
                "vacuum": types.SimpleNamespace(
                    get_entity=lambda _entity_id: vacuum_entity
                )
            },
            services=types.SimpleNamespace(async_call=service_call),
        )
        request = base.AdapterDispatchRequest(
            "vacuum.test", ("upper_dunny",), "vacuum", 1, {}
        )

        result = await adapter.async_dispatch(hass, self._context(), request)

        self.assertEqual(result.status, "mapping_error")
        self.assertEqual(result.code, "area_mapping_recheck_required")
        self.assertEqual(
            registry_instance.updated_options,
            (
                "vacuum",
                {
                    "area_mapping": {"upper_dunny": ["4"]},
                    "last_seen_segments": [
                        {"id": "1", "name": "Bedroom"},
                        {"id": "4", "name": "Dunny"},
                    ],
                },
            ),
        )
        service_call.assert_not_awaited()
        self.assertTrue((await adapter.async_preflight(hass, self._context(), request)).ready)


class RoborockReadinessTests(unittest.TestCase):
    def test_exactly_one_same_device_status_sensor_is_watched(self) -> None:
        status = base.AdapterEntityEvidence(
            entity_id="sensor.test_status",
            domain="sensor",
            platform="roborock",
            translation_key="status",
            device_class="enum",
            state="emptying_the_bin",
        )
        selected, watched = roborock.resolve_roborock_dispatch_readiness((status,))
        self.assertEqual(selected, "sensor.test_status")
        self.assertEqual(watched, ("sensor.test_status",))

    def test_missing_or_ambiguous_status_sensor_uses_generic_fallback(self) -> None:
        status = base.AdapterEntityEvidence(
            entity_id="sensor.test_status",
            domain="sensor",
            platform="roborock",
            translation_key="status",
            device_class="enum",
            state="charging",
        )
        self.assertEqual(
            roborock.resolve_roborock_dispatch_readiness(()), (None, ())
        )
        self.assertEqual(
            roborock.resolve_roborock_dispatch_readiness((status, status)), (None, ())
        )


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

    async def test_roborock_mop_start_evidence_uses_its_discovered_status_sensor(self) -> None:
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
                    entity_id="sensor.test_status",
                    domain="sensor",
                    platform="roborock",
                    translation_key="status",
                    device_class="enum",
                    state="washing_the_mop",
                ),
            ),
        )

        _adapter, capabilities, diagnostic = await registry.async_resolve_adapter(
            None, context
        )

        self.assertEqual(capabilities.readiness_entity_id, "sensor.test_status")
        self.assertEqual(capabilities.completion_status_entity_id, "sensor.test_status")
        self.assertEqual(
            capabilities.terminal_completion_states,
            frozenset({"charging", "charging_complete"}),
        )
        self.assertEqual(
            capabilities.mop_start_states, roborock.ROBOROCK_MOP_START_STATES
        )
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
        self.assertFalse(capabilities.native_mop_profile)
        self.assertIsNone(diagnostic)

    async def test_native_mop_profile_capability_is_advertised_only_for_qualifying_controls(self) -> None:
        _adapter, capabilities, diagnostic = await registry.async_resolve_adapter(
            None, self._native_mop_profile_context()
        )

        self.assertTrue(capabilities.native_mop_profile)
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

    @staticmethod
    def _mop_mode_context() -> base.AdapterMatchContext:
        profile = types.SimpleNamespace(
            supports_double_pass=False,
            supports_mopping=True,
            mode_options=("vacuum", "mop", "mop_only", "vac_and_mop"),
            mop_mode_options=("vacuum", "mop", "mop_only", "vac_and_mop"),
            mop_intensity_options=(),
            mode_select_entity_id="select.test_operation_mode",
            mop_mode_select_entity_id="select.test_operation_mode",
            mop_intensity_select_entity_id=None,
            passes_select_entity_id=None,
            passes_options=(),
        )
        return base.AdapterMatchContext(
            entity_id="vacuum.test",
            platform="generic",
            supports_area_clean=True,
            supports_send_command=False,
            profile=profile,
        )

    @staticmethod
    def _native_mop_profile_context(*, shared_controls: bool = False) -> base.AdapterMatchContext:
        profile = types.SimpleNamespace(
            supports_double_pass=False,
            supports_mopping=True,
            mode_options=("vacuum", "mop", "vac_and_mop"),
            mop_mode_options=("standard", "deep", "deep_plus", "fast", "smart_mode"),
            mop_intensity_options=("off", "low", "medium", "high", "smart_mode"),
            mode_select_entity_id="select.test_cleaning_mode",
            mop_mode_select_entity_id=(
                "select.test_cleaning_mode"
                if shared_controls
                else "select.test_mop_route"
            ),
            mop_intensity_select_entity_id=(
                "select.test_cleaning_mode"
                if shared_controls
                else "select.test_water_intensity"
            ),
            passes_select_entity_id=None,
            passes_options=(),
        )
        return base.AdapterMatchContext(
            entity_id="vacuum.test",
            platform="roborock",
            supports_area_clean=True,
            supports_send_command=True,
            profile=profile,
            fan_speed_options=("quiet", "balanced", "off", "custom"),
        )

    @staticmethod
    def _native_mop_profile_request(
        *, route: str = "deep", intensity: str = "high"
    ) -> base.AdapterDispatchRequest:
        return base.AdapterDispatchRequest(
            "vacuum.test",
            ("room",),
            "mop",
            1,
            {
                "mode": "mop",
                "fan_speed": "off",
                "mop_mode": route,
                "mop_intensity": intensity,
            },
        )

    def _native_mop_profile_hass(self):
        states = {
            "select.test_cleaning_mode": types.SimpleNamespace(
                state="vac_and_mop",
                attributes={"options": ["vacuum", "mop", "vac_and_mop"]},
            ),
            "select.test_mop_route": types.SimpleNamespace(
                state="smart_mode",
                attributes={"options": ["standard", "deep", "deep_plus", "fast", "smart_mode"]},
            ),
            "select.test_water_intensity": types.SimpleNamespace(
                state="off",
                attributes={"options": ["off", "low", "medium", "high", "smart_mode"]},
            ),
            "vacuum.test": types.SimpleNamespace(
                state="docked", attributes={"fan_speed": "max"}
            ),
        }
        return states, types.SimpleNamespace(
            states=types.SimpleNamespace(get=states.get),
        )

    def test_native_mop_profile_requires_independent_same_device_controls(self) -> None:
        self.assertTrue(
            roborock.supports_roborock_native_mop_profile(
                self._native_mop_profile_context()
            )
        )
        self.assertFalse(
            roborock.supports_roborock_native_mop_profile(
                self._native_mop_profile_context(shared_controls=True)
            )
        )

    async def test_native_mop_profile_applies_controls_in_safe_order_with_suction_off(self) -> None:
        states, hass = self._native_mop_profile_hass()
        calls: list[tuple[str, str, dict[str, object]]] = []

        async def service_call(domain, service, data, *, blocking):
            self.assertTrue(blocking)
            calls.append((domain, service, data))
            if domain == "select":
                states[data["entity_id"]].state = data["option"]
            else:
                states[data["entity_id"]].attributes["fan_speed"] = data["fan_speed"]

        hass.services = types.SimpleNamespace(async_call=service_call)
        result = await roborock.RoborockVacuumAdapter(
            generic.GenericVacuumAdapter()
        ).async_apply_profile(
            hass,
            self._native_mop_profile_context(),
            self._native_mop_profile_request(),
        )

        self.assertTrue(result.ready)
        self.assertEqual(
            calls,
            [
                ("select", "select_option", {"entity_id": "select.test_mop_route", "option": "deep"}),
                ("select", "select_option", {"entity_id": "select.test_water_intensity", "option": "high"}),
                ("select", "select_option", {"entity_id": "select.test_cleaning_mode", "option": "mop"}),
                ("vacuum", "set_fan_speed", {"entity_id": "vacuum.test", "fan_speed": "off"}),
            ],
        )
        self.assertEqual(states["vacuum.test"].attributes["fan_speed"], "off")
        self.assertNotIn("custom", {data.get("option") for _domain, _service, data in calls})
        self.assertNotIn(
            "vac_and_mop",
            {data.get("option") for _domain, _service, data in calls},
        )

    async def test_native_mop_profile_stabilizes_linked_roborock_controls(self) -> None:
        states, hass = self._native_mop_profile_hass()
        calls: list[tuple[str, str, dict[str, object]]] = []

        async def service_call(domain, service, data, *, blocking):
            self.assertTrue(blocking)
            calls.append((domain, service, data))
            if data.get("entity_id") == "select.test_mop_route":
                states["select.test_mop_route"].state = data["option"]
                states["select.test_cleaning_mode"].state = "vac_and_mop"
                states["vacuum.test"].attributes["fan_speed"] = "balanced"
            elif data.get("entity_id") == "select.test_water_intensity":
                states["select.test_water_intensity"].state = data["option"]
            elif data.get("entity_id") == "select.test_cleaning_mode":
                states["select.test_cleaning_mode"].state = data["option"]
            else:
                states["vacuum.test"].attributes["fan_speed"] = data["fan_speed"]

        hass.services = types.SimpleNamespace(async_call=service_call)
        result = await roborock.RoborockVacuumAdapter(
            generic.GenericVacuumAdapter()
        ).async_apply_profile(
            hass,
            self._native_mop_profile_context(),
            self._native_mop_profile_request(),
        )

        self.assertTrue(result.ready)
        self.assertEqual(states["select.test_cleaning_mode"].state, "mop")
        self.assertEqual(states["vacuum.test"].attributes["fan_speed"], "off")
        self.assertNotIn("custom", {data.get("option") for _, _, data in calls})
        self.assertNotIn("vac_and_mop", {data.get("option") for _, _, data in calls})

    async def test_native_mop_profile_deadline_is_a_safe_mop_block(self) -> None:
        _states, hass = self._native_mop_profile_hass()

        class TimeoutOnExit:
            async def __aenter__(self):
                return self

            async def __aexit__(self, _exc_type, _exc, _traceback):
                raise TimeoutError

        original_timeout = roborock.asyncio.timeout
        original_sleep = roborock.asyncio.sleep
        roborock.asyncio.timeout = lambda _seconds: TimeoutOnExit()
        roborock.asyncio.sleep = AsyncMock()
        self.addCleanup(setattr, roborock.asyncio, "timeout", original_timeout)
        self.addCleanup(setattr, roborock.asyncio, "sleep", original_sleep)
        hass.services = types.SimpleNamespace(async_call=AsyncMock())

        result = await roborock.RoborockVacuumAdapter(
            generic.GenericVacuumAdapter()
        ).async_apply_profile(
            hass,
            self._native_mop_profile_context(),
            self._native_mop_profile_request(),
        )

        self.assertTrue(result.blocked)
        self.assertEqual(result.code, "native_mop_profile_unconfirmed")

    async def test_native_mop_profile_write_error_is_a_safe_mop_block(self) -> None:
        _states, hass = self._native_mop_profile_hass()

        async def service_call(_domain, _service, _data, *, blocking):
            self.assertTrue(blocking)
            raise RuntimeError("native control rejected")

        hass.services = types.SimpleNamespace(async_call=service_call)
        result = await roborock.RoborockVacuumAdapter(
            generic.GenericVacuumAdapter()
        ).async_apply_profile(
            hass,
            self._native_mop_profile_context(),
            self._native_mop_profile_request(),
        )

        self.assertTrue(result.blocked)
        self.assertEqual(result.code, "native_mop_profile_apply_failed")

    async def test_native_mop_profile_retries_the_entire_profile_until_observed(self) -> None:
        states, hass = self._native_mop_profile_hass()
        calls: list[tuple[str, str, dict[str, object]]] = []

        async def service_call(domain, service, data, *, blocking):
            self.assertTrue(blocking)
            calls.append((domain, service, data))
            if len(calls) <= 4:
                return
            if domain == "select":
                states[data["entity_id"]].state = data["option"]
            else:
                states[data["entity_id"]].attributes["fan_speed"] = data["fan_speed"]

        sleep = AsyncMock()
        original_sleep = roborock.asyncio.sleep
        roborock.asyncio.sleep = sleep
        self.addCleanup(setattr, roborock.asyncio, "sleep", original_sleep)
        hass.services = types.SimpleNamespace(async_call=service_call)
        result = await roborock.RoborockVacuumAdapter(
            generic.GenericVacuumAdapter()
        ).async_apply_profile(
            hass,
            self._native_mop_profile_context(),
            self._native_mop_profile_request(),
        )

        self.assertTrue(result.ready)
        self.assertEqual(len(calls), 8)
        sleep.assert_awaited_once_with(
            roborock.NATIVE_MOP_PROFILE_RETRY_INTERVAL_SECONDS
        )

    async def test_native_mop_profile_timeout_blocks_before_any_clean_dispatch(self) -> None:
        _states, hass = self._native_mop_profile_hass()
        calls: list[tuple[str, str, dict[str, object]]] = []

        async def service_call(domain, service, data, *, blocking):
            self.assertTrue(blocking)
            calls.append((domain, service, data))

        sleep = AsyncMock()
        original_sleep = roborock.asyncio.sleep
        roborock.asyncio.sleep = sleep
        self.addCleanup(setattr, roborock.asyncio, "sleep", original_sleep)
        hass.services = types.SimpleNamespace(async_call=service_call)
        result = await roborock.RoborockVacuumAdapter(
            generic.GenericVacuumAdapter()
        ).async_apply_profile(
            hass,
            self._native_mop_profile_context(),
            self._native_mop_profile_request(),
        )

        self.assertTrue(result.blocked)
        self.assertEqual(result.code, "native_mop_profile_unconfirmed")
        self.assertEqual(len(calls), 28)
        self.assertEqual(sleep.await_count, 6)
        self.assertNotIn(
            ("vacuum", "clean_area"),
            {(domain, service) for domain, service, _data in calls},
        )

    async def test_native_mop_profile_rejects_nonconcrete_route_or_water(self) -> None:
        _states, hass = self._native_mop_profile_hass()
        hass.services = types.SimpleNamespace(async_call=AsyncMock())

        result = await roborock.RoborockVacuumAdapter(
            generic.GenericVacuumAdapter()
        ).async_validate_profile(
            hass,
            self._native_mop_profile_context(),
            self._native_mop_profile_request(route="smart_mode"),
        )

        self.assertTrue(result.blocked)
        self.assertEqual(result.code, "native_mop_profile_invalid")
        hass.services.async_call.assert_not_awaited()

    async def test_mop_profile_applies_shared_operation_selector_once(self) -> None:
        state = types.SimpleNamespace(
            state="vac_and_mop",
            attributes={"options": ["vacuum", "mop", "mop_only", "vac_and_mop"]},
        )

        async def service_call(_domain, _service, data, *, blocking):
            self.assertTrue(blocking)
            state.state = data["option"]

        hass = types.SimpleNamespace(
            states=types.SimpleNamespace(get=lambda _entity_id: state),
            services=types.SimpleNamespace(async_call=service_call),
        )
        result = await generic.GenericVacuumAdapter().async_apply_profile(
            hass,
            self._mop_mode_context(),
            base.AdapterDispatchRequest(
                "vacuum.test",
                ("room",),
                "mop",
                1,
                {"mode": "mop_only", "mop_mode": "vac_and_mop"},
            ),
        )

        self.assertTrue(result.ready)
        self.assertEqual(state.state, "mop_only")

    async def test_mop_profile_retries_until_mop_only_mode_is_observed(self) -> None:
        state = types.SimpleNamespace(
            state="vac_and_mop",
            attributes={"options": ["vacuum", "mop", "mop_only", "vac_and_mop"]},
        )
        calls: list[tuple[str, str, dict[str, object]]] = []

        async def service_call(domain, service, data, *, blocking):
            self.assertTrue(blocking)
            calls.append((domain, service, data))
            if len(calls) == 3:
                state.state = "mop_only"

        sleep = AsyncMock()
        original_sleep = base.asyncio.sleep
        base.asyncio.sleep = sleep
        self.addCleanup(setattr, base.asyncio, "sleep", original_sleep)
        hass = types.SimpleNamespace(
            states=types.SimpleNamespace(get=lambda _entity_id: state),
            services=types.SimpleNamespace(async_call=service_call),
        )

        result = await generic.GenericVacuumAdapter().async_apply_profile(
            hass,
            self._mop_mode_context(),
            base.AdapterDispatchRequest(
                "vacuum.test", ("room",), "mop", 1, {"mode": "mop_only"}
            ),
        )

        self.assertTrue(result.ready)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(domain == "select" for domain, _service, _data in calls))
        self.assertEqual(sleep.await_count, 2)

    async def test_mop_profile_timeout_never_dispatches_a_clean_area(self) -> None:
        state = types.SimpleNamespace(
            state="vac_and_mop",
            attributes={"options": ["vacuum", "mop", "mop_only", "vac_and_mop"]},
        )
        calls: list[tuple[str, str, dict[str, object]]] = []

        async def service_call(domain, service, data, *, blocking):
            self.assertTrue(blocking)
            calls.append((domain, service, data))

        sleep = AsyncMock()
        original_sleep = base.asyncio.sleep
        base.asyncio.sleep = sleep
        self.addCleanup(setattr, base.asyncio, "sleep", original_sleep)
        hass = types.SimpleNamespace(
            states=types.SimpleNamespace(get=lambda _entity_id: state),
            services=types.SimpleNamespace(async_call=service_call),
        )

        result = await generic.GenericVacuumAdapter().async_apply_profile(
            hass,
            self._mop_mode_context(),
            base.AdapterDispatchRequest(
                "vacuum.test", ("room",), "mop", 1, {"mode": "mop_only"}
            ),
        )

        self.assertTrue(result.blocked)
        self.assertEqual(result.code, "mop_only_mode_unconfirmed")
        self.assertEqual(len(calls), 7)
        self.assertTrue(all(domain == "select" for domain, _service, _data in calls))
        self.assertEqual(sleep.await_count, 6)

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
