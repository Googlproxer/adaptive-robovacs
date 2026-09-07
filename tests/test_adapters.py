"""Pure adapter schema and Roborock mapping tests."""

from __future__ import annotations

import base64
import types
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, call

from custom_components.adaptive_robovacs import models
from custom_components.adaptive_robovacs.adapters import (
    base,
    generic,
    registry,
    roborock,
)


def profile(**values: object):
    """Build the typed profile accepted at the adapter boundary."""

    return models.AdapterCleaningProfile(**values)


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
            with (
                self.subTest(code=code),
                self.assertRaises(roborock.RoborockMappingError) as raised,
            ):
                roborock.resolve_roborock_area_mapping(options, ["study"])
            self.assertEqual(raised.exception.code, code)

    def test_reconciliation_prunes_only_segments_absent_from_live_response(
        self,
    ) -> None:
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

    def test_reconciliation_never_changes_mapping_without_complete_live_evidence(
        self,
    ) -> None:
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

    def test_segment_evidence_rejects_every_incomplete_live_shape(self) -> None:
        self.assertIsNone(roborock._segment_parts(True))
        self.assertEqual(roborock._segment_parts(8), (None, 8))
        self.assertEqual(roborock._segment_parts("42_6"), ("42", 6))
        self.assertIsNone(roborock._segment_parts("42_room"))

        valid_mapping = {
            "area_mapping": {"study": ["42_6"]},
            "last_seen_segments": [{"id": "42_6"}],
        }
        invalid_segments = (
            ({"id": 6, "name": "Study"},),
            ({"id": "", "name": "Study"},),
            ({"id": "room", "name": "Study"},),
            ({"id": "42_6", "name": 6},),
            ({"id": "42_6", "name": "Study", "group": 1},),
            (
                {"id": "42_6", "name": "Study"},
                {"id": "42_6", "name": "Duplicate"},
            ),
        )
        for segments in invalid_segments:
            with self.subTest(segments=segments):
                self.assertIsNone(
                    roborock.reconcile_roborock_area_mapping(valid_mapping, segments)
                )

        with_group = roborock.reconcile_roborock_area_mapping(
            {
                "area_mapping": {"study": ["42_6"]},
                "last_seen_segments": [{"id": "42_7"}],
            },
            ({"id": "42_6", "name": "Study", "group": "Ground"},),
        )
        self.assertEqual(
            with_group.last_seen_segments,
            [{"id": "42_6", "name": "Study", "group": "Ground"}],
        )

    def test_stored_segment_evidence_and_reconciliation_fail_closed(self) -> None:
        for last_seen in (
            "42_6",
            ["42_6"],
            [{"name": "Study"}],
            [{"id": "room"}],
            [],
        ):
            with self.subTest(last_seen=last_seen):
                self.assertIsNone(
                    roborock._last_seen_segment_ids({"last_seen_segments": last_seen})
                )

        self.assertIsNone(
            roborock.reconcile_roborock_area_mapping(
                {"area_mapping": None}, ({"id": "1", "name": "Study"},)
            )
        )
        self.assertIsNone(
            roborock.reconcile_roborock_area_mapping(
                {"area_mapping": {1: ["1"]}},
                ({"id": "1", "name": "Study"},),
            )
        )
        unchanged = {
            "area_mapping": {"study": "legacy"},
            "last_seen_segments": [{"id": "2"}],
        }
        reconciliation = roborock.reconcile_roborock_area_mapping(
            unchanged, ({"id": "1", "name": "Study"},)
        )
        self.assertEqual(reconciliation.area_mapping, {"study": "legacy"})
        self.assertIsNone(
            roborock.reconcile_roborock_area_mapping(
                {
                    "area_mapping": {"study": ["1"]},
                    "last_seen_segments": [{"id": "1"}],
                },
                ({"id": "1", "name": "Study"},),
            )
        )
        removed = roborock.reconcile_roborock_area_mapping(
            {
                "area_mapping": {"study": ["2"]},
                "last_seen_segments": [{"id": "2"}],
            },
            ({"id": "1", "name": "Study"},),
        )
        self.assertEqual(removed.area_mapping, {})

    def test_mapping_resolution_classifies_all_unsafe_evidence(self) -> None:
        cases = (
            ({"area_mapping": {}}, "area_mapping_stale"),
            (
                {"area_mapping": {}, "last_seen_segments": []},
                "area_mapping_stale",
            ),
            (
                {
                    "area_mapping": {"study": "1"},
                    "last_seen_segments": [{"id": "1"}],
                },
                "area_mapping_missing",
            ),
            (
                {
                    "area_mapping": {"study": []},
                    "last_seen_segments": [{"id": "1"}],
                },
                "area_mapping_missing",
            ),
            (
                {
                    "area_mapping": {"study": [True]},
                    "last_seen_segments": [{"id": True}],
                },
                "area_mapping_ambiguous",
            ),
            (
                {
                    "area_mapping": {"study": ["42_1"]},
                    "last_seen_segments": [{"id": "room"}, {"id": "42_1"}],
                },
                "area_mapping_ambiguous",
            ),
            (
                {
                    "area_mapping": {"study": ["42_1"]},
                    "last_seen_segments": [{"id": "43_1"}, {"id": "42_1"}],
                },
                "area_mapping_ambiguous",
            ),
            (
                {
                    "area_mapping": {"study": ["42_1"]},
                    "last_seen_segments": [{"id": "43_1"}],
                },
                "area_mapping_stale",
            ),
        )
        for options, code in cases:
            with (
                self.subTest(code=code, options=options),
                self.assertRaises(roborock.RoborockMappingError) as raised,
            ):
                roborock.resolve_roborock_area_mapping(options, ["study"])
            self.assertEqual(raised.exception.code, code)

        too_many = [str(index) for index in range(1, 257)]
        with self.assertRaises(roborock.RoborockMappingError) as raised:
            roborock.resolve_roborock_area_mapping(
                {
                    "area_mapping": {"study": too_many},
                    "last_seen_segments": [{"id": value} for value in too_many],
                },
                ["study"],
            )
        self.assertEqual(raised.exception.code, "area_mapping_missing")

    def test_protocol_detection_defaults_safely_for_unknown_evidence(self) -> None:
        for options in (
            {},
            {"last_seen_segments": "6"},
            {"last_seen_segments": []},
            {"last_seen_segments": [{"name": "Study"}]},
            {"last_seen_segments": [{"id": "room"}]},
        ):
            with self.subTest(options=options):
                self.assertFalse(roborock.is_roborock_q10_protocol(options))
        self.assertTrue(
            roborock.supports_roborock_native_two_pass(
                {"last_seen_segments": "unknown"}
            )
        )
        self.assertTrue(
            roborock.supports_roborock_native_two_pass({"last_seen_segments": []})
        )
        self.assertTrue(
            roborock.supports_roborock_native_two_pass(
                {"last_seen_segments": [{"id": "room"}]}
            )
        )

    def test_q10_payload_validates_each_profile_dimension(self) -> None:
        cases = (
            ((), 4, 1, 1, "area_mapping_missing"),
            ((1,), 99, 1, 1, "profile_option_unsupported"),
            ((1,), 4, 4, 1, "adapter_request_unsupported"),
            ((1,), 4, 1, 99, "profile_option_unsupported"),
            ((True,), 4, 1, 1, "area_mapping_ambiguous"),
        )
        for targets, fan, count, line, code in cases:
            with (
                self.subTest(code=code),
                self.assertRaises(roborock.Q10CustomCleanError) as raised,
            ):
                roborock.build_q10_customer_clean_payload(
                    targets, fan_level=fan, clean_count=count, clean_line=line
                )
            self.assertEqual(raised.exception.code, code)


class AdapterBaseBehaviorTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def context(*, can_mutate=None, same_selector: bool = False):
        mode = "select.mode"
        return base.AdapterMatchContext(
            entity_id="vacuum.test",
            platform="generic",
            supports_area_clean=True,
            supports_send_command=False,
            profile=types.SimpleNamespace(
                supports_double_pass=True,
                supports_mopping=True,
                mode_options=("vacuum_only", "mop_only"),
                mop_mode_options=("standard",),
                mop_intensity_options=("medium",),
                mode_select_entity_id=mode,
                mop_mode_select_entity_id=mode if same_selector else "select.route",
                mop_intensity_select_entity_id="select.water",
                passes_select_entity_id="select.passes",
                passes_options=("One Pass", "Two Pass"),
            ),
            fan_speed_options=("quiet", "max"),
            can_mutate=can_mutate,
        )

    @staticmethod
    def hass(states=None):
        return types.SimpleNamespace(
            states=types.SimpleNamespace(get=(states or {}).get),
            services=types.SimpleNamespace(async_call=AsyncMock()),
        )

    async def test_profile_validation_reports_each_unsupported_boundary(self) -> None:
        adapter = generic.GenericVacuumAdapter()
        context = self.context()
        available = {
            "select.mode": types.SimpleNamespace(
                state="vacuum_only", attributes={"options": ["vacuum_only", "mop_only"]}
            ),
            "select.route": types.SimpleNamespace(
                state="standard", attributes={"options": ["standard"]}
            ),
            "select.water": types.SimpleNamespace(
                state="medium", attributes={"options": ["medium"]}
            ),
            "select.passes": types.SimpleNamespace(
                state="One Pass", attributes={"options": ["One Pass", "Two Pass"]}
            ),
        }
        cases = (
            (profile(mode=1), "profile_option_unsupported"),
            (profile(mode="removed"), "profile_option_unsupported"),
            (profile(mode="vacuum_only"), "profile_control_unavailable"),
            (profile(fan_speed="removed"), "profile_option_unsupported"),
            (profile(cleaning_depth="deep"), "profile_option_unsupported"),
        )
        for settings, code in cases:
            with self.subTest(settings=settings):
                states = available
                if settings.get("mode") == "vacuum_only":
                    states = {**available, "select.mode": None}
                result = await adapter.async_validate_profile(
                    self.hass(states),
                    context,
                    base.AdapterDispatchRequest(
                        "vacuum.test", ("study",), "vacuum", 1, settings
                    ),
                )
                self.assertEqual(result.code, code)

        context.profile.passes_options = ()
        available["select.passes"] = None
        result = await adapter.async_validate_profile(
            self.hass(available),
            context,
            base.AdapterDispatchRequest(
                "vacuum.test", ("study",), "vacuum", 2, profile()
            ),
        )
        self.assertEqual(result.code, "profile_control_unavailable")

    async def test_profile_apply_uses_portable_controls_in_safe_order(self) -> None:
        adapter = generic.GenericVacuumAdapter()
        context = self.context()
        states = {
            "select.mode": types.SimpleNamespace(
                state="mop_only", attributes={"options": ["mop_only"]}
            )
        }
        hass = self.hass(states)
        request = base.AdapterDispatchRequest(
            "vacuum.test",
            ("study",),
            "vacuum",
            2,
            profile(
                mode="vacuum_only",
                mop_mode="standard",
                mop_intensity="medium",
                fan_speed="max",
            ),
        )
        result = await adapter.async_apply_profile(hass, context, request)
        self.assertTrue(result.ready)
        calls = hass.services.async_call.await_args_list
        self.assertEqual(
            [item.args[0] for item in calls],
            ["select", "select", "vacuum", "select", "select"],
        )
        self.assertEqual(calls[-1].args[2]["entity_id"], "select.mode")

        shared = self.context(same_selector=True)
        hass = self.hass(states)
        await adapter.async_apply_profile(hass, shared, request)
        written = [
            item.args[2]["entity_id"]
            for item in hass.services.async_call.await_args_list
        ]
        self.assertEqual(written.count("select.mode"), 1)

    async def test_shutdown_callback_stops_before_each_profile_mutation(self) -> None:
        adapter = generic.GenericVacuumAdapter()
        requests = (
            profile(mop_mode="standard"),
            profile(fan_speed="max"),
            profile(),
            profile(mode="vacuum_only"),
        )
        for index, settings in enumerate(requests):
            with self.subTest(index=index):
                context = self.context(can_mutate=lambda: False)
                if index == 2:
                    request_passes = 2
                else:
                    request_passes = 1
                    if index != 3:
                        context = self.context(
                            can_mutate=lambda: False, same_selector=False
                        )
                hass = self.hass()
                result = await adapter.async_apply_profile(
                    hass,
                    context,
                    base.AdapterDispatchRequest(
                        "vacuum.test",
                        ("study",),
                        "vacuum",
                        request_passes,
                        settings,
                    ),
                )
                self.assertTrue(result.ready)
                hass.services.async_call.assert_not_awaited()

    async def test_mop_confirmation_handles_invalid_observed_and_shutdown_states(
        self,
    ) -> None:
        adapter = generic.GenericVacuumAdapter()
        context = self.context()
        hass = self.hass()
        invalid = await adapter._async_confirm_mop_only_mode(
            hass,
            context,
            base.AdapterDispatchRequest(
                "vacuum.test", ("study",), "mop", 1, profile(mode=None)
            ),
        )
        self.assertEqual(invalid.code, "mop_only_mode_unconfirmed")

        state = types.SimpleNamespace(state="mop_only", attributes={})
        hass = self.hass({"select.mode": state})
        ready = await adapter._async_confirm_mop_only_mode(
            hass,
            context,
            base.AdapterDispatchRequest(
                "vacuum.test", ("study",), "mop", 1, profile(mode="mop_only")
            ),
        )
        self.assertTrue(ready.ready)

        context = self.context(can_mutate=lambda: False)
        state.state = "vacuum_only"
        original_sleep = base.asyncio.sleep
        base.asyncio.sleep = AsyncMock()
        self.addCleanup(setattr, base.asyncio, "sleep", original_sleep)
        stopped = await adapter._async_confirm_mop_only_mode(
            hass,
            context,
            base.AdapterDispatchRequest(
                "vacuum.test", ("study",), "mop", 1, profile(mode="mop_only")
            ),
        )
        self.assertTrue(stopped.ready)

    async def test_generic_preflight_and_dispatch_cover_portable_contract(self) -> None:
        adapter = generic.GenericVacuumAdapter()
        context = self.context()
        hass = self.hass()
        unsupported = await adapter.async_preflight(
            hass,
            context,
            base.AdapterDispatchRequest("vacuum.test", (), "vacuum", 1, profile()),
        )
        self.assertEqual(unsupported.code, "adapter_request_unsupported")
        water = await adapter.async_preflight(
            hass,
            context,
            base.AdapterDispatchRequest("vacuum.test", ("study",), "mop", 1, profile()),
        )
        self.assertEqual(water.code, "water_confirmation_required")
        rejected = await adapter.async_dispatch(
            hass,
            context,
            base.AdapterDispatchRequest("vacuum.test", (), "vacuum", 1, profile()),
        )
        self.assertFalse(rejected.ready)
        accepted = await adapter.async_dispatch(
            hass,
            context,
            base.AdapterDispatchRequest(
                "vacuum.test", ("study",), "vacuum", 1, profile()
            ),
        )
        self.assertTrue(accepted.accepted)
        hass.services.async_call.assert_awaited_once()

    async def test_registry_ambiguity_probe_failure_and_lookup_fallback(self) -> None:
        capabilities = await generic.GenericVacuumAdapter().async_capabilities(
            None, self.context()
        )
        first = types.SimpleNamespace(
            adapter_id="one",
            priority=10,
            matches=lambda _context: True,
            async_capabilities=AsyncMock(return_value=capabilities),
        )
        second = types.SimpleNamespace(
            adapter_id="two",
            priority=10,
            matches=lambda _context: True,
            async_capabilities=AsyncMock(return_value=capabilities),
        )
        original = registry._REGISTERED
        registry._REGISTERED = (first, second)
        self.addCleanup(setattr, registry, "_REGISTERED", original)
        selected, _capabilities, diagnostic = await registry.async_resolve_adapter(
            None, self.context()
        )
        self.assertEqual(selected.adapter_id, "generic")
        self.assertEqual(diagnostic, "adapter_registration_ambiguous")

        first.priority = 11
        first.async_capabilities.side_effect = RuntimeError("private")
        selected, _capabilities, diagnostic = await registry.async_resolve_adapter(
            None, self.context()
        )
        self.assertEqual(selected.adapter_id, "generic")
        self.assertEqual(diagnostic, "adapter_probe_failed")
        self.assertIs(registry.adapter_for_id("missing"), registry._GENERIC)


class RoborockWaterTests(unittest.TestCase):
    @staticmethod
    def evidence(key: str, state: str):
        return base.AdapterEntityEvidence(
            entity_id=f"binary_sensor.{key}",
            domain="binary_sensor",
            platform="roborock",
            translation_key=key,
            device_class=None,
            state=state,
        )

    def test_complete_sensor_trio_is_authoritative(self) -> None:
        readiness, watched = roborock.resolve_roborock_water_readiness(
            (
                self.evidence("water_box_carriage_status", "on"),
                self.evidence("water_box_status", "on"),
                self.evidence("water_shortage", "off"),
            ),
            True,
        )
        self.assertEqual(readiness.status, "sensor_ready")
        self.assertTrue(readiness.ready)
        self.assertTrue(readiness.authoritative)
        self.assertEqual(len(watched), 3)

    def test_home_assistant_translation_keys_match_the_sensor_trio(self) -> None:
        readiness, _ = roborock.resolve_roborock_water_readiness(
            (
                self.evidence("mop_attached", "on"),
                self.evidence("water_box_attached", "on"),
                self.evidence("water_shortage", "off"),
            ),
            True,
        )
        self.assertEqual(readiness.status, "sensor_ready")

    def test_missing_or_duplicate_sensor_requires_confirmation(self) -> None:
        missing, _ = roborock.resolve_roborock_water_readiness(
            (self.evidence("water_box_status", "on"),), True
        )
        duplicate, _ = roborock.resolve_roborock_water_readiness(
            (
                self.evidence("water_box_carriage_status", "on"),
                self.evidence("water_box_status", "on"),
                self.evidence("water_box_status", "on"),
                self.evidence("water_shortage", "off"),
            ),
            True,
        )
        self.assertEqual(missing.status, "confirmation_required")
        self.assertEqual(duplicate.status, "confirmation_required")

    def test_unavailable_or_empty_authoritative_sensor_blocks_only_mopping(
        self,
    ) -> None:
        for states in (("on", "on", "unavailable"), ("on", "on", "on")):
            readiness, _ = roborock.resolve_roborock_water_readiness(
                tuple(
                    self.evidence(key, state)
                    for key, state in zip(
                        roborock.WATER_ENTITY_KEYS, states, strict=True
                    )
                ),
                True,
            )
            self.assertEqual(readiness.status, "sensor_blocked")
            self.assertFalse(readiness.ready)

    def test_only_attached_shortage_is_eligible_for_scheduled_revalidation(
        self,
    ) -> None:
        eligible, _ = roborock.resolve_roborock_water_readiness(
            (
                self.evidence("water_box_carriage_status", "on"),
                self.evidence("water_box_status", "on"),
                self.evidence("water_shortage", "on"),
            ),
            True,
        )
        self.assertTrue(eligible.revalidation_eligible)

        for states in (
            ("off", "on", "on"),
            ("on", "off", "on"),
            ("on", "on", "unavailable"),
        ):
            readiness, _ = roborock.resolve_roborock_water_readiness(
                tuple(
                    self.evidence(key, state)
                    for key, state in zip(
                        roborock.WATER_ENTITY_KEYS, states, strict=True
                    )
                ),
                True,
            )
            self.assertFalse(readiness.revalidation_eligible)


class RoborockWaterPreflightTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        original_async_get = roborock.er.async_get
        roborock.er.async_get = lambda _hass: types.SimpleNamespace(
            async_get=lambda _entity_id: None
        )
        self.addCleanup(setattr, roborock.er, "async_get", original_async_get)

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
            "vacuum.test",
            ("room",),
            "mop",
            1,
            profile(ignore_water_readiness=True),
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

    async def test_revalidation_bypass_never_skips_manual_water_confirmation(
        self,
    ) -> None:
        adapter = roborock.RoborockVacuumAdapter(generic.GenericVacuumAdapter())
        adapter.async_capabilities = AsyncMock(
            return_value=self._capabilities(
                models.WaterReadiness.confirmation_required()
            )
        )
        result = await adapter.async_preflight(
            types.SimpleNamespace(),
            self._context(),
            base.AdapterDispatchRequest(
                "vacuum.test",
                ("room",),
                "mop",
                1,
                profile(ignore_water_readiness=True),
            ),
        )
        self.assertTrue(result.blocked)
        self.assertEqual(result.code, "water_confirmation_required")

    async def test_mapping_reconciliation_requires_recheck_without_dispatching(
        self,
    ) -> None:
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
            "vacuum.test", ("upper_dunny",), "vacuum", 1, profile()
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
        self.assertTrue(
            (await adapter.async_preflight(hass, self._context(), request)).ready
        )


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
        self.assertEqual(roborock.resolve_roborock_dispatch_readiness(()), (None, ()))
        self.assertEqual(
            roborock.resolve_roborock_dispatch_readiness((status, status)), (None, ())
        )


class AdapterResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_robot_error_discovery_excludes_dock_errors_and_keeps_ambiguity(self):
        error = base.AdapterEntityEvidence(
            "sensor.native_error",
            "sensor",
            "roborock",
            "vacuum_error",
            "enum",
            "robot_trapped",
        )
        dock_error = replace(
            error, entity_id="sensor.dock_error", translation_key="dock_error"
        )
        context = replace(
            self._context("roborock", send_command=True), entities=(dock_error, error)
        )
        _, capabilities, _ = await registry.async_resolve_adapter(None, context)
        self.assertEqual(capabilities.error_entity_ids, (error.entity_id,))
        self.assertIn(error.entity_id, capabilities.watched_entity_ids)
        self.assertNotIn(dock_error.entity_id, capabilities.error_entity_ids)
        duplicate = replace(error, entity_id="sensor.second_error")
        _, capabilities, _ = await registry.async_resolve_adapter(
            None, replace(context, entities=(error, duplicate))
        )
        self.assertEqual(
            capabilities.error_entity_ids, (error.entity_id, duplicate.entity_id)
        )

    def setUp(self) -> None:
        original_async_get = roborock.er.async_get
        roborock.er.async_get = lambda _hass: types.SimpleNamespace(
            async_get=lambda _entity_id: None
        )
        self.addCleanup(setattr, roborock.er, "async_get", original_async_get)

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

    async def test_roborock_is_selected_from_platform_and_command_features(
        self,
    ) -> None:
        adapter, capabilities, diagnostic = await registry.async_resolve_adapter(
            None, self._context("roborock", send_command=True)
        )
        self.assertEqual(adapter.adapter_id, "roborock")
        self.assertEqual(capabilities.supported_pass_counts, frozenset({1, 2}))
        self.assertEqual(capabilities.native_area_pass_counts, frozenset({2}))
        self.assertEqual(capabilities.fan_speed_options, ("quiet", "max"))
        self.assertIsNone(diagnostic)

    async def test_roborock_mop_start_evidence_uses_its_discovered_status_sensor(
        self,
    ) -> None:
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

    def test_q10_profile_resolution_validates_control_state_and_fallbacks(
        self,
    ) -> None:
        context = self._q10_context()
        mode_entity_id = "select.test_cleaning_mode"
        states = {
            mode_entity_id: types.SimpleNamespace(
                state="vacuum", attributes={"options": ["vacuum", "customized"]}
            ),
            "vacuum.test": types.SimpleNamespace(
                state="docked", attributes={"fan_speed": "max"}
            ),
        }
        hass = types.SimpleNamespace(states=types.SimpleNamespace(get=states.get))

        def request(operation="vacuum", *, fan_speed=None, cleaning_depth=None):
            return base.AdapterDispatchRequest(
                "vacuum.test",
                ("study",),
                operation,
                2,
                profile(
                    fan_speed=fan_speed,
                    cleaning_depth=cleaning_depth,
                ),
            )

        no_control = base.AdapterMatchContext(
            entity_id=context.entity_id,
            platform=context.platform,
            supports_area_clean=True,
            supports_send_command=True,
            profile=context.profile,
            fan_speed_options=context.fan_speed_options,
        )
        cases = (
            (request("mop"), context, "adapter_request_unsupported"),
            (request(), no_control, "profile_control_unavailable"),
        )
        for candidate, candidate_context, code in cases:
            with (
                self.subTest(code=code),
                self.assertRaises(roborock.Q10CustomCleanError) as raised,
            ):
                roborock.resolve_q10_custom_clean_profile(
                    hass, candidate_context, candidate
                )
            self.assertEqual(raised.exception.code, code)

        for bad_state in (
            None,
            types.SimpleNamespace(state="unavailable", attributes={"options": []}),
            types.SimpleNamespace(state="vacuum", attributes={"options": ["vacuum"]}),
        ):
            with self.subTest(mode_state=bad_state):
                if bad_state is None:
                    states.pop(mode_entity_id, None)
                else:
                    states[mode_entity_id] = bad_state
                with self.assertRaises(roborock.Q10CustomCleanError) as raised:
                    roborock.resolve_q10_custom_clean_profile(hass, context, request())
                self.assertEqual(raised.exception.code, "profile_control_unavailable")

        states[mode_entity_id] = types.SimpleNamespace(
            state="vacuum", attributes={"options": ["customized"]}
        )
        states.pop("vacuum.test")
        with self.assertRaises(roborock.Q10CustomCleanError) as raised:
            roborock.resolve_q10_custom_clean_profile(hass, context, request())
        self.assertEqual(raised.exception.code, "profile_option_unsupported")

        states["vacuum.test"] = types.SimpleNamespace(
            state="docked", attributes={"fan_speed": "max"}
        )
        with self.assertRaises(roborock.Q10CustomCleanError) as raised:
            roborock.resolve_q10_custom_clean_profile(
                hass, context, request(fan_speed="unsupported")
            )
        self.assertEqual(raised.exception.code, "profile_option_unsupported")
        with self.assertRaises(roborock.Q10CustomCleanError) as raised:
            roborock.resolve_q10_custom_clean_profile(
                hass, context, request(cleaning_depth="unsupported")
            )
        self.assertEqual(raised.exception.code, "profile_option_unsupported")

        resolved = roborock.resolve_q10_custom_clean_profile(hass, context, request())
        self.assertEqual(resolved.fan_level, roborock.Q10_FAN_LEVELS["max"])
        self.assertEqual(resolved.clean_line, roborock.Q10_DEFAULT_CLEAN_LINE)

    async def test_native_mop_profile_requires_qualifying_controls(
        self,
    ) -> None:
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
    def _native_mop_profile_context(
        *, shared_controls: bool = False
    ) -> base.AdapterMatchContext:
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
            profile(
                mode="mop",
                fan_speed="off",
                mop_mode=route,
                mop_intensity=intensity,
            ),
        )

    def _native_mop_profile_hass(self):
        states = {
            "select.test_cleaning_mode": types.SimpleNamespace(
                state="vac_and_mop",
                attributes={"options": ["vacuum", "mop", "vac_and_mop"]},
            ),
            "select.test_mop_route": types.SimpleNamespace(
                state="smart_mode",
                attributes={
                    "options": ["standard", "deep", "deep_plus", "fast", "smart_mode"]
                },
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

    async def test_native_mop_profile_applies_controls_in_safe_order_with_suction_off(
        self,
    ) -> None:
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
                (
                    "select",
                    "select_option",
                    {"entity_id": "select.test_mop_route", "option": "deep"},
                ),
                (
                    "select",
                    "select_option",
                    {"entity_id": "select.test_water_intensity", "option": "high"},
                ),
                (
                    "select",
                    "select_option",
                    {"entity_id": "select.test_cleaning_mode", "option": "mop"},
                ),
                (
                    "vacuum",
                    "set_fan_speed",
                    {"entity_id": "vacuum.test", "fan_speed": "off"},
                ),
            ],
        )
        self.assertEqual(states["vacuum.test"].attributes["fan_speed"], "off")
        self.assertNotIn(
            "custom", {data.get("option") for _domain, _service, data in calls}
        )
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

    async def test_native_mop_profile_retries_the_entire_profile_until_observed(
        self,
    ) -> None:
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

    async def test_native_mop_profile_timeout_blocks_before_any_clean_dispatch(
        self,
    ) -> None:
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

    async def test_native_mop_profile_rejects_missing_values_and_live_controls(
        self,
    ) -> None:
        adapter = roborock.RoborockVacuumAdapter(generic.GenericVacuumAdapter())
        context = self._native_mop_profile_context()
        missing_value = base.AdapterDispatchRequest(
            "vacuum.test",
            ("room",),
            "mop",
            1,
            profile(mode="mop", fan_speed="off", mop_mode="deep"),
        )
        self.assertIsNone(adapter._native_mop_profile_values(context, missing_value))
        missing_option_context = replace(context, fan_speed_options=("max",))
        self.assertIsNone(
            adapter._native_mop_profile_values(
                missing_option_context, self._native_mop_profile_request()
            )
        )

        states, hass = self._native_mop_profile_hass()
        hass.services = types.SimpleNamespace(async_call=AsyncMock())
        del states["select.test_mop_route"]
        result = await adapter.async_validate_profile(
            hass, context, self._native_mop_profile_request()
        )
        self.assertEqual(result.code, "native_mop_profile_control_unavailable")

        states, hass = self._native_mop_profile_hass()
        hass.services = types.SimpleNamespace(async_call=AsyncMock())
        del states["vacuum.test"]
        result = await adapter.async_validate_profile(
            hass, context, self._native_mop_profile_request()
        )
        self.assertEqual(result.code, "native_mop_profile_control_unavailable")

        result = await adapter.async_apply_profile(hass, context, missing_value)
        self.assertEqual(result.code, "native_mop_profile_invalid")
        hass.services.async_call.assert_not_awaited()

    async def test_native_profile_shutdown_checks_bracket_all_mutations(self) -> None:
        adapter = roborock.RoborockVacuumAdapter(generic.GenericVacuumAdapter())
        _states, hass = self._native_mop_profile_hass()
        hass.services = types.SimpleNamespace(async_call=AsyncMock())
        request = self._native_mop_profile_request()

        before = replace(self._native_mop_profile_context(), can_mutate=lambda: False)
        result = await adapter.async_apply_profile(hass, before, request)
        self.assertTrue(result.ready)
        hass.services.async_call.assert_not_awaited()

        allowed = iter((True, False))
        between = replace(
            self._native_mop_profile_context(), can_mutate=lambda: next(allowed)
        )
        result = await adapter.async_apply_profile(hass, between, request)
        self.assertTrue(result.ready)
        self.assertEqual(hass.services.async_call.await_count, 3)

    async def test_roborock_profile_entrypoints_cover_q10_and_portable_paths(
        self,
    ) -> None:
        adapter = roborock.RoborockVacuumAdapter(generic.GenericVacuumAdapter())
        registry_entry = types.SimpleNamespace(
            options={
                "vacuum": {
                    "area_mapping": {"study": ["6"]},
                    "last_seen_segments": [{"id": "6"}],
                }
            }
        )
        roborock.er.async_get = lambda _hass: types.SimpleNamespace(
            async_get=lambda _entity_id: registry_entry
        )
        states = {
            "select.test_cleaning_mode": types.SimpleNamespace(
                state="vacuum", attributes={"options": ["customized"]}
            ),
            "vacuum.test": types.SimpleNamespace(
                state="docked", attributes={"fan_speed": "max"}
            ),
        }
        hass = types.SimpleNamespace(
            states=types.SimpleNamespace(get=states.get),
            services=types.SimpleNamespace(async_call=AsyncMock()),
        )
        invalid = base.AdapterDispatchRequest(
            "vacuum.test", ("study",), "vacuum", 2, profile(fan_speed="bad")
        )
        result = await adapter.async_validate_profile(
            hass, self._q10_context(), invalid
        )
        self.assertEqual(result.code, "profile_option_unsupported")

        valid = replace(invalid, cleaning_profile=profile(fan_speed="max"))
        result = await adapter.async_apply_profile(hass, self._q10_context(), valid)
        self.assertTrue(result.ready)
        hass.services.async_call.assert_not_awaited()

        portable_context = self._context("roborock", send_command=True)
        portable = base.AdapterDispatchRequest(
            "vacuum.test", ("study",), "vacuum", 1, profile()
        )
        registry_entry.options = {"vacuum": {}}
        result = await adapter.async_apply_profile(hass, portable_context, portable)
        self.assertTrue(result.ready)

    async def test_mapping_reconciliation_handles_unavailable_and_racing_sources(
        self,
    ) -> None:
        adapter = roborock.RoborockVacuumAdapter(generic.GenericVacuumAdapter())

        roborock.er.async_get = lambda _hass: None
        self.assertFalse(await adapter._async_reconcile_area_mapping(object(), "v"))

        entry = types.SimpleNamespace(
            options={
                "vacuum": {
                    "area_mapping": {"study": ["2"]},
                    "last_seen_segments": [{"id": "2"}],
                }
            }
        )
        registry_object = types.SimpleNamespace(async_get=lambda _entity_id: entry)
        roborock.er.async_get = lambda _hass: registry_object
        self.assertFalse(
            await adapter._async_reconcile_area_mapping(
                types.SimpleNamespace(data={}), "vacuum.test"
            )
        )

        entity = types.SimpleNamespace()
        hass = types.SimpleNamespace(
            data={"vacuum": types.SimpleNamespace(get_entity=lambda _entity_id: entity)}
        )
        self.assertFalse(
            await adapter._async_reconcile_area_mapping(hass, "vacuum.test")
        )

        entity.async_get_segments = AsyncMock(side_effect=RuntimeError("offline"))
        self.assertFalse(
            await adapter._async_reconcile_area_mapping(hass, "vacuum.test")
        )
        entity.async_get_segments = AsyncMock(return_value="invalid")
        self.assertFalse(
            await adapter._async_reconcile_area_mapping(hass, "vacuum.test")
        )
        entity.async_get_segments = AsyncMock(
            return_value=({"id": "2", "name": "Study"},)
        )
        self.assertFalse(
            await adapter._async_reconcile_area_mapping(hass, "vacuum.test")
        )

        entity.async_get_segments = AsyncMock(
            return_value=({"id": "1", "name": "Study"},)
        )
        latest_bad = types.SimpleNamespace(options={"vacuum": []})
        entries = iter((entry, latest_bad))
        registry_object.async_get = lambda _entity_id: next(entries)
        self.assertFalse(
            await adapter._async_reconcile_area_mapping(hass, "vacuum.test")
        )

        latest_same = types.SimpleNamespace(
            options={
                "vacuum": {
                    "area_mapping": {"study": ["1"]},
                    "last_seen_segments": [{"id": "1"}],
                }
            }
        )
        entries = iter((entry, latest_same))
        registry_object.async_get = lambda _entity_id: next(entries)
        self.assertFalse(
            await adapter._async_reconcile_area_mapping(hass, "vacuum.test")
        )

        entries = iter((entry, entry))
        registry_object.async_get = lambda _entity_id: next(entries)

        def fail_registry_write(*_args):
            raise RuntimeError("registry write failed")

        registry_object.async_update_entity_options = fail_registry_write
        self.assertFalse(
            await adapter._async_reconcile_area_mapping(hass, "vacuum.test")
        )

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
                profile(mode="mop_only", mop_mode="vac_and_mop"),
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
                "vacuum.test",
                ("room",),
                "mop",
                1,
                profile(mode="mop_only"),
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
                "vacuum.test",
                ("room",),
                "mop",
                1,
                profile(mode="mop_only"),
            ),
        )

        self.assertTrue(result.blocked)
        self.assertEqual(result.code, "mop_only_mode_unconfirmed")
        self.assertEqual(len(calls), 7)
        self.assertTrue(all(domain == "select" for domain, _service, _data in calls))
        self.assertEqual(sleep.await_count, 6)

    async def test_unprefixed_segment_mapping_uses_clean_area_for_one_pass(
        self,
    ) -> None:
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
                "vacuum.test", ("lego_room",), "vacuum", 1, profile()
            ),
        )

        self.assertTrue(result.accepted)
        service_call.assert_awaited_once_with(
            "vacuum",
            "clean_area",
            {"entity_id": "vacuum.test", "cleaning_area_id": ["lego_room"]},
            blocking=True,
        )

    async def test_unprefixed_segment_mapping_rejects_two_pass_before_dispatch(
        self,
    ) -> None:
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
                "vacuum.test", ("lego_room",), "vacuum", 2, profile()
            ),
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.code, "two_pass_no_longer_supported")
        service_call.assert_not_awaited()

    async def test_q10_dispatch_configures_customized_profile_then_starts_once(
        self,
    ) -> None:
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
            profile(fan_speed="max", cleaning_depth="fine"),
        )

        self.assertTrue(
            (
                await adapter.async_validate_profile(hass, self._q10_context(), request)
            ).ready
        )
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
            profile(fan_speed="max", cleaning_depth="fine"),
        )

        self.assertTrue(
            (
                await adapter.async_validate_profile(hass, self._q10_context(), request)
            ).ready
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
            "vacuum.test",
            ("test_room",),
            "vacuum",
            1,
            profile(fan_speed="max_plus"),
        )
        result = await adapter.async_validate_profile(
            hass,
            self._q10_context(),
            request,
        )

        self.assertTrue(result.ready)
        self.assertEqual(
            roborock.build_q10_customer_clean_payload(
                (6,),
                fan_level=roborock.Q10_FAN_LEVELS["max_plus"],
                clean_count=1,
                clean_line=0,
            ),
            "AQYIAAIBAA==",
        )
        service_call.assert_not_awaited()

    async def test_q10_max_plus_profile_write_failure_does_not_attempt_a_start(
        self,
    ) -> None:
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
                profile(fan_speed="max_plus", cleaning_depth="fast"),
            ),
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.code, "q10_max_plus_profile_write_failed")
        self.assertFalse(result.native_attempted)
        service_call.assert_awaited_once()
        sleep.assert_not_awaited()

    async def test_q10_dispatch_checks_shutdown_at_every_transaction_boundary(
        self,
    ) -> None:
        original_async_get = roborock.er.async_get
        original_sleep = roborock.asyncio.sleep
        registry_entry = types.SimpleNamespace(
            options={
                "vacuum": {
                    "area_mapping": {"study": ["6"]},
                    "last_seen_segments": [{"id": "6"}],
                }
            }
        )
        roborock.er.async_get = lambda _hass: types.SimpleNamespace(
            async_get=lambda _entity_id: registry_entry
        )
        roborock.asyncio.sleep = AsyncMock()
        self.addCleanup(setattr, roborock.er, "async_get", original_async_get)
        self.addCleanup(setattr, roborock.asyncio, "sleep", original_sleep)
        states = {
            "select.test_cleaning_mode": types.SimpleNamespace(
                state="vacuum", attributes={"options": ["customized"]}
            ),
            "vacuum.test": types.SimpleNamespace(
                state="docked", attributes={"fan_speed": "max"}
            ),
        }
        request = base.AdapterDispatchRequest(
            "vacuum.test", ("study",), "vacuum", 2, profile(fan_speed="max")
        )
        adapter = roborock.RoborockVacuumAdapter(generic.GenericVacuumAdapter())

        for permissions, expected_calls in (
            ((False,), 0),
            ((True, False), 1),
            ((True, True, False), 2),
        ):
            with self.subTest(permissions=permissions):
                allowed = iter(permissions)
                context = replace(
                    self._q10_context(),
                    can_mutate=lambda allowed=allowed: next(allowed),
                )
                service_call = AsyncMock()
                hass = types.SimpleNamespace(
                    states=types.SimpleNamespace(get=states.get),
                    services=types.SimpleNamespace(async_call=service_call),
                )
                result = await adapter.async_dispatch(hass, context, request)
                self.assertEqual(result.code, "coordinator_shutting_down")
                self.assertEqual(service_call.await_count, expected_calls)

    async def test_q10_and_legacy_dispatch_failures_keep_uncertainty_precise(
        self,
    ) -> None:
        original_async_get = roborock.er.async_get
        original_sleep = roborock.asyncio.sleep
        registry_entry = types.SimpleNamespace(
            options={
                "vacuum": {
                    "area_mapping": {"study": ["6"]},
                    "last_seen_segments": [{"id": "6"}],
                }
            }
        )
        roborock.er.async_get = lambda _hass: types.SimpleNamespace(
            async_get=lambda _entity_id: registry_entry
        )
        roborock.asyncio.sleep = AsyncMock()
        self.addCleanup(setattr, roborock.er, "async_get", original_async_get)
        self.addCleanup(setattr, roborock.asyncio, "sleep", original_sleep)
        states = {
            "select.test_cleaning_mode": types.SimpleNamespace(
                state="vacuum", attributes={"options": ["customized"]}
            ),
            "vacuum.test": types.SimpleNamespace(
                state="docked", attributes={"fan_speed": "max"}
            ),
        }
        context = self._q10_context()
        adapter = roborock.RoborockVacuumAdapter(generic.GenericVacuumAdapter())

        hass = types.SimpleNamespace(
            states=types.SimpleNamespace(get=states.get),
            services=types.SimpleNamespace(async_call=AsyncMock()),
        )
        invalid = base.AdapterDispatchRequest(
            "vacuum.test", ("study",), "vacuum", 2, profile(fan_speed="bad")
        )
        result = await adapter.async_dispatch(hass, context, invalid)
        self.assertEqual(result.code, "profile_option_unsupported")

        hass.services.async_call = AsyncMock(side_effect=RuntimeError("profile"))
        normal = replace(invalid, cleaning_profile=profile(fan_speed="max"))
        with self.assertRaisesRegex(RuntimeError, "profile"):
            await adapter.async_dispatch(hass, context, normal)

        hass.services.async_call = AsyncMock(
            side_effect=(None, None, RuntimeError("start uncertain"))
        )
        max_plus = replace(invalid, cleaning_profile=profile(fan_speed="max_plus"))
        result = await adapter.async_dispatch(hass, context, max_plus)
        self.assertEqual(result.code, "q10_max_plus_start_failed")
        self.assertTrue(result.native_attempted)
        self.assertTrue(result.outcome_uncertain)

        hass.services.async_call = AsyncMock(
            side_effect=(None, None, RuntimeError("start uncertain"))
        )
        with self.assertRaisesRegex(RuntimeError, "start uncertain"):
            await adapter.async_dispatch(hass, context, normal)

        registry_entry.options = {
            "vacuum": {
                "area_mapping": {"study": ["42_6"]},
                "last_seen_segments": [{"id": "42_6"}],
            }
        }
        service_call = AsyncMock()
        hass.services.async_call = service_call
        legacy_context = self._context("roborock", send_command=True)
        legacy_request = base.AdapterDispatchRequest(
            "vacuum.test", ("study",), "vacuum", 2, profile()
        )
        result = await adapter.async_dispatch(hass, legacy_context, legacy_request)
        self.assertTrue(result.accepted)
        self.assertTrue(result.native_attempted)
        service_call.assert_awaited_once_with(
            "vacuum",
            "send_command",
            {
                "entity_id": "vacuum.test",
                "command": "app_segment_clean",
                "params": [{"segments": [6], "repeat": 2}],
            },
            blocking=True,
        )

    async def test_roborock_preflight_returns_mapping_error_without_mutation(
        self,
    ) -> None:
        original_async_get = roborock.er.async_get
        registry_entry = types.SimpleNamespace(
            options={
                "vacuum": {
                    "last_seen_segments": [{"id": "42_6"}],
                }
            }
        )
        roborock.er.async_get = lambda _hass: types.SimpleNamespace(
            async_get=lambda _entity_id: registry_entry
        )
        self.addCleanup(setattr, roborock.er, "async_get", original_async_get)
        hass = types.SimpleNamespace(data={})
        adapter = roborock.RoborockVacuumAdapter(generic.GenericVacuumAdapter())
        result = await adapter.async_preflight(
            hass,
            self._context("roborock", send_command=True),
            base.AdapterDispatchRequest(
                "vacuum.test", ("study",), "vacuum", 2, profile()
            ),
        )
        self.assertEqual(result.code, "area_mapping_missing")

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
        self.assertEqual(capabilities.water_readiness.status, "confirmation_required")


if __name__ == "__main__":
    unittest.main()
