"""Behavioral coverage for occupancy labels inherited from HA devices."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from custom_components.adaptive_robovacs.const import LABEL_EXCLUDE_OCCUPANCY
from custom_components.adaptive_robovacs.discovery import (
    _occupancy_labels,
    _occupancy_source_is_excluded,
)


class _Registry:
    def __init__(self, values):
        self._values = values

    def async_get(self, key):
        return self._values.get(key)

    def async_get_label(self, key):
        return self._values.get(key)


class DiscoveryLabelInheritanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.labels = _Registry(
            {
                "radar-id": SimpleNamespace(name="Robovac Radar"),
                "exclude-id": SimpleNamespace(name="Robovac Exclude Occupancy"),
            }
        )

    def test_unlabelled_entity_inherits_device_label(self) -> None:
        devices = _Registry({"device-1": SimpleNamespace(labels={"radar-id"})})
        entry = SimpleNamespace(labels=set(), device_id="device-1")

        self.assertIn("robovac-radar", _occupancy_labels(entry, devices, self.labels))

    def test_direct_entity_labels_replace_device_defaults(self) -> None:
        devices = _Registry({"device-1": SimpleNamespace(labels={"radar-id"})})
        entry = SimpleNamespace(labels={"entity-label"}, device_id="device-1")

        self.assertEqual(
            _occupancy_labels(entry, devices, self.labels), {"entity-label"}
        )

    def test_device_exclusion_always_excludes_the_source(self) -> None:
        devices = _Registry({"device-1": SimpleNamespace(labels={"exclude-id"})})
        entry = SimpleNamespace(labels={"radar-id"}, device_id="device-1")

        self.assertTrue(_occupancy_source_is_excluded(entry, devices, self.labels))
        self.assertEqual(
            LABEL_EXCLUDE_OCCUPANCY,
            "robovac_exclude_occupancy",
        )

    def test_entity_exclusion_does_not_exclude_its_device(self) -> None:
        devices = _Registry({"device-1": SimpleNamespace(labels=set())})
        entry = SimpleNamespace(labels={"exclude-id"}, device_id="device-1")

        self.assertFalse(_occupancy_source_is_excluded(entry, devices, self.labels))


if __name__ == "__main__":
    unittest.main()
