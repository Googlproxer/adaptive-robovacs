"""Portable Home Assistant vacuum adapter."""

from __future__ import annotations

from typing import Any

from ..models import (
    AdapterCapabilities,
    AdapterDispatchRequest,
    AdapterDispatchResult,
    WaterReadiness,
)
from .base import AdapterMatchContext, VacuumAdapter


def evidence_supports_mopping(context: AdapterMatchContext) -> bool:
    """Verify an explicit same-device operation selector from transient evidence."""

    if context.profile.supports_mopping:
        return True
    for evidence in context.entities:
        if evidence.domain != "select":
            continue
        options = {
            str(option).strip().lower().replace("_", "-").replace(" ", "-")
            for option in evidence.options
        }
        if options.intersection({"vacuum", "vacuum-only"}) and options.intersection(
            {"mop", "mop-only", "vac-and-mop", "vacuum-and-mop"}
        ):
            return True
    return False


class GenericVacuumAdapter(VacuumAdapter):
    """Fallback adapter using only standard Home Assistant actions."""

    adapter_id = "generic"
    schema_version = 2
    priority = -1000

    async def async_capabilities(
        self, hass: Any, context: AdapterMatchContext
    ) -> AdapterCapabilities:
        del hass
        passes = {1}
        if context.profile.supports_double_pass:
            passes.add(2)
        operations = {"vacuum"}
        water = WaterReadiness.unsupported()
        if evidence_supports_mopping(context):
            operations.add("mop")
            water = WaterReadiness.confirmation_required()
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
            water_readiness=water,
            vacuum_pass_counts=frozenset(passes),
            # A generic shared repeat control only proves portable vacuum repeat.
            # Vendors must explicitly advertise mop repeat semantics.
            mop_pass_counts=(frozenset({1}) if "mop" in operations else frozenset()),
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
        if (
            request.operation == "mop"
            and capabilities.water_readiness.status == "confirmation_required"
            and not bool(request.cleaning_profile.get("water_confirmed"))
        ):
            return AdapterDispatchResult(
                "blocked",
                "water_confirmation_required",
                "Water confirmation is required before mopping.",
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
