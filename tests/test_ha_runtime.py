"""Exercise isolation between successive real-Core benchmark contexts."""

import asyncio
import tempfile
import unittest

from homeassistant.helpers import area_registry as ar
from homeassistant.util import dt as dt_util
from homeassistant.util.async_ import run_callback_threadsafe

from tests.ha_runtime import MockConfigEntry, async_test_home_assistant


class CoreRuntimeFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def test_successive_contexts_restore_timezone_registries_and_thread_access(
        self,
    ):
        prior = dt_util.get_default_time_zone()
        for index in range(2):
            with tempfile.TemporaryDirectory(prefix="adaptive-runtime-") as directory:
                async with async_test_home_assistant(config_dir=directory) as hass:
                    self.assertFalse(ar.async_get(hass).areas)
                    entry = MockConfigEntry(domain="synthetic", entry_id="same-entry")
                    entry.add_to_hass(hass)
                    ar.async_get(hass).async_create("Synthetic room")
                    await hass.config.async_set_time_zone("Pacific/Auckland")

                    def thread_callback(value=index):
                        return run_callback_threadsafe(hass.loop, lambda: value).result(
                            timeout=2
                        )

                    result = await asyncio.wait_for(
                        hass.async_add_executor_job(thread_callback),
                        timeout=3,
                    )
                    self.assertEqual(result, index)
            self.assertEqual(dt_util.get_default_time_zone(), prior)
