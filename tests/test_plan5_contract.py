"""Contracts for room profiles and integration-owned manual room actions."""

from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "custom_components" / "adaptive_robovacs"


class PlanFiveContractTests(unittest.TestCase):
    def test_three_room_buttons_share_the_manual_override_entry_point(self) -> None:
        buttons = (PACKAGE / "button.py").read_text(encoding="utf-8")
        coordinator = (PACKAGE / "coordinator.py").read_text(encoding="utf-8")
        for mode in ("configured", "vacuum_only", "mop_only"):
            self.assertIn(f'"{mode}"', buttons)
        self.assertIn("SERVICE_MANUAL_CLEAN_ROOM", buttons)
        self.assertIn("hass.services.async_call", buttons)
        self.assertIn("async def async_manual_clean_room", coordinator)
        for safeguard in (
            "coordinator shutting down",
            "observe-only mode",
            "party mode",
            "_manual_robot_ready(robot)",
        ):
            self.assertIn(safeguard, coordinator)
        self.assertIn('"bypass_desired_window": True', coordinator)
        self.assertIn('"bypass_forecast": True', coordinator)
        self.assertIn('"manual_override": True', coordinator)
        self.assertIn('"source": "manual_dashboard"', coordinator)

    def test_service_targets_one_area_and_disambiguates_config_entries(self) -> None:
        services = (PACKAGE / "services.py").read_text(encoding="utf-8")
        yaml = (PACKAGE / "services.yaml").read_text(encoding="utf-8")
        self.assertIn("SERVICE_MANUAL_CLEAN_ROOM", services)
        self.assertIn('vol.Required("area_id")', services)
        self.assertIn('vol.Optional("entry_id")', services)
        self.assertIn("when multiple Adaptive RoboVacs entries", services)
        self.assertIn("manual_clean_room:", yaml)
        self.assertIn("integration: adaptive_robovacs", yaml)

    def test_profile_is_resolved_before_adapter_preflight_and_apply(self) -> None:
        coordinator = (PACKAGE / "coordinator.py").read_text(encoding="utf-8")
        runtime = (PACKAGE / "runtime.py").read_text(encoding="utf-8")
        adapter = (PACKAGE / "adapters" / "base.py").read_text(encoding="utf-8")
        self.assertIn("resolve_cleaning_profile", coordinator)
        self.assertIn('"cleaning_profile": resolved_profile.to_mapping()', coordinator)
        self.assertIn('candidate.get("resolved_profile")', runtime)
        self.assertIn("adapter.async_validate_profile", runtime)
        self.assertIn("adapter.async_apply_profile", runtime)
        self.assertIn("async def async_validate_profile", adapter)
        self.assertIn("async def async_apply_profile", adapter)

    def test_room_card_orders_profile_and_manual_controls_without_audit_status(self) -> None:
        served = (PACKAGE / "frontend" / "adaptive-robovacs-dashboard.js").read_bytes()
        standalone = (ROOT / "dashboard" / "adaptive-robovacs-dashboard.js").read_bytes()
        self.assertEqual(served, standalone)
        text = served.decode("utf-8")
        for role in (
            "room_fan_speed_control",
            "room_mode_control",
            "room_mop_mode_control",
            "room_mop_intensity_control",
            "room_cleaning_depth_control",
            "room_manual_clean_control",
            "room_manual_vacuum_control",
            "room_manual_mop_control",
        ):
            self.assertIn(role, text)
        room_role_map = text.split("const ROOM_ROLES = new Map([", 1)[1].split(
            "]);", 1
        )[0]
        self.assertNotIn("room_manual_status", room_role_map)
        self.assertIn(
            'ROOM_HIDDEN_ROLES = new Set(["room_manual_status"])', text
        )


if __name__ == "__main__":
    unittest.main()
