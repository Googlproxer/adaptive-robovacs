"""Tests for registry-driven configuration and integration bootstrap."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from probatio.error import MultipleInvalid

from custom_components.adaptive_robovacs import async_setup
from custom_components.adaptive_robovacs.config_flow import (
    AdaptiveRoboVacsConfigFlow,
)
from custom_components.adaptive_robovacs.const import (
    CONF_FORECAST_CONFIDENCE,
    CONF_HALL_END,
    CONF_HALL_START,
    CONF_OBSERVE_ONLY,
    CONF_UNRESOLVED_END,
    CONF_UNRESOLVED_START,
    DOMAIN,
    NAME,
)


class ConfigFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_form_defaults_validation_and_entry_creation(self) -> None:
        flow = AdaptiveRoboVacsConfigFlow()
        flow.hass = SimpleNamespace()
        unique_id = AsyncMock()
        configured = Mock()
        with (
            patch.object(flow, "async_set_unique_id", unique_id),
            patch.object(flow, "_abort_if_unique_id_configured", configured),
        ):
            form = await flow.async_step_user()
            data = {
                CONF_OBSERVE_ONLY: False,
                CONF_FORECAST_CONFIDENCE: 75,
                CONF_HALL_START: "08:00",
                CONF_HALL_END: "20:00",
                CONF_UNRESOLVED_START: "01:00",
                CONF_UNRESOLVED_END: "05:00",
            }
            validated = form["data_schema"](data)
            created = await flow.async_step_user(validated)

        self.assertEqual(form["type"].value, "form")
        self.assertEqual(form["step_id"], "user")
        defaults = form["data_schema"]({})
        self.assertTrue(defaults[CONF_OBSERVE_ONLY])
        self.assertEqual(defaults[CONF_FORECAST_CONFIDENCE], 80)
        with self.assertRaises(MultipleInvalid):
            form["data_schema"]({CONF_HALL_START: "25:99"})
        with self.assertRaises(MultipleInvalid):
            form["data_schema"]({CONF_FORECAST_CONFIDENCE: 49})
        self.assertEqual(created["type"].value, "create_entry")
        self.assertEqual(created["title"], NAME)
        self.assertEqual(created["data"], data)
        self.assertEqual(unique_id.await_count, 2)
        unique_id.assert_awaited_with(DOMAIN)
        self.assertEqual(configured.call_count, 2)

    async def test_bootstrap_registers_static_frontend_once(self) -> None:
        register_paths = AsyncMock()
        hass = SimpleNamespace(
            data={},
            http=SimpleNamespace(async_register_static_paths=register_paths),
        )
        with patch(
            "custom_components.adaptive_robovacs.async_register_services",
            AsyncMock(),
        ) as register_services:
            self.assertTrue(await async_setup(hass, {}))
            self.assertTrue(await async_setup(hass, {}))

        self.assertEqual(register_services.await_count, 2)
        register_paths.assert_awaited_once()
        path = register_paths.await_args.args[0][0]
        self.assertEqual(path.url_path, f"/api/{DOMAIN}/frontend")
        self.assertEqual(Path(path.path).name, "frontend")
        self.assertFalse(path.cache_headers)


if __name__ == "__main__":
    unittest.main()
