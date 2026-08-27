"""Tests for scheduler decisions that do not need a Home Assistant runtime."""

from __future__ import annotations

from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import sys
import unittest


MODULE_PATH = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs" / "models.py"
SPEC = importlib.util.spec_from_file_location("adaptive_robovacs_models", MODULE_PATH)
assert SPEC and SPEC.loader
models = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = models
SPEC.loader.exec_module(models)


class OccupancyTests(unittest.TestCase):
    def test_complete_radar_set_is_preferred(self) -> None:
        result = models.resolve_occupancy(["off", "off"], ["on"])
        self.assertEqual(result.state, "unoccupied")
        self.assertEqual(result.source, "radars")

    def test_available_radar_on_blocks_even_with_fallback_clear(self) -> None:
        result = models.resolve_occupancy(["on", "unavailable"], ["off"])
        self.assertEqual(result.state, "occupied")
        self.assertEqual(result.source, "radars")

    def test_incomplete_radar_uses_complete_clear_fallback(self) -> None:
        result = models.resolve_occupancy(["off", "unavailable"], ["off"])
        self.assertEqual(result.state, "unoccupied")
        self.assertEqual(result.source, "motion_fallback")

    def test_unconfigured_room_is_eligible(self) -> None:
        result = models.resolve_occupancy([], [])
        self.assertEqual(result.state, "unoccupied")
        self.assertEqual(result.source, "no_sensor")


class CadenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 7, 12, 0)

    def test_manual_clean_defers_only_near_due_work(self) -> None:
        self.assertEqual(
            models.manual_deferral(self.now, self.now + timedelta(hours=23)),
            self.now + timedelta(days=1),
        )
        self.assertIsNone(models.manual_deferral(self.now, self.now + timedelta(hours=25)))

    def test_manual_clean_accepts_only_a_docked_robot(self) -> None:
        self.assertTrue(models.manual_clean_robot_is_docked("docked"))
        self.assertFalse(models.manual_clean_robot_is_docked("idle"))
        self.assertFalse(models.manual_clean_robot_is_docked("cleaning"))
        self.assertFalse(models.manual_clean_robot_is_docked(None))

    def test_docked_robot_can_acknowledge_a_halt_without_dispatch_readiness(self) -> None:
        result = models.scheduler_halt_recheck_result("docked")
        self.assertTrue(result.cleared)
        self.assertEqual(result.reason, "cleared_docked")

    def test_cleaning_robot_can_acknowledge_a_halt_without_room_attribution(self) -> None:
        result = models.scheduler_halt_recheck_result("cleaning")
        self.assertTrue(result.cleared)
        self.assertEqual(result.reason, "cleared_cleaning")

    def test_halt_recheck_explains_unavailable_or_unsafe_robot_state(self) -> None:
        unavailable = models.scheduler_halt_recheck_result("unavailable")
        self.assertFalse(unavailable.cleared)
        self.assertEqual(unavailable.reason, "robot_state_unavailable")

        returning = models.scheduler_halt_recheck_result("returning")
        self.assertFalse(returning.cleared)
        self.assertEqual(returning.reason, "robot_not_docked_or_cleaning")

    def test_return_to_dock_is_available_until_the_robot_is_docked(self) -> None:
        self.assertTrue(models.can_request_return_to_dock("cleaning"))
        self.assertTrue(models.can_request_return_to_dock("paused"))
        self.assertFalse(models.can_request_return_to_dock("docked"))
        self.assertFalse(models.can_request_return_to_dock("unavailable"))
        self.assertFalse(models.can_request_return_to_dock(None))

    def test_map_recovery_hold_requires_explicit_verification(self) -> None:
        self.assertTrue(models.map_recovery_hold_is_manual("map_recovery_pending"))
        self.assertFalse(models.map_recovery_hold_is_manual("paused"))
        self.assertFalse(models.map_recovery_hold_is_manual(None))

    def test_room_pass_override_never_downgrades_an_unsupported_request(self) -> None:
        self.assertEqual(models.resolve_pass_count(None, False, {1, 2}), 1)
        self.assertEqual(models.resolve_pass_count(None, True, {1, 2}), 2)
        self.assertEqual(models.resolve_pass_count(1, True, {1, 2}), 1)
        self.assertEqual(models.resolve_pass_count(2, False, {1, 2}), 2)
        self.assertIsNone(models.resolve_pass_count(2, True, {1}))

    def test_cleaning_programs_expand_into_ordered_physical_stages(self) -> None:
        self.assertEqual(models.expand_cleaning_program("vacuum_only"), ("vacuum",))
        self.assertEqual(models.expand_cleaning_program("mop_only"), ("mop",))
        self.assertEqual(
            models.expand_cleaning_program("vacuum_then_mop"), ("vacuum", "mop")
        )
        self.assertEqual(
            models.expand_cleaning_program("mop_then_vacuum"), ("mop", "vacuum")
        )

    def test_vacuum_and_mop_passes_resolve_independently(self) -> None:
        capabilities = models.AdapterCapabilities(
            adapter_id="test",
            schema_version=2,
            portable_area_clean=True,
            supported_pass_counts=frozenset({1, 2}),
            supported_operations=frozenset({"vacuum", "mop"}),
            vacuum_pass_counts=frozenset({1, 2}),
            mop_pass_counts=frozenset({1, 2}),
        )
        self.assertEqual(
            models.stage_pass_count("vacuum", None, None, True, False, capabilities), 2
        )
        self.assertEqual(
            models.stage_pass_count("mop", None, None, True, False, capabilities), 1
        )

    def test_operation_specific_native_passes_do_not_inherit_to_mopping(self) -> None:
        capabilities = models.AdapterCapabilities(
            adapter_id="q10",
            schema_version=4,
            portable_area_clean=True,
            supported_pass_counts=frozenset({1, 2}),
            native_area_pass_counts=frozenset({2}),
            supported_operations=frozenset({"vacuum", "mop"}),
            vacuum_pass_counts=frozenset({1, 2}),
            mop_pass_counts=frozenset({1}),
            native_vacuum_pass_counts=frozenset({2}),
            native_mop_pass_counts=frozenset(),
        )
        self.assertEqual(capabilities.native_pass_counts_for("vacuum"), frozenset({2}))
        self.assertEqual(capabilities.native_pass_counts_for("mop"), frozenset())

    def test_due_at_honours_later_manual_deferral(self) -> None:
        result = models.due_at(
            self.now - timedelta(hours=100),
            84,
            self.now + timedelta(hours=10),
            self.now,
        )
        self.assertEqual(result, self.now + timedelta(hours=10))

    def test_due_at_ignores_a_deferral_beyond_the_cleaning_cadence(self) -> None:
        last_cleaned = self.now - timedelta(hours=1)
        result = models.due_at(
            last_cleaned,
            48,
            self.now + timedelta(days=5),
            self.now,
        )
        self.assertEqual(result, last_cleaned + timedelta(hours=48))

    def test_time_until_uses_only_the_largest_whole_unit(self) -> None:
        self.assertEqual(
            models.format_time_until(
                self.now + timedelta(days=1, hours=2, minutes=3, seconds=1), self.now
            ),
            "in 1 day",
        )
        self.assertEqual(
            models.format_time_until(self.now + timedelta(hours=2, minutes=59), self.now),
            "in 2 hours",
        )
        self.assertEqual(
            models.format_time_until(self.now + timedelta(seconds=1), self.now),
            "in 1 minute",
        )

    def test_conservative_forecast_waits_for_clear_duration(self) -> None:
        result = models.forecast_vacancy(
            [], self.now, self.now - timedelta(minutes=20), 30, 80, 6
        )
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "waiting for 30 clear minutes")
        result = models.forecast_vacancy(
            [], self.now, self.now - timedelta(minutes=30), 30, 80, 6
        )
        self.assertTrue(result.allowed)

    def test_hall_window_is_half_open(self) -> None:
        self.assertTrue(models.in_daytime_window(self.now, "09:00", "20:00"))
        self.assertFalse(models.in_daytime_window(self.now.replace(hour=20), "09:00", "20:00"))

    def test_night_window_can_cross_midnight(self) -> None:
        self.assertTrue(models.in_daytime_window(self.now.replace(hour=2), "22:00", "05:00"))
        self.assertFalse(models.in_daytime_window(self.now.replace(hour=12), "22:00", "05:00"))

    def test_next_window_start_uses_today_before_the_window_and_tomorrow_after_it(self) -> None:
        self.assertEqual(
            models.next_window_start(self.now.replace(hour=0, minute=30), "01:00"),
            self.now.replace(hour=1, minute=0),
        )
        self.assertEqual(
            models.next_window_start(self.now, "01:00"),
            self.now.replace(hour=1, minute=0) + timedelta(days=1),
        )

    def test_desired_window_defers_default_rooms_but_allows_the_room_override(self) -> None:
        daytime = self.now.replace(hour=12)
        after_hours = self.now.replace(hour=21)
        self.assertTrue(models.desired_window_allows(False, daytime, "09:00", "20:00"))
        self.assertFalse(models.desired_window_allows(False, after_hours, "09:00", "20:00"))
        self.assertTrue(models.desired_window_allows(True, after_hours, "09:00", "20:00"))

    def test_simple_cleaning_periods_translate_to_the_existing_room_settings(self) -> None:
        self.assertEqual(models.room_cleaning_period_update("Off"), {"enabled": False})
        self.assertEqual(
            models.room_cleaning_period_update("Night"),
            {"enabled": True, "desired_window_start": "00:00", "desired_window_end": "06:00"},
        )
        self.assertEqual(
            models.room_cleaning_period_update("Morning"),
            {"enabled": True, "desired_window_start": "06:00", "desired_window_end": "12:00"},
        )
        self.assertEqual(
            models.room_cleaning_period_update("Afternoon"),
            {"enabled": True, "desired_window_start": "12:00", "desired_window_end": "18:00"},
        )
        self.assertEqual(
            models.room_cleaning_period_update("Evening"),
            {"enabled": True, "desired_window_start": "18:00", "desired_window_end": "00:00"},
        )
        self.assertEqual(
            models.room_cleaning_period_update("Custom"),
            {"enabled": True, "desired_window_start": "09:00", "desired_window_end": "20:00"},
        )

    def test_simple_cleaning_period_is_derived_without_changing_inherited_windows(self) -> None:
        self.assertEqual(models.room_cleaning_period({"enabled": False}), "Off")
        self.assertEqual(
            models.room_cleaning_period(
                {"enabled": True, "desired_window_start": "00:00", "desired_window_end": "06:00"}
            ),
            "Night",
        )
        self.assertEqual(
            models.room_cleaning_period(
                {"enabled": True, "desired_window_start": None, "desired_window_end": None}
            ),
            "Custom",
        )
        self.assertEqual(
            models.room_cleaning_period(
                {"enabled": True, "desired_window_start": "09:15", "desired_window_end": "20:00"}
            ),
            "Custom",
        )

    def test_room_profile_mode_keeps_or_clears_every_room_override(self) -> None:
        inherited = {key: None for key in models.ROOM_PROFILE_OVERRIDE_KEYS}
        self.assertEqual(models.room_cleaning_profile(inherited), "Robot default")
        self.assertEqual(
            models.room_cleaning_profile_update("Custom"), {"profile_custom": True}
        )
        custom_with_inheritance = {**inherited, "profile_custom": True}
        self.assertEqual(models.room_cleaning_profile(custom_with_inheritance), "Custom")
        legacy_override = {**inherited, "fan_speed": "max"}
        self.assertEqual(models.room_cleaning_profile(legacy_override), "Custom")
        self.assertEqual(
            models.room_cleaning_profile_update("Robot default"),
            {
                "profile_custom": False,
                "pass_count": None,
                **{key: None for key in models.ROOM_PROFILE_OVERRIDE_KEYS},
            },
        )

    def test_unresolved_occupancy_is_only_allowed_in_the_desired_window_for_non_transit_rooms(self) -> None:
        desired_window = self.now.replace(hour=12)
        self.assertTrue(
            models.unresolved_occupancy_allowed("unresolved", False, desired_window, "09:00", "20:00")
        )
        self.assertFalse(
            models.unresolved_occupancy_allowed("unresolved", True, desired_window, "09:00", "20:00")
        )
        self.assertFalse(
            models.unresolved_occupancy_allowed("occupied", False, desired_window, "09:00", "20:00")
        )

    def test_room_window_bounds_inherit_independently(self) -> None:
        inherited = models.resolve_daily_window(None, None, "09:00", "20:00")
        partial = models.resolve_daily_window("10:15", None, "09:00", "20:00")

        self.assertEqual((inherited.start, inherited.end), ("09:00", "20:00"))
        self.assertTrue(inherited.start_inherited)
        self.assertTrue(inherited.end_inherited)
        self.assertEqual((partial.start, partial.end), ("10:15", "20:00"))
        self.assertFalse(partial.start_inherited)
        self.assertTrue(partial.end_inherited)

    def test_daily_window_validation_rejects_malformed_times_and_equal_bounds(self) -> None:
        for value in ("9:00", "24:00", "09:60", "09:00:00", None):
            self.assertFalse(models.is_valid_daily_time(value))
        with self.assertRaises(ValueError):
            models.resolve_daily_window("9:00", None, "09:00", "20:00")
        self.assertFalse(
            models.resolve_daily_window("09:00", "09:00", "08:00", "20:00").valid
        )

    def test_daily_window_boundaries_are_half_open_for_day_and_overnight_ranges(self) -> None:
        self.assertTrue(models.in_daytime_window(self.now.replace(hour=9), "09:00", "20:00"))
        self.assertFalse(models.in_daytime_window(self.now.replace(hour=20), "09:00", "20:00"))
        self.assertTrue(models.in_daytime_window(self.now.replace(hour=22), "22:00", "05:00"))
        self.assertTrue(models.in_daytime_window(self.now.replace(hour=4, minute=59), "22:00", "05:00"))
        self.assertFalse(models.in_daytime_window(self.now.replace(hour=5), "22:00", "05:00"))

    def test_next_usable_window_start_is_now_inside_and_next_boundary_outside(self) -> None:
        inside = self.now.replace(hour=10, minute=30)
        outside = self.now.replace(hour=21, minute=30)
        self.assertEqual(
            models.next_usable_window_start(inside, "09:00", "20:00"),
            inside,
        )
        self.assertEqual(
            models.next_usable_window_start(outside, "09:00", "20:00"),
            self.now.replace(hour=9, minute=0) + timedelta(days=1),
        )

    def test_two_rooms_can_have_different_candidate_windows(self) -> None:
        now = self.now.replace(hour=10)
        morning = models.resolve_daily_window("09:00", "11:00", "01:00", "05:00")
        afternoon = models.resolve_daily_window("14:00", "16:00", "01:00", "05:00")

        self.assertTrue(models.desired_window_allows(False, now, morning.start, morning.end))
        self.assertFalse(
            models.desired_window_allows(False, now, afternoon.start, afternoon.end)
        )

    def test_due_mopping_is_selected_when_the_room_program_supports_it(self) -> None:
        mop_due = self.now - timedelta(hours=1)
        vacuum_due = self.now + timedelta(hours=4)
        self.assertEqual(
            models.select_operation(vacuum_due, mop_due, True, self.now),
            ("mop", mop_due),
        )

    def test_learned_duration_keeps_the_user_prior_until_samples_are_sufficient(self) -> None:
        self.assertEqual(models.learned_duration_minutes([12, 14], 30), (30, 2))

    def test_learned_duration_uses_a_conservative_outlier_resistant_percentile(self) -> None:
        duration, samples = models.learned_duration_minutes([20, 22, 24, 180], 30)
        self.assertEqual(duration, 24)
        self.assertEqual(samples, 3)

    def test_zero_native_clean_duration_fails_only_an_attributed_clean(self) -> None:
        for source in ("scheduler", "manual_dashboard", "manual_home_assistant"):
            self.assertTrue(models.managed_clean_duration_failed(source, "robot_timer", 0))
        self.assertTrue(models.managed_clean_duration_failed("scheduler", "robot_timer", -1))
        self.assertFalse(models.managed_clean_duration_failed("scheduler", "robot_timer", 0.5))
        self.assertFalse(models.managed_clean_duration_failed("scheduler", "state_transition", 0))
        self.assertFalse(models.managed_clean_duration_failed("scheduler", "robot_timer", None))
        self.assertFalse(models.managed_clean_duration_failed("native_app", "robot_timer", 0))


class CleaningProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.capabilities = models.AdapterCapabilities(
            adapter_id="roborock",
            schema_version=2,
            portable_area_clean=True,
            supported_pass_counts=frozenset({1, 2}),
            supported_operations=frozenset({"vacuum", "mop"}),
            fan_speed_options=("quiet", "max"),
            mode_options=("vacuum", "mop"),
            mop_mode_options=("standard", "deep"),
            mop_intensity_options=("low", "high"),
            cleaning_depth_options=("fast", "daily", "fine"),
            native_vacuum_pass_counts=frozenset({2}),
        )

    def test_room_values_replace_robot_defaults_and_are_exact(self) -> None:
        resolved = models.resolve_cleaning_profile(
            "mop",
            {"fan_speed": "max", "mop_mode": "deep"},
            {
                "fan_speed": "quiet",
                "mode": "vacuum",
                "mop_mode": "standard",
                "mop_intensity": "high",
            },
            self.capabilities,
        )

        self.assertEqual(
            resolved.to_mapping(),
            {
                "operation": "mop",
                "fan_speed": "max",
                "mode": "mop",
                "mop_mode": "deep",
                "mop_intensity": "high",
                "cleaning_depth": None,
            },
        )

    def test_stale_or_operation_conflicting_room_values_fail_closed(self) -> None:
        self.assertIsNone(
            models.resolve_cleaning_profile(
                "vacuum",
                {"fan_speed": "removed"},
                {},
                self.capabilities,
            )
        )

    def test_mop_only_is_preferred_and_combined_mop_mode_is_rejected(self) -> None:
        capabilities = models.AdapterCapabilities(
            adapter_id="roborock",
            schema_version=2,
            portable_area_clean=True,
            supported_pass_counts=frozenset({1}),
            supported_operations=frozenset({"vacuum", "mop"}),
            mode_options=("vacuum", "mop", "mop_only", "vac_and_mop"),
            mop_mode_options=("mop", "mop_only", "vac_and_mop"),
        )
        resolved = models.resolve_cleaning_profile("mop", {}, {}, capabilities)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.mode, "mop_only")
        self.assertEqual(resolved.mop_mode, "mop_only")
        self.assertIsNone(
            models.resolve_cleaning_profile(
                "mop", {"mop_mode": "vac_and_mop"}, {}, capabilities
            )
        )

    def test_native_mop_profile_resolves_explicit_native_controls(self) -> None:
        capabilities = models.AdapterCapabilities(
            adapter_id="roborock",
            schema_version=7,
            portable_area_clean=True,
            supported_pass_counts=frozenset({1}),
            supported_operations=frozenset({"vacuum", "mop"}),
            fan_speed_options=("quiet", "off", "custom"),
            mode_options=("vacuum", "mop", "vac_and_mop", "custom"),
            mop_mode_options=("standard", "deep", "deep_plus", "fast", "smart_mode"),
            mop_intensity_options=("off", "low", "medium", "high", "smart_mode"),
            native_mop_profile=True,
        )

        resolved = models.resolve_cleaning_profile(
            "mop", {}, {}, capabilities
        )

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.mode, "mop")
        self.assertEqual(resolved.fan_speed, "off")
        self.assertEqual(resolved.mop_mode, "standard")
        self.assertEqual(resolved.mop_intensity, "medium")

    def test_native_mop_profile_preserves_a_nonconcrete_room_override_for_safe_blocking(self) -> None:
        capabilities = models.AdapterCapabilities(
            adapter_id="roborock",
            schema_version=7,
            portable_area_clean=True,
            supported_pass_counts=frozenset({1}),
            supported_operations=frozenset({"vacuum", "mop"}),
            fan_speed_options=("quiet", "off"),
            mode_options=("vacuum", "mop"),
            mop_mode_options=("standard", "deep", "smart_mode"),
            mop_intensity_options=("off", "medium", "smart_mode"),
            native_mop_profile=True,
        )

        resolved = models.resolve_cleaning_profile(
            "mop",
            {"mop_mode": "smart_mode", "mop_intensity": "smart_mode"},
            {"mop_mode": "standard", "mop_intensity": "medium"},
            capabilities,
        )

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.mode, "mop")
        self.assertEqual(resolved.fan_speed, "off")
        self.assertEqual(resolved.mop_mode, "smart_mode")
        self.assertEqual(resolved.mop_intensity, "smart_mode")

    def test_native_mop_profile_migration_normalizes_only_robot_defaults_once(self) -> None:
        migrated = models.native_mop_profile_default_migration(
            {"mop_mode": "smart_mode", "mop_intensity": "off"}
        )

        self.assertEqual(
            migrated,
            {
                "direct_custom_mop_migrated": True,
                "mop_mode": "standard",
                "mop_intensity": "medium",
            },
        )
        self.assertEqual(
            models.native_mop_profile_default_migration(
                {"mop_mode": "deep", "mop_intensity": "high"}
            ),
            {"direct_custom_mop_migrated": True},
        )
        self.assertIsNone(
            models.native_mop_profile_default_migration(
                {
                    "direct_custom_mop_migrated": True,
                    "mop_mode": "deep",
                    "mop_intensity": "high",
                }
            )
        )

    def test_native_mop_profile_controls_accept_only_concrete_route_and_water_values(self) -> None:
        for value in ("standard", "deep", "deep_plus", "fast"):
            self.assertTrue(models.is_native_mop_profile_value("mop_mode", value))
        for value in ("low", "medium", "high"):
            self.assertTrue(
                models.is_native_mop_profile_value("mop_intensity", value)
            )
        for key, value in (
            ("mop_mode", "custom"),
            ("mop_mode", "smart_mode"),
            ("mop_intensity", "off"),
            ("mop_intensity", "custom"),
            ("mop_intensity", None),
        ):
            self.assertFalse(models.is_native_mop_profile_value(key, value))

    def test_vacuum_profile_ignores_retained_mop_only_values(self) -> None:
        resolved = models.resolve_cleaning_profile(
            "vacuum",
            {},
            {
                "fan_speed": "max",
                "mode": None,
                "mop_mode": "removed_mop_mode",
                "mop_intensity": "removed_mop_intensity",
            },
            self.capabilities,
        )

        self.assertEqual(
            resolved.to_mapping(),
            {
                "operation": "vacuum",
                "fan_speed": "max",
                "mode": "vacuum",
                "mop_mode": None,
                "mop_intensity": None,
                "cleaning_depth": None,
            },
        )

    def test_supported_fields_require_a_robot_default_or_room_override(self) -> None:
        self.assertIsNone(
            models.resolve_cleaning_profile(
                "vacuum",
                {},
                {"mode": "vacuum"},
                self.capabilities,
            )
        )
        resolved = models.resolve_cleaning_profile(
            "vacuum",
            {"fan_speed": "quiet"},
            {"mode": "vacuum"},
            self.capabilities,
        )
        self.assertIsNotNone(resolved)
        self.assertIsNone(
            models.resolve_cleaning_profile(
                "mop",
                {"mode": "vacuum"},
                {},
                self.capabilities,
            )
        )

    def test_persisted_profile_support_ignores_later_default_changes(self) -> None:
        profile = {
            "operation": "vacuum",
            "fan_speed": "max",
            "mode": "vacuum",
            "mop_mode": None,
            "mop_intensity": None,
        }
        self.assertTrue(
            models.cleaning_profile_is_supported(profile, self.capabilities)
        )

    def test_q10_cleaning_depth_resolves_for_eligible_vacuum_profiles(self) -> None:
        resolved = models.resolve_cleaning_profile(
            "vacuum",
            {"fan_speed": "max", "cleaning_depth": "fine"},
            {"mode": "vacuum"},
            self.capabilities,
        )

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.cleaning_depth, "fine")


class RecoveryTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.recovered_at = datetime(2026, 8, 8, 12, 0)

    def test_live_returning_transition_after_recovery_is_authoritative(self) -> None:
        self.assertTrue(
            models.recovery_transition_is_observed(
                "cleaning", "returning", self.recovered_at + timedelta(minutes=2), self.recovered_at
            )
        )

    def test_state_snapshot_without_a_cleaning_or_returning_origin_is_not_authoritative(self) -> None:
        self.assertFalse(
            models.recovery_transition_is_observed(
                "unavailable", "docked", self.recovered_at + timedelta(minutes=2), self.recovered_at
            )
        )

    def test_unconfirmed_cleaning_is_treated_as_native_app_activity(self) -> None:
        fault = {"robot_registry_id": "registry-robot"}
        active = {"source": "scheduler", "seen_cleaning": False}
        self.assertTrue(
            models.should_assume_native_app_clean(
                "cleaning", fault, "registry-robot", active
            )
        )
        self.assertFalse(
            models.should_assume_native_app_clean(
                "docked", fault, "registry-robot", active
            )
        )
        self.assertFalse(
            models.should_assume_native_app_clean(
                "cleaning", fault, "registry-robot", {**active, "seen_cleaning": True}
            )
        )

    def test_idle_transition_does_not_complete_a_recovered_job(self) -> None:
        self.assertFalse(
            models.recovery_transition_is_observed(
                "returning", "idle", self.recovered_at + timedelta(minutes=2), self.recovered_at
            )
        )

    def test_transition_from_before_recovery_remains_an_offline_completion(self) -> None:
        self.assertFalse(
            models.recovery_transition_is_observed(
                "cleaning", "docked", self.recovered_at - timedelta(seconds=1), self.recovered_at
            )
        )


class PendingProfileRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.occurrence = {"source": "scheduler"}
        self.stage = {"status": "pending", "started_at": None}

    def test_allows_only_an_unstarted_scheduler_stage_on_a_docked_robot(self) -> None:
        self.assertTrue(
            models.can_refresh_pending_occurrence_profile(
                self.occurrence, self.stage, "docked", False
            )
        )

    def test_rejects_active_manual_and_started_work(self) -> None:
        self.assertFalse(
            models.can_refresh_pending_occurrence_profile(
                self.occurrence, self.stage, "docked", True
            )
        )
        self.assertFalse(
            models.can_refresh_pending_occurrence_profile(
                {"source": "manual_dashboard"}, self.stage, "docked", False
            )
        )
        self.assertFalse(
            models.can_refresh_pending_occurrence_profile(
                self.occurrence,
                {"status": "running", "started_at": "2026-08-14T00:00:00+00:00"},
                "docked",
                False,
            )
        )

    def test_rejects_a_robot_that_is_not_observed_docked(self) -> None:
        self.assertFalse(
            models.can_refresh_pending_occurrence_profile(
                self.occurrence, self.stage, "idle", False
            )
        )


class ManualCleanRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.robots = ["vacuum.sheila"]
        self.rooms = ["lego_room", "bedroom_1"]

    def test_user_room_clean_tracks_multiple_discovered_areas(self) -> None:
        request = models.parse_manual_clean_request(
            "vacuum",
            "clean_area",
            "user-id",
            {"entity_id": "vacuum.sheila", "cleaning_area_id": ["lego_room", "bedroom_1"]},
            self.robots,
            self.rooms,
        )
        self.assertEqual(request, models.ManualCleanRequest("vacuum.sheila", ["lego_room", "bedroom_1"]))

    def test_no_user_context_is_not_a_manual_home_assistant_clean(self) -> None:
        self.assertIsNone(
            models.parse_manual_clean_request(
                "vacuum",
                "clean_area",
                None,
                {"entity_id": "vacuum.sheila", "cleaning_area_id": ["lego_room"]},
                self.robots,
                self.rooms,
            )
        )

    def test_whole_home_or_unknown_area_calls_are_not_tracked(self) -> None:
        self.assertIsNone(
            models.parse_manual_clean_request(
                "vacuum", "start", "user-id", {"entity_id": "vacuum.sheila"}, self.robots, self.rooms
            )
        )
        self.assertIsNone(
            models.parse_manual_clean_request(
                "vacuum",
                "clean_area",
                "user-id",
                {"entity_id": "vacuum.sheila", "cleaning_area_id": ["native_segment_1"]},
                self.robots,
                self.rooms,
            )
        )

class ActiveJobHoldTests(unittest.TestCase):
    def test_physical_resume_continues_a_held_job(self) -> None:
        self.assertEqual(models.held_job_transition("cleaning", "held", False), "resumed")

    def test_direct_error_recovery_to_idle_remains_held(self) -> None:
        self.assertEqual(models.held_job_transition("idle", "held", False), "held")

    def test_returning_then_docked_is_a_physical_cancellation(self) -> None:
        self.assertEqual(models.held_job_transition("returning", "held", False), "cancelling")
        self.assertEqual(models.held_job_transition("docked", "cancelling", False), "cancelled")

    def test_completion_before_a_fault_waits_for_a_physical_return(self) -> None:
        self.assertEqual(models.held_job_transition("docked", "held", True), "held")
        self.assertEqual(
            models.held_job_transition("returning", "held", True), "completion_pending"
        )
        self.assertEqual(
            models.held_job_transition("docked", "completion_pending", True), "complete"
        )

    def test_pending_completion_accepts_a_direct_dock_observation(self) -> None:
        self.assertTrue(
            models.pending_completion_is_docked("docked", "completion_held")
        )
        self.assertFalse(models.pending_completion_is_docked("idle", "recovery_waiting"))
        self.assertFalse(models.pending_completion_is_docked("docked", "held"))
        self.assertFalse(
            models.pending_completion_is_docked("returning", "completion_held")
        )

    def test_only_a_docked_robot_can_start_scheduled_work(self) -> None:
        self.assertTrue(models.can_start_scheduled_clean("docked"))
        self.assertFalse(models.can_start_scheduled_clean("idle"))
        self.assertFalse(models.can_start_scheduled_clean("returning"))
        self.assertFalse(models.can_start_scheduled_clean(None))

    def test_roborock_status_must_be_terminal_before_dispatch(self) -> None:
        for status in ("idle", "docked", "charging", "charging_complete"):
            self.assertTrue(
                models.detailed_status_is_dispatchable(status, required=True)
            )
        for status in ("emptying_the_bin", "washing_the_mop", "docking", None):
            self.assertFalse(
                models.detailed_status_is_dispatchable(status, required=True)
            )
        self.assertTrue(models.detailed_status_is_dispatchable(None, required=False))

    def test_mop_washing_confirms_only_an_adapter_opted_in_mop_stage(self) -> None:
        mop_start_states = frozenset({"washing_the_mop"})
        self.assertTrue(
            models.mop_stage_start_is_observed(
                "mop", "Washing the Mop", mop_start_states
            )
        )
        self.assertFalse(
            models.mop_stage_start_is_observed(
                "vacuum", "washing_the_mop", mop_start_states
            )
        )
        self.assertFalse(
            models.mop_stage_start_is_observed("mop", "charging", mop_start_states)
        )
        self.assertFalse(
            models.mop_stage_start_is_observed(
                "mop", "washing_the_mop", frozenset()
            )
        )

    def test_ready_confirmation_requires_the_full_ten_seconds(self) -> None:
        now = datetime(2026, 8, 21, 10, 0)
        delay = timedelta(seconds=10)
        self.assertFalse(models.ready_confirmation_elapsed(now, now, delay))
        self.assertFalse(
            models.ready_confirmation_elapsed(now, now + timedelta(seconds=9), delay)
        )
        self.assertTrue(
            models.ready_confirmation_elapsed(now, now + timedelta(seconds=10), delay)
        )

    def test_idle_does_not_close_a_held_cancellation(self) -> None:
        self.assertEqual(models.held_job_transition("idle", "cancelling", False), "held")

    def test_cancellation_rebases_due_queue_without_collapsing_spacing(self) -> None:
        now = datetime(2026, 8, 8, 12, 0)
        result = models.rebase_due_times(
            {
                "area_a:vacuum": now - timedelta(hours=2),
                "area_b:vacuum": now + timedelta(hours=1),
                "area_c:mop": now + timedelta(hours=4),
            },
            now + timedelta(hours=24),
        )
        self.assertEqual(result["area_a:vacuum"], now + timedelta(hours=24))
        self.assertEqual(result["area_b:vacuum"], now + timedelta(hours=27))
        self.assertEqual(result["area_c:mop"], now + timedelta(hours=30))

    def test_offline_held_job_uses_expected_duration_to_classify_docked_state(self) -> None:
        recovered = datetime(2026, 8, 8, 12, 0)
        self.assertEqual(
            models.offline_held_recovery_outcome(
                "docked", "held", recovered - timedelta(minutes=31), 30, recovered
            ),
            "complete",
        )
        self.assertEqual(
            models.offline_held_recovery_outcome(
                "idle", "held", recovered - timedelta(minutes=29), 30, recovered
            ),
            "held",
        )
        self.assertEqual(
            models.offline_held_recovery_outcome("idle", "held", None, None, recovered),
            "held",
        )

    def test_profile_control_metadata_precedes_option_heuristics(self) -> None:
        self.assertEqual(
            models.profile_control_kind(
                "mop_intensity", ("off", "light", "medium", "high", "custom")
            ),
            "mop_intensity",
        )
        self.assertEqual(
            models.profile_control_kind("mop_mode", ("standard", "deep")),
            "mop_mode",
        )
        self.assertEqual(
            models.profile_control_kind("cleaning_mode", ("silent", "turbo")),
            "mode",
        )

    def test_profile_control_fallback_rejects_unrelated_selects(self) -> None:
        self.assertIsNone(
            models.profile_control_kind(None, ("enabled", "disabled", "automatic"))
        )

if __name__ == "__main__":
    unittest.main()
