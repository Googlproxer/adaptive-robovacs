"""Behavioral tests for the per-entry application command queue."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from custom_components.adaptive_robovacs.command_queue import (
    ApplicationClosedError,
    ApplicationCommandQueue,
)
from custom_components.adaptive_robovacs.commands import (
    EvaluateCommand,
    RefreshDiscoveryCommand,
    StateChangedCommand,
)
from custom_components.adaptive_robovacs.models import EvaluationCause, EvaluationMode


class _Entry:
    entry_id = "entry-1"

    def __init__(self) -> None:
        self.background_task_names = []

    @staticmethod
    def async_create_task(_hass, _coroutine, *, name):
        raise AssertionError(f"persistent worker registered as foreground task: {name}")

    def async_create_background_task(self, _hass, coroutine, *, name):
        self.background_task_names.append(name)
        return asyncio.create_task(coroutine, name=name)


class ApplicationCommandQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.seen = []
        self.hass = SimpleNamespace(loop=asyncio.get_running_loop())

        async def handler(command):
            self.seen.append(command)
            await asyncio.sleep(0)
            return {"kind": type(command).__name__}

        self.entry = _Entry()
        self.queue = ApplicationCommandQueue(self.hass, self.entry, handler)
        await self.queue.async_start()

    async def asyncTearDown(self) -> None:
        await self.queue.async_close()

    async def test_preserves_fifo_order_for_state_transitions(self) -> None:
        first = StateChangedCommand("vacuum.one", "docked", "cleaning", None)
        second = StateChangedCommand("vacuum.one", "cleaning", "returning", None)

        await asyncio.gather(
            self.queue.async_execute(first),
            self.queue.async_execute(second),
        )

        self.assertEqual(self.seen, [first, second])

    async def test_persistent_worker_is_a_config_entry_background_task(self) -> None:
        self.assertEqual(
            self.entry.background_task_names,
            ["adaptive_robovacs:entry-1:commands"],
        )

    async def test_coalesces_only_duplicate_refresh_work(self) -> None:
        gate = asyncio.Event()
        calls = []

        async def handler(command):
            calls.append(command)
            await gate.wait()
            return {"done": True}

        await self.queue.async_close()
        self.queue = ApplicationCommandQueue(self.hass, _Entry(), handler)
        await self.queue.async_start()
        command = RefreshDiscoveryCommand("device-labels")
        first = asyncio.create_task(self.queue.async_execute(command))
        await asyncio.sleep(0)
        second = asyncio.create_task(self.queue.async_execute(command))
        await asyncio.sleep(0)
        gate.set()

        self.assertEqual(await first, {"done": True})
        self.assertEqual(await second, {"done": True})
        self.assertEqual(calls, [command])

    async def test_different_evaluation_causes_are_not_coalesced(self) -> None:
        interval = EvaluateCommand(
            EvaluationMode.DISPATCH,
            EvaluationCause.INTERVAL,
            coalesce=True,
        )
        recovery = EvaluateCommand(
            EvaluationMode.DISPATCH,
            EvaluationCause.RECOVERY,
            coalesce=True,
        )

        await asyncio.gather(
            self.queue.async_execute(interval),
            self.queue.async_execute(recovery),
        )

        self.assertEqual(self.seen, [interval, recovery])

    async def test_duplicate_state_change_evaluations_share_one_result(self) -> None:
        gate = asyncio.Event()
        calls = []

        async def handler(command):
            calls.append(command)
            await gate.wait()
            return {"done": True}

        await self.queue.async_close()
        self.queue = ApplicationCommandQueue(self.hass, _Entry(), handler)
        await self.queue.async_start()
        command = EvaluateCommand(
            EvaluationMode.DISPATCH,
            EvaluationCause.STATE_CHANGE,
            coalesce=True,
        )
        first = asyncio.create_task(self.queue.async_execute(command))
        await asyncio.sleep(0)
        second = asyncio.create_task(self.queue.async_execute(command))
        await asyncio.sleep(0)
        gate.set()

        self.assertEqual(await first, {"done": True})
        self.assertEqual(await second, {"done": True})
        self.assertEqual(calls, [command])

    async def test_events_arriving_during_evaluation_produce_one_follow_up(
        self,
    ) -> None:
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        second_started = asyncio.Event()
        release_second = asyncio.Event()
        calls = []
        evaluation_count = 0

        async def handler(command):
            nonlocal evaluation_count
            calls.append(command)
            if isinstance(command, EvaluateCommand):
                evaluation_count += 1
                if evaluation_count == 1:
                    first_started.set()
                    await release_first.wait()
                elif evaluation_count == 2:
                    second_started.set()
                    await release_second.wait()
            return None

        await self.queue.async_close()
        self.queue = ApplicationCommandQueue(self.hass, _Entry(), handler)
        await self.queue.async_start()
        evaluation = EvaluateCommand(
            EvaluationMode.DISPATCH,
            EvaluationCause.STATE_CHANGE,
            coalesce=True,
        )
        first = asyncio.create_task(self.queue.async_execute(evaluation))
        await first_started.wait()

        async def ingest(entity_id: str) -> None:
            await self.queue.async_execute(
                StateChangedCommand(entity_id, "off", "on", None)
            )
            await asyncio.sleep(0)
            await self.queue.async_execute(evaluation)

        arrivals = [
            asyncio.create_task(ingest("binary_sensor.one")),
            asyncio.create_task(ingest("binary_sensor.two")),
        ]
        await asyncio.sleep(0)
        release_first.set()
        await first
        await second_started.wait()
        await asyncio.sleep(0)
        release_second.set()
        await asyncio.gather(*arrivals)

        self.assertEqual(evaluation_count, 2)
        self.assertEqual(
            [item.entity_id for item in calls if isinstance(item, StateChangedCommand)],
            ["binary_sensor.one", "binary_sensor.two"],
        )

    async def test_shutdown_rejects_new_work_and_drains_accepted_work(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def handler(command):
            started.set()
            await release.wait()
            return {"kind": type(command).__name__}

        await self.queue.async_close()
        self.queue = ApplicationCommandQueue(self.hass, _Entry(), handler)
        await self.queue.async_start()
        accepted = asyncio.create_task(
            self.queue.async_execute(RefreshDiscoveryCommand("accepted", False))
        )
        await started.wait()
        self.queue.begin_shutdown()

        with self.assertRaises(ApplicationClosedError):
            await self.queue.async_execute(RefreshDiscoveryCommand("rejected", False))

        release.set()
        await self.queue.async_close()
        self.assertEqual(await accepted, {"kind": "RefreshDiscoveryCommand"})


if __name__ == "__main__":
    unittest.main()
