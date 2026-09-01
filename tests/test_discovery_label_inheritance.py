"""Regression coverage for radar labels inherited from Home Assistant devices."""

import ast
from pathlib import Path
import types
import unittest
from unittest.mock import Mock


DISCOVERY_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "adaptive_robovacs"
    / "discovery_core.py"
)


def _load_label_helpers() -> dict[str, object]:
    """Load the small pure label helpers without requiring Home Assistant."""

    tree = ast.parse(DISCOVERY_PATH.read_text(encoding="utf-8"))
    wanted = {
        "_normalised_label",
        "_labels_for",
        "_occupancy_labels",
        "_occupancy_source_is_excluded",
    }
    helpers = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    module = ast.Module(body=helpers, type_ignores=[])
    namespace = {
        "dr": types.SimpleNamespace(DeviceRegistry=object),
        "er": types.SimpleNamespace(RegistryEntry=object),
        "lr": types.SimpleNamespace(LabelRegistry=object),
        "LABEL_EXCLUDE_OCCUPANCY": "robovac_exclude_occupancy",
    }
    exec(
        compile(ast.fix_missing_locations(module), str(DISCOVERY_PATH), "exec"),
        namespace,
    )
    return namespace


class DiscoveryLabelInheritanceTests(unittest.TestCase):
    """Device labels are a default, while entity labels are an override."""

    def setUp(self) -> None:
        self.helpers = _load_label_helpers()
        self.label_registry = types.SimpleNamespace(
            async_get_label=lambda label_id: types.SimpleNamespace(name=label_id)
        )

    def test_unlabelled_occupancy_entity_inherits_its_device_radar_label(self) -> None:
        entry = types.SimpleNamespace(labels=frozenset(), device_id="radar-device")
        devices = types.SimpleNamespace(
            async_get=lambda device_id: types.SimpleNamespace(
                labels=frozenset({"robovac_radar"})
            )
        )

        result = self.helpers["_occupancy_labels"](
            entry, devices, self.label_registry
        )

        self.assertIn("robovac_radar", result)

    def test_direct_entity_labels_override_device_labels(self) -> None:
        entry = types.SimpleNamespace(
            labels=frozenset({"not_a_radar"}), device_id="radar-device"
        )
        devices = types.SimpleNamespace(async_get=Mock())

        result = self.helpers["_occupancy_labels"](
            entry, devices, self.label_registry
        )

        self.assertIn("not_a_radar", result)
        self.assertNotIn("robovac_radar", result)
        devices.async_get.assert_not_called()

    def test_device_occupancy_exclusion_overrides_direct_entity_labels(self) -> None:
        entry = types.SimpleNamespace(
            labels=frozenset({"robovac_radar"}), device_id="doorbell-device"
        )
        devices = types.SimpleNamespace(
            async_get=lambda device_id: types.SimpleNamespace(
                labels=frozenset({"robovac_exclude_occupancy"})
            )
        )

        result = self.helpers["_occupancy_source_is_excluded"](
            entry, devices, self.label_registry
        )

        self.assertTrue(result)

    def test_entity_occupancy_exclusion_is_honoured(self) -> None:
        entry = types.SimpleNamespace(
            labels=frozenset({"robovac_exclude_occupancy"}), device_id=None
        )
        devices = types.SimpleNamespace(async_get=Mock())

        result = self.helpers["_occupancy_source_is_excluded"](
            entry, devices, self.label_registry
        )

        self.assertTrue(result)
        devices.async_get.assert_not_called()

    def test_occupancy_exclusion_matches_the_label_display_name(self) -> None:
        entry = types.SimpleNamespace(labels=frozenset({"label-id"}), device_id=None)
        devices = types.SimpleNamespace(async_get=Mock())
        label_registry = types.SimpleNamespace(
            async_get_label=lambda label_id: types.SimpleNamespace(
                name="robovac-exclude-occupancy"
            )
        )

        result = self.helpers["_occupancy_source_is_excluded"](
            entry, devices, label_registry
        )

        self.assertTrue(result)

    def test_discovery_uses_the_inheritance_helper_for_occupancy_sources(self) -> None:
        source = DISCOVERY_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "occupancy_labels = _occupancy_labels(entry, devices, labels)", source
        )
        self.assertIn(
            "if _occupancy_source_is_excluded(entry, devices, labels):", source
        )


if __name__ == "__main__":
    unittest.main()
