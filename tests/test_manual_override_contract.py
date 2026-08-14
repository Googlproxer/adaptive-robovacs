"""Contracts for explicit dashboard manual-clean overrides."""

from pathlib import Path
import unittest


COORDINATOR_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "adaptive_robovacs"
    / "coordinator.py"
)
MODELS_PATH = COORDINATOR_PATH.with_name("models.py")


class ManualOverrideContractTests(unittest.TestCase):
    def test_manual_clean_bypasses_scheduler_and_vacancy_gates(self) -> None:
        source = COORDINATOR_PATH.read_text(encoding="utf-8")
        manual = source[source.index("    async def async_manual_clean_room"):]
        self.assertIn("self._manual_candidate(", manual)
        self.assertIn("self._manual_robot_ready(robot)", manual)
        self.assertIn('"manual_override": True', source)
        self.assertIn('"bypass_forecast": True', source)
        self.assertNotIn('return await reject("scheduler dispatch halted")', manual)
        self.assertNotIn('return await reject("room already has a cleaning occurrence")', manual)
        self.assertNotIn('return await reject(\n                    f"occupancy', manual)

    def test_manual_clean_keeps_party_and_observe_only_non_bypassable(self) -> None:
        manual = COORDINATOR_PATH.read_text(encoding="utf-8")
        manual = manual[manual.index("    async def async_manual_clean_room"):]
        self.assertIn('return await reject("observe-only mode")', manual)
        self.assertIn('return await reject("party mode")', manual)

    def test_manual_readiness_is_a_pure_docked_state_decision(self) -> None:
        source = MODELS_PATH.read_text(encoding="utf-8")
        self.assertIn("def manual_clean_robot_is_docked", source)
        self.assertIn('return state == "docked"', source)


if __name__ == "__main__":
    unittest.main()
