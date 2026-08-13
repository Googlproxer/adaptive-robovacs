"""Release metadata contract checks."""

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]


class ReleaseContractTests(unittest.TestCase):
    def test_hacs_manifest_and_integration_versions_are_release_ready(self) -> None:
        hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
        manifest = json.loads(
            (ROOT / "custom_components" / "adaptive_robovacs" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(hacs["name"], manifest["name"])
        self.assertRegex(manifest["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(manifest["version"], "1.4.1")

    def test_hacs_listing_icon_matches_the_local_integration_brand(self) -> None:
        root_icon = ROOT / "icon.png"
        integration_icon = ROOT / "custom_components" / "adaptive_robovacs" / "brand" / "icon.png"
        self.assertTrue(root_icon.is_file())
        self.assertEqual(root_icon.read_bytes(), integration_icon.read_bytes())


if __name__ == "__main__":
    unittest.main()
