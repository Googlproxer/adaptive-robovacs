"""Per-config-entry FIFO command worker."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .commands import (
    EvaluateCommand,
    RefreshDiscoveryCommand,
    SchedulerCommand,
    SchedulerCommandResult,
)
from .const import DOMAIN


class ApplicationClosedError(RuntimeError):
    """The config-entry application no longer accepts commands."""


@dataclass(slots=True)
class _Envelope:
    command: SchedulerCommand
    result: asyncio.Future[SchedulerCommandResult]
    coalesce_key: str | None = None


class ApplicationCommandQueue:
    """Serialize state changes and outbound actions for one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        handler: Callable[[SchedulerCommand], Awaitable[SchedulerCommandResult]],
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._handler = handler
        self._queue: asyncio.Queue[_Envelope | None] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._closing = False
        self._coalesced: dict[str, asyncio.Future[SchedulerCommandResult]] = {}

    async def async_start(self) -> None:
        """Start the config-entry-owned worker once."""

        if self._worker is not None:
            return
        self._worker = self._entry.async_create_background_task(
            self._hass,
            self._async_worker(),
            name=f"{DOMAIN}:{self._entry.entry_id}:commands",
        )

    def begin_shutdown(self) -> None:
        """Reject newly submitted work synchronously."""

        self._closing = True

    def cancel_shutdown(self) -> None:
        """Resume submissions when Home Assistant rejects platform unload."""

        if self._worker is not None:
            self._closing = False

    async def async_execute(self, command: SchedulerCommand) -> SchedulerCommandResult:
        """Enqueue one command and wait for its ordered result."""

        if self._closing:
            raise ApplicationClosedError("Adaptive RoboVacs is shutting down")
        if self._worker is None:
            return await self._handler(command)
        if asyncio.current_task() is self._worker:
            raise RuntimeError(
                "recursive command execution must be enqueued as follow-up work"
            )
        coalesce_key: str | None = None
        if isinstance(command, EvaluateCommand) and command.coalesce:
            coalesce_key = f"evaluate:{command.dry_run}:{command.reason}"
        elif isinstance(command, RefreshDiscoveryCommand) and command.coalesce:
            coalesce_key = "refresh-discovery"
        if coalesce_key and (pending := self._coalesced.get(coalesce_key)):
            return await asyncio.shield(pending)
        result: asyncio.Future[SchedulerCommandResult] = self._hass.loop.create_future()
        if coalesce_key:
            self._coalesced[coalesce_key] = result
        await self._queue.put(_Envelope(command, result, coalesce_key))
        return await asyncio.shield(result)

    async def async_close(self) -> None:
        """Drain accepted work, stop the worker, and resolve no work twice."""

        self._closing = True
        worker = self._worker
        if worker is None:
            return
        await self._queue.put(None)
        await worker
        self._worker = None

    async def _async_worker(self) -> None:
        while (envelope := await self._queue.get()) is not None:
            try:
                value = await self._handler(envelope.command)
            except Exception as err:
                if not envelope.result.done():
                    envelope.result.set_exception(err)
            else:
                if not envelope.result.done():
                    envelope.result.set_result(value)
            finally:
                if envelope.coalesce_key:
                    self._coalesced.pop(envelope.coalesce_key, None)
                self._queue.task_done()
        self._queue.task_done()
