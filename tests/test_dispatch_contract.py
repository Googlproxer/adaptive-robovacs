"""Contract checks for Home Assistant-native vacuum dispatch."""

from pathlib import Path
import unittest


COORDINATOR_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "adaptive_robovacs"
    / "coordinator.py"
)


class VacuumDispatchContractTests(unittest.TestCase):
    """Guard the native clean-area service payload without importing HA."""

    def test_native_area_id_payload_is_used(self) -> None:
        source = COORDINATOR_PATH.read_text(encoding="utf-8")
        self.assertIn('"area_id": [room.area_id]', source)
        self.assertNotIn("cleaning_area_id", source)


if __name__ == "__main__":
    unittest.main()
