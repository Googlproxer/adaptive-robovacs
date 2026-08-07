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


if __name__ == "__main__":
    unittest.main()
