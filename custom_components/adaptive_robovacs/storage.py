"""Home Assistant Store boundary for typed scheduler state."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from .const import STORAGE_KEY, STORE_VERSION
from .state import SchedulerState, StateSchemaError


class SchedulerWriteError(HomeAssistantError):
    """A requested checkpoint did not reach persistent storage."""


class _CheckpointStore(Store[dict[str, object]]):
    """Require an acknowledged atomic write, with JSON encoding in the executor.

    Core Store logs and consumes serialization/write errors. Its write hook is
    the narrow compatibility seam needed to distinguish a completed write from
    a logged failure, read-only store or a write deferred during shutdown.
    Contract tests exercise this against the real Home Assistant Store.
    """

    def __init__(self, hass: HomeAssistant, version: int, key: str) -> None:
        super().__init__(
            hass, version, key, atomic_writes=True, serialize_in_event_loop=False
        )
        self._checkpoint_lock = asyncio.Lock()
        self._written_payload: object = None

    async def async_save(self, data: dict[str, object]) -> None:
        async with self._checkpoint_lock:
            self._written_payload = None
            try:
                # Cancelling an executor await cannot stop the filesystem
                # thread. Drain it before releasing our lock, so an older
                # atomic replace cannot overtake a subsequent checkpoint.
                operation = asyncio.create_task(super().async_save(data))
                cancelled = False
                while not operation.done():
                    try:
                        await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        cancelled = True
                operation.result()
                if cancelled:
                    raise asyncio.CancelledError
                if self._written_payload is not data:
                    raise SchedulerWriteError("Scheduler checkpoint could not be saved")
            finally:
                self._written_payload = None

    async def _async_write_data(self, data: dict[str, Any]) -> None:
        await super()._async_write_data(data)
        self._written_payload = data["data"]


@dataclass(frozen=True, slots=True)
class StateLoadResult:
    """Result of parsing and validating one persisted scheduler payload."""

    state: SchedulerState
    migrated: bool
    safe_mode: bool = False
    error: StateSchemaError | None = None


class SchedulerStore:
    """Load and save only validated typed scheduler aggregates."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._store: Store[dict[str, object]] = _CheckpointStore(
            hass,
            STORE_VERSION,
            f"{STORAGE_KEY}.{entry_id}",
        )

    async def async_load(self, entry_data: Mapping[str, object]) -> StateLoadResult:
        """Parse the entire payload before allowing any migrated write."""

        payload = await self._store.async_load()
        try:
            state, migrated = SchedulerState.from_store(payload, entry_data)
            state.encode()
        except StateSchemaError as err:
            state = SchedulerState.create(entry_data)
            state.global_settings.observe_only = True
            return StateLoadResult(
                state=state,
                migrated=False,
                safe_mode=True,
                error=err,
            )
        return StateLoadResult(state=state, migrated=migrated)

    async def async_save(
        self, state: SchedulerState, *, payload: dict[str, object] | None = None
    ) -> None:
        """Validate once, then persist the detached payload used for comparison."""

        if payload is None:
            payload = state.encode()
        else:
            SchedulerState._validate_current_schema(payload)
        await self._store.async_save(payload)

    async def async_reset_unsafe_state(
        self, entry_data: Mapping[str, object]
    ) -> tuple[SchedulerState, str]:
        """Back up the exact unsafe payload before a user-requested reset."""

        payload = await self._store.async_load()
        if payload is None:
            raise StateSchemaError("the unsafe Store is no longer present")
        try:
            SchedulerState.from_store(payload, entry_data)
        except StateSchemaError:
            pass
        else:
            raise StateSchemaError("the Store has become valid; reset was cancelled")
        backup_key = (
            f"{STORAGE_KEY}.{self._entry_id}.reset_backup."
            f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"
        )
        backup: Store[dict[str, object]] = _CheckpointStore(
            self._hass, STORE_VERSION, backup_key
        )
        await backup.async_save({"original_payload": payload})
        if (await backup.async_load()) != {"original_payload": payload}:
            raise StateSchemaError("the unsafe Store backup could not be verified")
        state = SchedulerState.create(entry_data)
        state.global_settings.observe_only = True
        encoded = state.encode()
        await self._store.async_save(encoded)
        restored = await self._store.async_load()
        verified, _ = SchedulerState.from_store(restored, entry_data)
        if restored != encoded or not verified.global_settings.observe_only:
            raise StateSchemaError("the reset Store could not be verified")
        return verified, backup_key

    async def async_remove(self) -> None:
        """Remove scheduler data when its config entry is deleted."""

        await self._store.async_remove()
