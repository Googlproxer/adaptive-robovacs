"""Contracts for the per-robot stop-and-return control."""

from pathlib import Path
import unittest


PACKAGE = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs"


class StopAndReturnContractTests(unittest.TestCase):
    """Keep cancellation distinct from an observed completed clean."""

    def test_each_robot_gets_a_stop_and_return_button(self) -> None:
        buttons = (PACKAGE / "button.py").read_text(encoding="utf-8")
        self.assertIn("class _StopAndReturnButton", buttons)
        self.assertIn('"robot_stop_return_control"', buttons)
        self.assertIn("async_stop_and_return_to_dock", buttons)

    def test_return_command_marks_tracked_work_as_cancelling_until_docked(self) -> None:
        coordinator = (PACKAGE / "coordinator.py").read_text(encoding="utf-8")
        self.assertIn("async def async_stop_and_return_to_dock", coordinator)
        self.assertIn('"return_to_base"', coordinator)
        self.assertIn('"user_requested_return"', coordinator)
        self.assertIn('"phase"] = "cancelling"', coordinator)
        self.assertIn("_cancel_start_confirmation", coordinator)
        self.assertIn("and state_text != \"docked\"", coordinator)

    def test_carpet_setting_is_not_exposed_or_persisted(self) -> None:
        switch = (PACKAGE / "switch.py").read_text(encoding="utf-8")
        state = (PACKAGE / "state.py").read_text(encoding="utf-8")
        self.assertNotIn('"carpet"', switch)
        self.assertNotIn("carpet:", state)
        self.assertIn("SCHEMA_VERSION = 15", state)


if __name__ == "__main__":
    unittest.main()
