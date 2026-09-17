"""Tests for bounded privacy-safe integration diagnostics."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from custom_components.adaptive_robovacs.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.adaptive_robovacs.metrics import RuntimeMetrics


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_diagnostics_expose_counts_without_household_identifiers(
        self,
    ) -> None:
        metrics = RuntimeMetrics()
        metrics.state_events["meaningful"] = 3
        for index in range(150):
            metrics.record_evaluation("state_change", index / 1000)
            metrics.record_discovery(index / 1000)
        application = SimpleNamespace(
            metrics=metrics,
            discovery=SimpleNamespace(
                rooms={"private-room-id": object()},
                robots={"vacuum.private": object()},
            ),
            _watch_entity_ids={"vacuum.private", "binary_sensor.private"},
            _watch_capability_entity_ids={"select.private"},
            _storage_safe_mode=False,
        )
        entry = SimpleNamespace(
            entry_id="entry-1",
            runtime_data=SimpleNamespace(application=application),
        )
        registry = SimpleNamespace(
            entities={
                "one": SimpleNamespace(config_entry_id="entry-1", domain="sensor"),
                "two": SimpleNamespace(config_entry_id="entry-1", domain="select"),
                "other": SimpleNamespace(config_entry_id="entry-2", domain="sensor"),
            }
        )
        with patch(
            "custom_components.adaptive_robovacs.diagnostics.er.async_get",
            return_value=registry,
        ):
            result = await async_get_config_entry_diagnostics(SimpleNamespace(), entry)

        self.assertEqual(result["runtime"]["topology"]["rooms"], 1)
        self.assertEqual(result["runtime"]["evaluations"]["samples"], 128)
        self.assertEqual(result["runtime"]["discovery"]["samples"], 128)
        self.assertEqual(result["entity_platform_counts"], {"select": 1, "sensor": 1})
        encoded = json.dumps(result)
        self.assertNotIn("private-room-id", encoded)
        self.assertNotIn("vacuum.private", encoded)
        self.assertNotIn("binary_sensor.private", encoded)


if __name__ == "__main__":
    unittest.main()
