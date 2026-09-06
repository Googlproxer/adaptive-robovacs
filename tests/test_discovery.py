"""Behavioral tests for registry-only discovery and profile inference."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from custom_components.adaptive_robovacs import discovery
from custom_components.adaptive_robovacs.models import AdapterCapabilities


class Registry:
    """Small registry double exposing Home Assistant's read interface."""

    def __init__(self, values=None) -> None:
        self.values = values or {}
        self.entities = self.values
        self.areas = self.values

    def async_get(self, key):
        return self.values.get(key)

    def async_get_area(self, key):
        return self.values.get(key)

    def async_get_label(self, key):
        return self.values.get(key)


def registry_entry(entity_id: str, **values):
    """Build the stable registry metadata used by discovery."""

    defaults = {
        "entity_id": entity_id,
        "id": f"registry-{entity_id}",
        "device_id": "device-robot",
        "area_id": None,
        "platform": "test_vendor",
        "unique_id": entity_id,
        "labels": set(),
        "name": None,
        "original_name": None,
        "original_device_class": None,
        "translation_key": None,
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def entity_state(state: str = "on", *, name: str | None = None, **attributes):
    return SimpleNamespace(state=state, name=name, attributes=attributes)


class DiscoveryHelperTests(unittest.TestCase):
    def test_profile_inference_uses_typed_metadata_and_live_options(self) -> None:
        entries = [
            registry_entry("sensor.alpha_battery"),
            registry_entry("sensor.alpha_cleaning_time"),
            registry_entry("select.alpha_passes", translation_key="cleaning_passes"),
            registry_entry("select.alpha_water", translation_key="water_level"),
            registry_entry("select.alpha_mode", translation_key="cleaning_mode"),
            registry_entry("select.alpha_route", translation_key="mop_mode"),
        ]
        states = {
            "sensor.alpha_battery": entity_state(
                "80", name="Battery", device_class="battery"
            ),
            "sensor.alpha_cleaning_time": entity_state(
                "12", name="Cleaning time", device_class="duration"
            ),
            "select.alpha_passes": entity_state(
                "one_pass", options=["one_pass", "two_pass"]
            ),
            "select.alpha_water": entity_state(
                "medium", options=["low", "medium", "high"]
            ),
            "select.alpha_mode": entity_state("vacuum", options=["vacuum", "mop"]),
            "select.alpha_route": entity_state(
                "standard", options=["standard", "deep"]
            ),
        }
        hass = SimpleNamespace(states=SimpleNamespace(get=states.get))

        profile = discovery._find_profile(hass, entries, Registry())

        self.assertEqual(profile.battery_entity_id, "sensor.alpha_battery")
        self.assertEqual(profile.cleaning_time_entity_id, "sensor.alpha_cleaning_time")
        self.assertEqual(profile.passes_select_entity_id, "select.alpha_passes")
        self.assertEqual(profile.mop_intensity_select_entity_id, "select.alpha_water")
        self.assertEqual(profile.mode_select_entity_id, "select.alpha_mode")
        self.assertEqual(profile.mop_mode_select_entity_id, "select.alpha_route")
        self.assertEqual(
            discovery._state_options(hass, "select.alpha_passes"),
            ("one_pass", "two_pass"),
        )
        self.assertEqual(discovery._state_options(hass, "select.missing"), ())

        evidence = discovery._adapter_evidence(hass, entries)
        self.assertEqual(
            discovery._verified_operation_mode(evidence).entity_id,
            "select.alpha_mode",
        )
        self.assertIsNone(discovery._verified_operation_mode(()))

    def test_entity_area_resolution_prefers_entity_then_device(self) -> None:
        devices = Registry({"device": SimpleNamespace(area_id="device-area")})
        self.assertEqual(
            discovery._entity_area_id(
                registry_entry("sensor.one", area_id="entity-area"), devices
            ),
            "entity-area",
        )
        self.assertEqual(
            discovery._entity_area_id(
                registry_entry("sensor.two", device_id="device"), devices
            ),
            "device-area",
        )
        self.assertIsNone(
            discovery._entity_area_id(
                registry_entry("sensor.three", device_id=None), devices
            )
        )


class RegistryDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_filters_unserved_excluded_and_unlocated_objects(
        self,
    ) -> None:
        vacuum = registry_entry(
            "vacuum.alpha", name="Alpha", original_name="Original Alpha"
        )
        operation = registry_entry("select.alpha_mode", translation_key="cleaning_mode")
        occupancy = registry_entry(
            "binary_sensor.study_motion",
            id="registry-motion",
            area_id="study",
            device_id=None,
        )
        unsupported = registry_entry(
            "binary_sensor.study_door",
            area_id="study",
            device_id=None,
        )
        unlocated = registry_entry(
            "binary_sensor.nowhere_motion",
            device_id=None,
            original_device_class="motion",
        )
        excluded = registry_entry(
            "binary_sensor.excluded_motion",
            device_id="device-excluded",
            original_device_class="motion",
        )
        entries = Registry(
            {
                item.entity_id: item
                for item in (
                    vacuum,
                    operation,
                    occupancy,
                    unsupported,
                    unlocated,
                    excluded,
                )
            }
        )
        devices = Registry(
            {
                "device-robot": SimpleNamespace(
                    area_id="dock",
                    labels=set(),
                    name_by_user="Robot Device",
                    name="Alpha Device",
                ),
                "device-excluded": SimpleNamespace(
                    area_id="study", labels={"exclude-occupancy"}
                ),
            }
        )
        areas = Registry(
            {
                "dock": SimpleNamespace(
                    id="dock", name="Dock", floor_id="ground", labels=set()
                ),
                "study": SimpleNamespace(
                    id="study", name="Study", floor_id="ground", labels=set()
                ),
                "excluded": SimpleNamespace(
                    id="excluded",
                    name="Excluded",
                    floor_id="ground",
                    labels={"exclude-room"},
                ),
                "upper": SimpleNamespace(
                    id="upper", name="Upper", floor_id="upper", labels=set()
                ),
                "unfloored": SimpleNamespace(
                    id="unfloored", name="No Floor", floor_id=None, labels=set()
                ),
            }
        )
        labels = Registry(
            {
                "exclude-occupancy": SimpleNamespace(name="Robovac Exclude Occupancy"),
                "exclude-room": SimpleNamespace(name="Robovac Exclude"),
            }
        )
        states = {
            "vacuum.alpha": entity_state(
                "docked",
                name="Alpha",
                supported_features=int(
                    discovery.VacuumEntityFeature.CLEAN_AREA
                    | discovery.VacuumEntityFeature.SEND_COMMAND
                ),
                fan_speed_list=["quiet", "max"],
            ),
            "select.alpha_mode": entity_state("vacuum", options=["vacuum", "mop"]),
            "binary_sensor.study_motion": entity_state("off", device_class="motion"),
            "binary_sensor.study_door": entity_state("off", device_class="door"),
        }
        hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
        capabilities = AdapterCapabilities(
            "fake", 1, True, frozenset({1}), supported_operations=frozenset({"vacuum"})
        )
        adapter = SimpleNamespace(adapter_id="fake", schema_version=1)

        with (
            patch.object(discovery.er, "async_get", return_value=entries),
            patch.object(discovery.dr, "async_get", return_value=devices),
            patch.object(discovery.ar, "async_get", return_value=areas),
            patch.object(discovery.lr, "async_get", return_value=labels),
            patch.object(
                discovery,
                "async_resolve_adapter",
                AsyncMock(return_value=(adapter, capabilities, None)),
            ),
        ):
            result = await discovery.async_discover(hass)

        self.assertEqual(tuple(result.robots), ("vacuum.alpha",))
        found_robot = result.robots["vacuum.alpha"]
        self.assertEqual(found_robot.floor_id, "ground")
        self.assertTrue(found_robot.supports_area_clean)
        self.assertTrue(found_robot.supports_send_command)
        self.assertEqual(found_robot.profile.mode_select_entity_id, "select.alpha_mode")
        self.assertEqual(set(result.rooms), {"dock", "study"})
        self.assertEqual(
            result.rooms["study"].fallback_entity_ids,
            ("binary_sensor.study_motion",),
        )
        self.assertEqual(len(result.rooms["study"].occupancy_sources), 1)


if __name__ == "__main__":
    unittest.main()
