"""Release metadata contract checks."""

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]


class ReleaseContractTests(unittest.TestCase):
    def test_hacs_manifest_and_integration_versions_are_release_ready(self) -> None:
        hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
        manifest = json.loads(
            (
                ROOT / "custom_components" / "adaptive_robovacs" / "manifest.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(hacs["name"], manifest["name"])
        self.assertEqual(manifest["version"], "1.13.0")
        self.assertEqual(hacs["homeassistant"], "2026.9.0")

    def test_dashboard_sources_are_byte_identical(self) -> None:
        integration_dashboard = (
            ROOT
            / "custom_components"
            / "adaptive_robovacs"
            / "frontend"
            / "adaptive-robovacs-dashboard.js"
        )
        standalone_dashboard = ROOT / "dashboard" / "adaptive-robovacs-dashboard.js"
        self.assertEqual(
            integration_dashboard.read_bytes(),
            standalone_dashboard.read_bytes(),
        )

    def test_hacs_listing_icon_matches_the_local_integration_brand(self) -> None:
        root_icon = ROOT / "icon.png"
        integration_icon = (
            ROOT / "custom_components" / "adaptive_robovacs" / "brand" / "icon.png"
        )
        self.assertTrue(root_icon.is_file())
        self.assertEqual(root_icon.read_bytes(), integration_icon.read_bytes())


if __name__ == "__main__":
    unittest.main()
