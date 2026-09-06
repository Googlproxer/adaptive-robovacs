"""Home Assistant Store boundary for typed scheduler state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORAGE_KEY, STORE_VERSION
from .state import SchedulerState, StateSchemaError


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
        self._store: Store[dict[str, object]] = Store(
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

    async def async_save(self, state: SchedulerState) -> None:
        """Serialize one validated aggregate to the existing Store envelope."""

        await self._store.async_save(state.encode())

    async def async_remove(self) -> None:
        """Remove scheduler data when its config entry is deleted."""

        await self._store.async_remove()
