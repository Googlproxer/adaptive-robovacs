"""Typed contract shared by generic and vendor vacuum adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ..models import AdapterCapabilities, AdapterDispatchRequest, AdapterDispatchResult


@dataclass(frozen=True, slots=True)
class AdapterEntityEvidence:
    """Transient same-device registry/state evidence supplied to an adapter."""

    entity_id: str
    domain: str
    platform: str
    translation_key: str | None
    device_class: str | None
    state: str | None
    options: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AdapterMatchContext:
    """Stable registry and feature evidence available during discovery."""

    entity_id: str
    platform: str
    supports_area_clean: bool
    supports_send_command: bool
    profile: Any
    fan_speed_options: tuple[str, ...] = ()
    device_id: str | None = None
    entities: tuple[AdapterEntityEvidence, ...] = ()


class VacuumAdapter(ABC):
    """Interface implemented by every integration-owned vacuum adapter."""

    adapter_id = "base"
    schema_version = 1
    priority = 0
    platforms: frozenset[str] = frozenset()

    def matches(self, context: AdapterMatchContext) -> bool:
        """Return whether stable registry metadata selects this adapter."""

        return context.platform in self.platforms

    @abstractmethod
    async def async_capabilities(
        self, hass: Any, context: AdapterMatchContext
    ) -> AdapterCapabilities:
        """Return current normalized capabilities without dispatching."""

    @abstractmethod
    async def async_preflight(
        self,
        hass: Any,
        context: AdapterMatchContext,
        request: AdapterDispatchRequest,
    ) -> AdapterDispatchResult:
        """Validate a request without changing vacuum state."""

    @abstractmethod
    async def async_dispatch(
        self,
        hass: Any,
        context: AdapterMatchContext,
        request: AdapterDispatchRequest,
    ) -> AdapterDispatchResult:
        """Perform at most one clean dispatch."""
