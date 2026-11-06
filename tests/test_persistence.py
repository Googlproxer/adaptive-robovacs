"""Cache invalidation and real Home Assistant Store durability contracts."""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import storage as ha_storage
from homeassistant.util.file import WriteError
from homeassistant.util.json import SerializationError

from custom_components.adaptive_robovacs.persistence import StatePersistenceEncoder
from custom_components.adaptive_robovacs.state import (
    OccupancySample,
    RoomHistory,
    SchedulerState,
    StateSchemaError,
)
from custom_components.adaptive_robovacs.storage import (
    SchedulerStore,
    SchedulerWriteError,
)
from tests.test_application_state import NOW
from tests.test_cleaning_performance import progress_application
from tests.test_storage import ENTRY_DATA


def populated_state():
    state = SchedulerState.create(ENTRY_DATA)
    state.room_history["study"] = RoomHistory(
        occupancy_samples=[OccupancySample(NOW, index + 1) for index in range(64)]
    )
    return state


class PersistenceEncoderTests(unittest.TestCase):
    def test_warm_saves_reuse_history_but_validate_other_records(self):
        encoder = StatePersistenceEncoder()
        state = populated_state()
        parse = OccupancySample.from_mapping
        with patch.object(OccupancySample, "from_mapping", wraps=parse) as decode:
            first = encoder.prepare(state)
            SchedulerState._validate_current_schema(first)
            self.assertEqual(decode.call_count, 64)
            decode.reset_mock()
            state.room_history["study"].occupancy = "occupied"
            second = encoder.prepare(state)
            SchedulerState._validate_current_schema(second)
            self.assertEqual(decode.call_count, 0)
            self.assertEqual(second, state.to_store())
            second["room_history"]["study"]["opportunity"]["reason"] = "invalid"
            with self.assertRaises(StateSchemaError):
                SchedulerState._validate_current_schema(second)

    def test_every_history_mutation_invalidates_by_value(self):
        def edit_sample(samples):
            samples[10].minutes += 1

        def edit_time(samples):
            samples[10].started_at += timedelta(minutes=1)

        mutations = {
            "edit sample": edit_sample,
            "edit timestamp": edit_time,
            "append": lambda samples: samples.append(deepcopy(samples[0])),
            "prune": lambda samples: samples.pop(0),
            "clear": lambda samples: samples.clear(),
            "reverse": lambda samples: samples.reverse(),
            "replace": lambda samples: samples.__setitem__(
                10, OccupancySample(samples[0].started_at, 400)
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                state = populated_state()
                encoder = StatePersistenceEncoder()
                first = encoder.prepare(state)
                mutate(state.room_history["study"].occupancy_samples)
                with patch.object(
                    OccupancySample, "from_mapping", wraps=OccupancySample.from_mapping
                ) as decode:
                    payload = encoder.prepare(state)
                    SchedulerState._validate_current_schema(payload)
                    self.assertEqual(
                        decode.call_count,
                        len(state.room_history["study"].occupancy_samples),
                    )
                self.assertEqual(payload, state.to_store())
                self.assertNotEqual(
                    first.fingerprint_data(), payload.fingerprint_data()
                )

    def test_prepared_payload_mutation_cannot_poison_cache_or_bypass_validation(self):
        encoder = StatePersistenceEncoder()
        state = populated_state()
        payload = encoder.prepare(state)
        expected_fingerprint = payload.fingerprint_data()
        payload["room_history"]["study"]["occupancy_samples"][0]["minutes"] = 999
        self.assertNotEqual(payload.fingerprint_data(), expected_fingerprint)
        with patch.object(
            OccupancySample, "from_mapping", wraps=OccupancySample.from_mapping
        ) as decode:
            SchedulerState._validate_current_schema(payload)
            self.assertEqual(decode.call_count, 64)
        fresh = encoder.prepare(state)
        self.assertEqual(fresh, state.to_store())
        self.assertEqual(fresh.fingerprint_data(), expected_fingerprint)
        # Both raw mappings and JSON-loaded payloads always take full validation.
        with patch.object(
            OccupancySample, "from_mapping", wraps=OccupancySample.from_mapping
        ) as decode:
            SchedulerState._validate_current_schema(dict(fresh))
            SchedulerState._validate_current_schema(json.loads(json.dumps(fresh)))
            self.assertEqual(decode.call_count, 128)

    def test_snapshots_are_detached_across_state_and_payload_mutation(self):
        encoder = StatePersistenceEncoder()
        state = populated_state()
        first = encoder.prepare(state)
        original = deepcopy(dict(first))
        state.room_history["study"].occupancy_samples[0].minutes = 777
        second = encoder.prepare(state)
        second["room_history"]["study"]["occupancy_samples"][1]["minutes"] = 888
        self.assertEqual(first, original)
        self.assertEqual(set(first), set(state.to_store()))

    def test_removed_room_and_replacement_state_do_not_reuse_stale_history(self):
        encoder = StatePersistenceEncoder()
        state = populated_state()
        encoder.prepare(state)
        state.room_history.clear()
        self.assertFalse(encoder.prepare(state)["room_history"])
        self.assertFalse(encoder._occupancy)
        replacement = populated_state()
        replacement.room_history["study"].occupancy_samples[0].minutes = 789
        self.assertEqual(encoder.prepare(replacement), replacement.to_store())

    def test_equal_datetimes_with_different_dst_folds_invalidate_the_cache(self):
        state = populated_state()
        sample = state.room_history["study"].occupancy_samples[0]
        sample.started_at = datetime(
            2026, 4, 5, 2, 30, tzinfo=ZoneInfo("Australia/Sydney")
        )
        encoder = StatePersistenceEncoder()
        first = encoder.prepare(state)
        sample.started_at = sample.started_at.replace(fold=1)
        second = encoder.prepare(state)
        self.assertNotEqual(first, second)
        self.assertEqual(second, state.to_store())


class HomeAssistantStoreContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="adaptive-store-contract-")
        # Exercise the real Core Store without unrelated registry setup.
        self.hass = HomeAssistant(self.directory.name)
        self.hass.set_state(CoreState.running)
        self.store = SchedulerStore(self.hass, "synthetic-entry")

    async def asyncTearDown(self):
        await self.hass.async_stop()
        self.directory.cleanup()

    async def disk_payload(self):
        path = Path(self.store._store.path)
        text = await self.hass.async_add_executor_job(path.read_text)
        return json.loads(text)["data"]

    async def test_atomic_checkpoint_encodes_off_loop_and_restores_full_state(self):
        state = populated_state()
        main_thread = threading.get_ident()
        threads = []
        original = ha_storage.json_helper.prepare_save_json

        def encode(*args, **kwargs):
            threads.append(threading.get_ident())
            return original(*args, **kwargs)

        with (
            patch.object(
                ha_storage.json_helper, "prepare_save_json", side_effect=encode
            ),
            patch.object(
                ha_storage,
                "write_utf8_file_atomic",
                wraps=ha_storage.write_utf8_file_atomic,
            ) as atomic_write,
        ):
            await self.store.async_save(
                state, payload=StatePersistenceEncoder().prepare(state)
            )
        self.assertTrue(threads)
        self.assertNotIn(main_thread, threads)
        self.assertEqual(atomic_write.call_count, 1)
        self.assertEqual(await self.disk_payload(), state.to_store())
        fresh = SchedulerStore(self.hass, "synthetic-entry")
        loaded = await fresh.async_load(ENTRY_DATA)
        self.assertFalse(loaded.safe_mode)
        self.assertEqual(loaded.state.to_store(), state.to_store())

    async def test_real_consumed_write_errors_do_not_acknowledge_or_advance_fingerprint(
        self,
    ):
        app, job = progress_application()
        app.storage = self.store
        await app._async_save()
        previous = await self.disk_payload()
        fingerprint = app._last_durable_fingerprint
        job.last_observed_at = NOW + timedelta(seconds=1)
        for error in (
            WriteError("synthetic failure"),
            SerializationError("synthetic failure"),
        ):
            with (
                self.subTest(error=type(error).__name__),
                patch.object(
                    ha_storage.json_helper, "prepare_save_json", side_effect=error
                ),
                self.assertLogs("homeassistant.helpers.storage", level="ERROR"),
                self.assertRaises(SchedulerWriteError),
            ):
                await app._async_save()
            self.assertIsNone(app._last_durable_fingerprint)
            self.assertEqual(await self.disk_payload(), previous)
        await app._async_save()
        self.assertNotEqual(app._last_durable_fingerprint, fingerprint)
        restored, _ = SchedulerState.from_store(await self.disk_payload(), ENTRY_DATA)
        self.assertEqual(
            restored.active_jobs["registry-alpha"].last_observed_at,
            job.last_observed_at,
        )

    async def test_failed_atomic_replace_preserves_previous_checkpoint(self):
        state = populated_state()
        await self.store.async_save(state)
        previous = await self.disk_payload()
        state.global_settings.observe_only = True
        with (
            patch.object(
                ha_storage,
                "write_utf8_file_atomic",
                side_effect=WriteError("disk failure"),
            ),
            self.assertLogs("homeassistant.helpers.storage", level="ERROR"),
            self.assertRaises(SchedulerWriteError),
        ):
            await self.store.async_save(state)
        self.assertEqual(await self.disk_payload(), previous)
        await self.store.async_save(state)
        self.assertEqual(await self.disk_payload(), state.to_store())

    async def test_cancelled_replacement_cannot_skip_restoring_unchanged_current_state(
        self,
    ):
        app, _ = progress_application()
        app.storage = self.store
        await app._async_save()
        current = app.state
        candidate = deepcopy(current)
        candidate.global_settings.party_mode = True
        original = self.store.async_save

        async def persisted_without_acknowledgement(state, *, payload=None):
            await original(state, payload=payload)
            raise asyncio.CancelledError

        with (
            patch.object(
                self.store, "async_save", side_effect=persisted_without_acknowledgement
            ),
            self.assertRaises(asyncio.CancelledError),
        ):
            await app._async_commit_state(candidate)
        self.assertIs(app.state, current)
        self.assertIsNone(app._last_durable_fingerprint)
        self.assertTrue((await self.disk_payload())["global"]["party_mode"])
        await app._async_save()
        self.assertEqual(await self.disk_payload(), current.to_store())
        self.assertIsNotNone(app._last_durable_fingerprint)

    async def test_read_only_and_shutdown_deferred_writes_are_not_acknowledged(self):
        state = populated_state()
        await self.store.async_save(state)
        self.store._store.make_read_only()
        with self.assertRaises(SchedulerWriteError):
            await self.store.async_save(state)
        another = SchedulerStore(self.hass, "shutdown-checkpoint")
        prior = self.hass.state
        self.hass.state = CoreState.stopping
        try:
            with self.assertRaises(SchedulerWriteError):
                await another.async_save(state)
        finally:
            self.hass.state = prior

    async def test_concurrent_detached_payloads_each_receive_their_own_acknowledgement(
        self,
    ):
        first = populated_state()
        second = populated_state()
        second.room_history["study"].occupancy_samples[0].minutes = 321
        await asyncio.gather(
            self.store.async_save(first), self.store.async_save(second)
        )
        self.assertEqual(await self.disk_payload(), second.to_store())

    async def test_cancellation_drains_the_write_before_accepting_a_new_checkpoint(
        self,
    ):
        started = threading.Event()
        release = threading.Event()
        calls = []
        original = ha_storage.write_utf8_file_atomic

        def write(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                started.set()
                if not release.wait(timeout=5):
                    raise AssertionError("test did not release the blocked writer")
            return original(*args, **kwargs)

        first = populated_state()
        second = populated_state()
        second.room_history["study"].occupancy_samples[0].minutes = 234
        with patch.object(ha_storage, "write_utf8_file_atomic", side_effect=write):
            old = asyncio.create_task(self.store.async_save(first))
            await self.hass.async_add_executor_job(started.wait)
            old.cancel()
            new = asyncio.create_task(self.store.async_save(second))
            try:
                await asyncio.sleep(0.01)
                self.assertFalse(old.done())
                self.assertFalse(new.done())
                self.assertEqual(len(calls), 1)
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await old
            await new
        self.assertEqual(await self.disk_payload(), second.to_store())


if __name__ == "__main__":
    unittest.main()
