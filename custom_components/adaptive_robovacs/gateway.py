"""Home Assistant I/O gateway for discovered vacuum adapters."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Protocol

from homeassistant.core import Context, HomeAssistant

from .adapters.base import AdapterMatchContext
from .adapters.registry import adapter_for_id
from .discovery import DiscoveredRobot
from .models import (
    AdapterCleaningProfile,
    AdapterDispatchRequest,
    AdapterDispatchResult,
)


class VacuumGateway(Protocol):
    """All outbound vacuum I/O used by the scheduler application."""

    async def async_preflight(
        self, robot: DiscoveredRobot, request: AdapterDispatchRequest
    ) -> AdapterDispatchResult: ...

    async def async_validate_profile(
        self, robot: DiscoveredRobot, request: AdapterDispatchRequest
    ) -> AdapterDispatchResult: ...

    async def async_apply_profile(
        self, robot: DiscoveredRobot, request: AdapterDispatchRequest
    ) -> AdapterDispatchResult: ...

    async def async_dispatch(
        self, robot: DiscoveredRobot, request: AdapterDispatchRequest
    ) -> AdapterDispatchResult: ...

    def profile_is_ready(
        self,
        robot: DiscoveredRobot,
        operation: str,
        passes: int,
        cleaning_profile: AdapterCleaningProfile,
    ) -> bool: ...

    async def async_return_to_dock(
        self, robot_entity_id: str, context: Context | None
    ) -> None: ...


class HomeAssistantVacuumGateway:
    """Adapter-backed vacuum gateway using Home Assistant services and state."""

    def __init__(
        self,
        hass: HomeAssistant,
        can_mutate: Callable[[], bool],
    ) -> None:
        self._hass = hass
        self._can_mutate = can_mutate

    def _adapter_context(self, robot: DiscoveredRobot) -> AdapterMatchContext:
        entities = tuple(
            replace(
                evidence,
                state=(
                    state.state
                    if (state := self._hass.states.get(evidence.entity_id))
                    else None
                ),
            )
            for evidence in robot.adapter_entities
        )
        return AdapterMatchContext(
            entity_id=robot.entity_id,
            platform=robot.platform,
            supports_area_clean=robot.supports_area_clean,
            supports_send_command=robot.supports_send_command,
            profile=robot.profile,
            fan_speed_options=robot.adapter_capabilities.fan_speed_options,
            device_id=robot.device_id,
            entities=entities,
            can_mutate=self._can_mutate,
        )

    async def async_preflight(
        self, robot: DiscoveredRobot, request: AdapterDispatchRequest
    ) -> AdapterDispatchResult:
        return await adapter_for_id(robot.adapter_id).async_preflight(
            self._hass,
            self._adapter_context(robot),
            request,
        )

    async def async_validate_profile(
        self, robot: DiscoveredRobot, request: AdapterDispatchRequest
    ) -> AdapterDispatchResult:
        return await adapter_for_id(robot.adapter_id).async_validate_profile(
            self._hass,
            self._adapter_context(robot),
            request,
        )

    async def async_apply_profile(
        self, robot: DiscoveredRobot, request: AdapterDispatchRequest
    ) -> AdapterDispatchResult:
        return await adapter_for_id(robot.adapter_id).async_apply_profile(
            self._hass,
            self._adapter_context(robot),
            request,
        )

    async def async_dispatch(
        self, robot: DiscoveredRobot, request: AdapterDispatchRequest
    ) -> AdapterDispatchResult:
        return await adapter_for_id(robot.adapter_id).async_dispatch(
            self._hass,
            self._adapter_context(robot),
            request,
        )

    def profile_is_ready(
        self,
        robot: DiscoveredRobot,
        operation: str,
        passes: int,
        cleaning_profile: AdapterCleaningProfile,
    ) -> bool:
        selections = (
            (robot.profile.mode_select_entity_id, cleaning_profile.get("mode")),
            (
                robot.profile.mop_mode_select_entity_id,
                cleaning_profile.get("mop_mode"),
            ),
            (
                robot.profile.mop_intensity_select_entity_id,
                cleaning_profile.get("mop_intensity"),
            ),
        )
        for entity_id, option in selections:
            if not entity_id or not option:
                continue
            state = self._hass.states.get(entity_id)
            if (
                state is None
                or state.state in {"unavailable", "unknown"}
                or option not in state.attributes.get("options", [])
            ):
                return False
        if (
            robot.profile.passes_select_entity_id
            and passes
            not in robot.adapter_capabilities.native_pass_counts_for(operation)
        ):
            state = self._hass.states.get(robot.profile.passes_select_entity_id)
            wanted = (
                {"two_pass", "double_pass"}
                if passes == 2
                else {"one_pass", "single_pass"}
            )
            if state is None or not any(
                _slugify(option) in wanted
                for option in state.attributes.get("options", [])
            ):
                return False
        fan_speed = cleaning_profile.get("fan_speed")
        return bool(
            not fan_speed or fan_speed in robot.adapter_capabilities.fan_speed_options
        )

    async def async_return_to_dock(
        self, robot_entity_id: str, context: Context | None
    ) -> None:
        if not self._can_mutate():
            return
        await self._hass.services.async_call(
            "vacuum",
            "return_to_base",
            {"entity_id": robot_entity_id},
            blocking=True,
            context=context,
        )


def _slugify(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")
