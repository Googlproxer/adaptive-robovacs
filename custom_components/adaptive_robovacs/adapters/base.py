"""Typed contract shared by generic and vendor vacuum adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import re
from typing import Any, Callable

from ..models import AdapterCapabilities, AdapterDispatchRequest, AdapterDispatchResult


def _slugify(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


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
    can_mutate: Callable[[], bool] | None = None


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

    async def async_validate_profile(
        self,
        hass: Any,
        context: AdapterMatchContext,
        request: AdapterDispatchRequest,
    ) -> AdapterDispatchResult:
        """Validate exact profile values and live controls without mutation."""

        capabilities = await self.async_capabilities(hass, context)
        values = request.cleaning_profile
        configured = (
            (
                context.profile.mode_select_entity_id,
                values.get("mode"),
                capabilities.mode_options,
            ),
            (
                context.profile.mop_mode_select_entity_id,
                values.get("mop_mode"),
                capabilities.mop_mode_options,
            ),
            (
                context.profile.mop_intensity_select_entity_id,
                values.get("mop_intensity"),
                capabilities.mop_intensity_options,
            ),
        )
        for entity_id, option, options in configured:
            if option is None:
                continue
            if not isinstance(option, str) or option not in options or not entity_id:
                return AdapterDispatchResult(
                    "unsupported",
                    "profile_option_unsupported",
                    "A saved cleaning profile option is no longer supported.",
                )
            state = hass.states.get(entity_id)
            if (
                not state
                or state.state in {"unavailable", "unknown"}
                or option not in state.attributes.get("options", [])
            ):
                return AdapterDispatchResult(
                    "blocked",
                    "profile_control_unavailable",
                    "A cleaning profile control is unavailable.",
                )
        fan_speed = values.get("fan_speed")
        if fan_speed is not None and fan_speed not in capabilities.fan_speed_options:
            return AdapterDispatchResult(
                "unsupported",
                "profile_option_unsupported",
                "A saved fan speed is no longer supported.",
            )
        if (
            context.profile.passes_select_entity_id
            and request.passes
            not in capabilities.native_pass_counts_for(request.operation)
        ):
            wanted = (
                {"two_pass", "double_pass"}
                if request.passes == 2
                else {"one_pass", "single_pass"}
            )
            state = hass.states.get(context.profile.passes_select_entity_id)
            if (
                not state
                or state.state in {"unavailable", "unknown"}
                or not any(
                    _slugify(option) in wanted
                    for option in state.attributes.get("options", [])
                )
            ):
                return AdapterDispatchResult(
                    "blocked",
                    "profile_control_unavailable",
                    "The pass-count control is unavailable.",
                )
        return AdapterDispatchResult("ready", "ready", "Ready")

    async def async_apply_profile(
        self,
        hass: Any,
        context: AdapterMatchContext,
        request: AdapterDispatchRequest,
    ) -> None:
        """Apply one exact profile through portable Home Assistant controls."""

        values = request.cleaning_profile
        for entity_id, key in (
            (context.profile.mode_select_entity_id, "mode"),
            (context.profile.mop_mode_select_entity_id, "mop_mode"),
            (context.profile.mop_intensity_select_entity_id, "mop_intensity"),
        ):
            option = values.get(key)
            if entity_id and isinstance(option, str):
                if context.can_mutate and not context.can_mutate():
                    return
                await hass.services.async_call(
                    "select",
                    "select_option",
                    {"entity_id": entity_id, "option": option},
                    blocking=True,
                )
        fan_speed = values.get("fan_speed")
        if isinstance(fan_speed, str):
            if context.can_mutate and not context.can_mutate():
                return
            await hass.services.async_call(
                "vacuum",
                "set_fan_speed",
                {"entity_id": request.robot_entity_id, "fan_speed": fan_speed},
                blocking=True,
            )
        capabilities = await self.async_capabilities(hass, context)
        if (
            context.profile.passes_select_entity_id
            and request.passes
            not in capabilities.native_pass_counts_for(request.operation)
        ):
            wanted_slugs = (
                {"two_pass", "double_pass"}
                if request.passes == 2
                else {"one_pass", "single_pass"}
            )
            wanted = next(
                (
                    option
                    for option in context.profile.passes_options
                    if _slugify(option) in wanted_slugs
                ),
                None,
            )
            if wanted:
                if context.can_mutate and not context.can_mutate():
                    return
                await hass.services.async_call(
                    "select",
                    "select_option",
                    {
                        "entity_id": context.profile.passes_select_entity_id,
                        "option": wanted,
                    },
                    blocking=True,
                )

    @abstractmethod
    async def async_dispatch(
        self,
        hass: Any,
        context: AdapterMatchContext,
        request: AdapterDispatchRequest,
    ) -> AdapterDispatchResult:
        """Perform at most one clean dispatch."""
