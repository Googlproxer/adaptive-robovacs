"""Pure adapter schema and Roborock mapping tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import unittest


PACKAGE_PATH = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs"
PACKAGE_NAME = "adaptive_robovacs_adapter_test"
package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(PACKAGE_PATH)]
sys.modules[PACKAGE_NAME] = package
adapters_package = types.ModuleType(f"{PACKAGE_NAME}.adapters")
adapters_package.__path__ = [str(PACKAGE_PATH / "adapters")]
sys.modules[adapters_package.__name__] = adapters_package

homeassistant = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
helpers = sys.modules.setdefault(
    "homeassistant.helpers", types.ModuleType("homeassistant.helpers")
)
entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
entity_registry.async_get = lambda _hass: None
sys.modules[entity_registry.__name__] = entity_registry
helpers.entity_registry = entity_registry
homeassistant.helpers = helpers


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_load(f"{PACKAGE_NAME}.models", PACKAGE_PATH / "models.py")
_load(f"{PACKAGE_NAME}.adapters.base", PACKAGE_PATH / "adapters" / "base.py")
_load(f"{PACKAGE_NAME}.adapters.generic", PACKAGE_PATH / "adapters" / "generic.py")
roborock = _load(
    f"{PACKAGE_NAME}.adapters.roborock", PACKAGE_PATH / "adapters" / "roborock.py"
)
registry = _load(
    f"{PACKAGE_NAME}.adapters.registry", PACKAGE_PATH / "adapters" / "registry.py"
)
base = sys.modules[f"{PACKAGE_NAME}.adapters.base"]


class RoborockMappingTests(unittest.TestCase):
    def test_numeric_mapping_preserves_order_and_deduplicates(self) -> None:
        result = roborock.resolve_roborock_area_mapping(
            {
                "area_mapping": {"study": [8, "8", "10"]},
                "last_seen_segments": [{"id": "8"}, {"id": "10"}],
            },
            ["study"],
        )
        self.assertEqual(result.targets, (8, 10))

    def test_single_map_compound_mapping_is_normalized(self) -> None:
        result = roborock.resolve_roborock_area_mapping(
            {
                "area_mapping": {"kitchen": ["42_6", "42_5"]},
                "last_seen_segments": [
                    {"id": "42_5"},
                    {"id": "42_6"},
                ],
            },
            ["kitchen"],
        )

        self.assertEqual(result.targets, (6, 5))

    def test_missing_stale_and_cross_map_mappings_fail_closed(self) -> None:
        cases = (
            (
                {"area_mapping": {}, "last_seen_segments": [{"id": "1"}]},
                "area_mapping_missing",
            ),
            (
                {
                    "area_mapping": {"study": ["2"]},
                    "last_seen_segments": [{"id": "1"}],
                },
                "area_mapping_stale",
            ),
            (
                {
                    "area_mapping": {"study": ["1_2", "2_3"]},
                    "last_seen_segments": [{"id": "1_2"}, {"id": "2_3"}],
                },
                "area_mapping_ambiguous",
            ),
        )
        for options, code in cases:
            with self.subTest(code=code), self.assertRaises(
                roborock.RoborockMappingError
            ) as raised:
                roborock.resolve_roborock_area_mapping(options, ["study"])
            self.assertEqual(raised.exception.code, code)

    def test_native_payload_uses_one_repeat_two_command(self) -> None:
        self.assertEqual(
            roborock.build_roborock_two_pass_payload((6, 5)),
            {
                "command": "app_segment_clean",
                "params": [{"segments": [6, 5], "repeat": 2}],
            },
        )


class AdapterResolverTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _context(platform: str, *, send_command: bool):
        profile = types.SimpleNamespace(
            supports_double_pass=False,
            supports_mopping=False,
            mode_options=(),
            mop_mode_options=(),
            mop_intensity_options=(),
        )
        return base.AdapterMatchContext(
            entity_id="vacuum.test",
            platform=platform,
            supports_area_clean=True,
            supports_send_command=send_command,
            profile=profile,
            fan_speed_options=("quiet", "max"),
        )

    async def test_unknown_vendor_gets_portable_generic_fallback(self) -> None:
        adapter, capabilities, diagnostic = await registry.async_resolve_adapter(
            None, self._context("unknown_vendor", send_command=False)
        )
        self.assertEqual(adapter.adapter_id, "generic")
        self.assertTrue(capabilities.portable_area_clean)
        self.assertEqual(capabilities.supported_pass_counts, frozenset({1}))
        self.assertIsNone(diagnostic)

    async def test_roborock_is_selected_from_platform_and_command_features(self) -> None:
        adapter, capabilities, diagnostic = await registry.async_resolve_adapter(
            None, self._context("roborock", send_command=True)
        )
        self.assertEqual(adapter.adapter_id, "roborock")
        self.assertEqual(capabilities.supported_pass_counts, frozenset({1, 2}))
        self.assertEqual(capabilities.native_area_pass_counts, frozenset({2}))
        self.assertEqual(capabilities.fan_speed_options, ("quiet", "max"))
        self.assertIsNone(diagnostic)


if __name__ == "__main__":
    unittest.main()
