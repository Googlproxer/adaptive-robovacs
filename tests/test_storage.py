"""Tests for safe typed persistence loading."""

from __future__ import annotations

import unittest

from custom_components.adaptive_robovacs.state import SCHEMA_VERSION, SchedulerState
from custom_components.adaptive_robovacs.storage import SchedulerStore

ENTRY_DATA = {
    "observe_only": False,
    "forecast_confidence": 75,
    "unresolved_start": "00:00",
    "unresolved_end": "04:00",
}


class _Store:
    def __init__(self, payload):
        self.payload = payload
        self.saved = []

    async def async_load(self):
        return self.payload

    async def async_save(self, payload):
        self.saved.append(payload)

    async def async_remove(self):
        self.payload = None


def scheduler_store(payload):
    result = SchedulerStore.__new__(SchedulerStore)
    result._store = _Store(payload)
    return result


class SchedulerStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_retired_keys_cannot_trigger_a_write_of_malformed_state(self) -> None:
        payload = SchedulerState.create(ENTRY_DATA).to_store()
        payload["global"].update(hall_start="09:00", hall_end="20:00")
        payload["global"]["unresolved_start"] = "bad"
        store = scheduler_store(payload)

        loaded = await store.async_load(ENTRY_DATA)

        self.assertTrue(loaded.safe_mode)
        self.assertFalse(loaded.migrated)
        self.assertEqual(store._store.saved, [])
        self.assertIs(store._store.payload, payload)

    async def test_malformed_current_data_enters_observe_only_without_write(
        self,
    ) -> None:
        payload = SchedulerState.create(ENTRY_DATA).to_store()
        payload["global"]["unresolved_start"] = "bad"
        store = scheduler_store(payload)

        loaded = await store.async_load(ENTRY_DATA)

        self.assertTrue(loaded.safe_mode)
        self.assertTrue(loaded.state.global_settings.observe_only)
        self.assertIsNotNone(loaded.error)
        self.assertEqual(store._store.saved, [])
        self.assertIs(store._store.payload, payload)

    async def test_newer_schema_enters_safe_mode_without_overwrite(self) -> None:
        payload = {"schema_version": SCHEMA_VERSION + 1, "future": "value"}
        store = scheduler_store(payload)

        loaded = await store.async_load(ENTRY_DATA)

        self.assertTrue(loaded.safe_mode)
        self.assertEqual(store._store.saved, [])
        self.assertEqual(store._store.payload, payload)

    async def test_legacy_data_is_fully_parsed_before_caller_may_save(self) -> None:
        payload = SchedulerState.create(ENTRY_DATA).to_store()
        payload["schema_version"] = 15
        store = scheduler_store(payload)

        loaded = await store.async_load(ENTRY_DATA)

        self.assertTrue(loaded.migrated)
        self.assertFalse(loaded.safe_mode)
        self.assertEqual(store._store.saved, [])
        await store.async_save(loaded.state)
        self.assertEqual(store._store.saved[0]["schema_version"], SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
