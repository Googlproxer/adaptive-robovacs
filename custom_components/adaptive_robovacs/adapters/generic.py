"""Portable Home Assistant vacuum adapter."""

from __future__ import annotations

from typing import Any

from ..models import AdapterCapabilities, AdapterDispatchRequest, AdapterDispatchResult
from .base import AdapterMatchContext, VacuumAdapter


class GenericVacuumAdapter(VacuumAdapter):
    """Fallback adapter using only standard Home Assistant actions."""

    adapter_id = "generic"
    schema_version = 1
    priority = -1000

    async def async_capabilities(
        self, hass: Any, context: AdapterMatchContext
    ) -> AdapterCapabilities:
        del hass
        passes = {1}
        if context.profile.supports_double_pass:
            passes.add(2)
        operations = {"vacuum"}
        if context.profile.supports_mopping:
            operations.update({"mop", "vac_and_mop"})
        return AdapterCapabilities(
            adapter_id=self.adapter_id,
            schema_version=self.schema_version,
            portable_area_clean=context.supports_area_clean,
            supported_pass_counts=frozenset(passes),
            supported_operations=frozenset(operations),
            fan_speed_options=context.fan_speed_options,
            mode_options=context.profile.mode_options,
            mop_mode_options=context.profile.mop_mode_options,
            mop_intensity_options=context.profile.mop_intensity_options,
        )

    async def async_preflight(
        self,
        hass: Any,
        context: AdapterMatchContext,
        request: AdapterDispatchRequest,
    ) -> AdapterDispatchResult:
        capabilities = await self.async_capabilities(hass, context)
        if not request.area_ids or not capabilities.supports(
            request.operation, request.passes
        ):
            return AdapterDispatchResult(
                "unsupported",
                "adapter_request_unsupported",
                "The selected vacuum no longer supports this cleaning request.",
            )
        return AdapterDispatchResult("ready", "ready", "Ready")

    async def async_dispatch(
        self,
        hass: Any,
        context: AdapterMatchContext,
        request: AdapterDispatchRequest,
    ) -> AdapterDispatchResult:
        preflight = await self.async_preflight(hass, context, request)
        if not preflight.ready:
            return preflight
        await hass.services.async_call(
            "vacuum",
            "clean_area",
            {
                "entity_id": request.robot_entity_id,
                "cleaning_area_id": list(request.area_ids),
            },
            blocking=True,
        )
        return AdapterDispatchResult(
            "accepted", "accepted", "Cleaning request accepted"
        )
