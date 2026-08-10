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

    def test_due_at_honours_later_manual_deferral(self) -> None:
        result = models.due_at(
            self.now - timedelta(hours=100),
            84,
            self.now + timedelta(hours=10),
            self.now,
        )
        self.assertEqual(result, self.now + timedelta(hours=10))

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

    def test_carpet_rooms_never_choose_a_mopping_operation(self) -> None:
        mop_due = self.now - timedelta(hours=1)
        vacuum_due = self.now + timedelta(hours=4)
        self.assertEqual(
            models.select_operation(vacuum_due, mop_due, True, True, self.now),
            ("vacuum", vacuum_due),
        )
        self.assertEqual(
            models.select_operation(vacuum_due, mop_due, True, False, self.now),
            ("mop", mop_due),
        )

    def test_learned_duration_keeps_the_user_prior_until_samples_are_sufficient(self) -> None:
        self.assertEqual(models.learned_duration_minutes([12, 14], 30), (30, 2))

    def test_learned_duration_uses_a_conservative_outlier_resistant_percentile(self) -> None:
        duration, samples = models.learned_duration_minutes([20, 22, 24, 180], 30)
        self.assertEqual(duration, 24)
        self.assertEqual(samples, 3)


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

    def test_transition_from_before_recovery_remains_an_offline_completion(self) -> None:
        self.assertFalse(
            models.recovery_transition_is_observed(
                "cleaning", "docked", self.recovered_at - timedelta(seconds=1), self.recovered_at
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
            "cancelled",
        )
        self.assertEqual(
            models.offline_held_recovery_outcome("idle", "held", None, None, recovered),
            "held",
        )

if __name__ == "__main__":
    unittest.main()
