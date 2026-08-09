"""Static contracts for the coordinator facade used by platform entities."""

from pathlib import Path
import unittest


PACKAGE_PATH = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs"
COORDINATOR_PATH = PACKAGE_PATH / "coordinator.py"
PLATFORM_PATHS = [
    PACKAGE_PATH / "number.py",
    PACKAGE_PATH / "select.py",
    PACKAGE_PATH / "sensor.py",
    PACKAGE_PATH / "switch.py",
]


class CoordinatorFacadeContractTests(unittest.TestCase):
    def test_platforms_use_coordinator_accessors_not_mutable_store_data(self) -> None:
        for path in PLATFORM_PATHS:
            self.assertNotIn("coordinator.data", path.read_text(encoding="utf-8"), path.name)

    def test_global_controls_and_scheduler_metadata_have_stable_accessors(self) -> None:
        source = COORDINATOR_PATH.read_text(encoding="utf-8")
        self.assertIn("def get_global_setting", source)
        self.assertIn("def scheduler_summary", source)
        self.assertIn('"last_evaluation"', source)
        self.assertIn('"preview"', source)
        self.assertIn('"migration"', source)


if __name__ == "__main__":
    unittest.main()
