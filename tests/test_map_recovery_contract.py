"""Static safety contracts for the optional Q10 retained-map guard."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "custom_components" / "adaptive_robovacs"


class MapRecoveryContractTests(unittest.TestCase):
    def test_recovery_has_its_own_store_and_no_cleaning_service_path(self) -> None:
        recovery = (PACKAGE / "map_recovery.py").read_text(encoding="utf-8")
        integration = (PACKAGE / "integration_core.py").read_text(encoding="utf-8")

        self.assertIn('MAP_RECOVERY_STORAGE_KEY', recovery)
        self.assertIn('"op": "list"', recovery)
        # Q10 returns an active raw map frame in response to its direct
        # read-only MULTI_MAP list request; it has no per-slot get operation.
        self.assertIn('_async_send_active_frame_request', recovery)
        self.assertNotIn('"op": "get"', recovery)
        self.assertIn('"op": "apply"', recovery)
        self.assertIn('map_recovery_pending', recovery)
        self.assertNotIn("hass.services.async_call", recovery)
        self.assertNotIn('"vacuum", "start"', recovery)
        self.assertIn("map_store.async_remove()", integration)

    def test_public_services_and_runtime_dependency_are_declared(self) -> None:
        services = (PACKAGE / "services.py").read_text(encoding="utf-8")
        descriptions = (PACKAGE / "services.yaml").read_text(encoding="utf-8")
        manifest = (PACKAGE / "manifest.json").read_text(encoding="utf-8")

        for name in (
            "list_retained_maps",
            "capture_map_snapshot",
            "activate_retained_map",
            "verify_map_recovery",
        ):
            self.assertIn(name, services)
            self.assertIn(f"{name}:", descriptions)
        self.assertIn('"after_dependencies": ["roborock"]', manifest)
        self.assertIn('"camera"', (PACKAGE / "const.py").read_text(encoding="utf-8"))

    def test_capture_and_stop_buttons_keep_their_separate_actions(self) -> None:
        tree = ast.parse((PACKAGE / "button.py").read_text(encoding="utf-8"))
        methods = {
            node.name: [item.name for item in node.body if isinstance(item, ast.AsyncFunctionDef)]
            for node in tree.body
            if isinstance(node, ast.ClassDef)
        }

        self.assertEqual(methods["_StopAndReturnButton"], ["async_press"])
        self.assertEqual(methods["_CaptureMapSnapshotButton"], ["async_press"])


if __name__ == "__main__":
    unittest.main()
