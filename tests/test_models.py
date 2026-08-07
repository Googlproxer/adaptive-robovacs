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

    def test_unresolved_occupancy_is_only_allowed_overnight_for_non_transit_rooms(self) -> None:
        overnight = self.now.replace(hour=2)
        self.assertTrue(
            models.unresolved_occupancy_allowed("unresolved", False, overnight, "01:00", "05:00")
        )
        self.assertFalse(
            models.unresolved_occupancy_allowed("unresolved", True, overnight, "01:00", "05:00")
        )
        self.assertFalse(
            models.unresolved_occupancy_allowed("occupied", False, overnight, "01:00", "05:00")
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

if __name__ == "__main__":
    unittest.main()
