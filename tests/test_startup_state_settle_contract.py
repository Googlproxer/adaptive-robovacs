"""Contracts for the post-startup occupancy-state settling guard."""

from pathlib import Path
import unittest


PACKAGE = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs"
COORDINATOR = (PACKAGE / "coordinator.py").read_text(encoding="utf-8")
CONSTANTS = (PACKAGE / "const.py").read_text(encoding="utf-8")


class StartupStateSettleContractTests(unittest.TestCase):
    def test_every_coordinator_start_arms_a_one_minute_settle_period(self) -> None:
        self.assertIn("STARTUP_STATE_SETTLE_DELAY: Final = timedelta(minutes=1)", CONSTANTS)
        self.assertIn(
            "self._startup_state_settle_until = _now() + STARTUP_STATE_SETTLE_DELAY",
            COORDINATOR,
        )
        self.assertIn('reason="startup-state-settled"', COORDINATOR)

    def test_scheduler_and_dashboard_manual_work_share_the_guard(self) -> None:
        self.assertIn("def _startup_state_settle_reason", COORDINATOR)
        self.assertIn("if (settle_reason := self._startup_state_settle_reason(now))", COORDINATOR)
        manual = COORDINATOR[COORDINATOR.index("    async def async_manual_clean_room"):]
        self.assertIn("self._startup_state_settle_reason(_now())", manual)
        self.assertIn('return "awaiting Home Assistant state restoration"', COORDINATOR)


if __name__ == "__main__":
    unittest.main()
