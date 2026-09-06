"""Services exposed by Adaptive RoboVacs."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .application import SchedulerApplication
from .commands import (
    ActivateRetainedMapCommand,
    CaptureMapSnapshotCommand,
    ClearLegacyDeferralsCommand,
    EvaluateCommand,
    ListRetainedMapsCommand,
    ManualCleanRoomCommand,
    RecordManualCleanCommand,
    SaveFloorPlanCommand,
    SchedulerCommandResult,
    SetRoomAdjacencyCommand,
    VerifyRetainedMapCommand,
)
from .const import (
    DOMAIN,
    SERVICE_ACTIVATE_RETAINED_MAP,
    SERVICE_CAPTURE_MAP_SNAPSHOT,
    SERVICE_CLEAR_LEGACY_DEFERRALS,
    SERVICE_CONFIRM_MAP_SELECTION,
    SERVICE_EVALUATE,
    SERVICE_LIST_LEGACY_DEFERRALS,
    SERVICE_LIST_RETAINED_MAPS,
    SERVICE_MANUAL_CLEAN_ROOM,
    SERVICE_RECORD_MANUAL_CLEAN,
    SERVICE_SAVE_FLOOR_PLAN,
    SERVICE_SET_ROOM_ADJACENCY,
)
from .floor_plans import decode_floor_plan_write
from .models import EvaluationCause, EvaluationMode
from .runtime_data import AdaptiveRoboVacsRuntimeData


def _service_response(result: SchedulerCommandResult) -> dict[str, Any]:
    """Serialize a typed command result at the Home Assistant boundary."""

    if result is None:
        raise ServiceValidationError("The scheduler command returned no response")
    return result.as_response()


def _application(
    hass: HomeAssistant, entry_id: str | None = None
) -> SchedulerApplication:
    entries = {
        entry.entry_id: runtime.application
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
        and isinstance(
            (runtime := getattr(entry, "runtime_data", None)),
            AdaptiveRoboVacsRuntimeData,
        )
    }
    if not entries:
        raise ServiceValidationError("Adaptive RoboVacs is not configured")
    if entry_id:
        if entry_id not in entries:
            raise ServiceValidationError(
                "The selected Adaptive RoboVacs entry is not loaded"
            )
        return entries[entry_id]
    if len(entries) != 1:
        raise ServiceValidationError(
            "entry_id is required when multiple Adaptive RoboVacs entries are loaded"
        )
    return next(iter(entries.values()))


async def _require_admin(hass: HomeAssistant, call: ServiceCall) -> None:
    """Keep topology changes limited to an authenticated Home Assistant admin."""

    if not call.context.user_id:
        raise ServiceValidationError(
            "floor-plan changes require an authenticated administrator"
        )
    user = await hass.auth.async_get_user(call.context.user_id)
    if user is None or not user.is_admin:
        raise ServiceValidationError(
            "floor-plan changes require a Home Assistant administrator"
        )


async def async_register_services(hass: HomeAssistant) -> None:
    """Register services once, including before a config entry is loaded."""

    if hass.services.has_service(DOMAIN, SERVICE_EVALUATE):
        return

    async def evaluate(call: ServiceCall) -> dict[str, Any]:
        return _service_response(
            await _application(hass, call.data.get("entry_id")).async_execute(
                EvaluateCommand(
                    mode=(
                        EvaluationMode.PREVIEW
                        if bool(call.data.get("dry_run", False))
                        else EvaluationMode.DISPATCH
                    ),
                    cause=EvaluationCause.SERVICE,
                )
            )
        )

    async def manual_clean(call: ServiceCall) -> dict[str, Any]:
        return _service_response(
            await _application(hass, call.data.get("entry_id")).async_execute(
                RecordManualCleanCommand(
                    robot_entity_id=call.data["robot_entity_id"],
                    area_ids=tuple(call.data["area_ids"]),
                    operations=tuple(call.data.get("operations", ["vacuum"])),
                )
            )
        )

    async def manual_clean_room(call: ServiceCall) -> dict[str, Any]:
        return _service_response(
            await _application(hass, call.data.get("entry_id")).async_execute(
                ManualCleanRoomCommand(
                    area_id=call.data["area_id"],
                    mode=call.data.get("mode", "configured"),
                    context_id=call.context.id,
                    user_id=call.context.user_id,
                )
            )
        )

    async def list_retained_maps(call: ServiceCall) -> dict[str, Any]:
        return _service_response(
            await _application(hass, call.data.get("entry_id")).async_execute(
                ListRetainedMapsCommand(call.data["robot_entity_id"])
            )
        )

    async def capture_map_snapshot(call: ServiceCall) -> dict[str, Any]:
        return _service_response(
            await _application(hass, call.data.get("entry_id")).async_execute(
                CaptureMapSnapshotCommand(call.data["robot_entity_id"])
            )
        )

    async def activate_retained_map(call: ServiceCall) -> dict[str, Any]:
        return _service_response(
            await _application(hass, call.data.get("entry_id")).async_execute(
                ActivateRetainedMapCommand(
                    call.data["robot_entity_id"],
                    call.data["map_id"],
                    call.data["confirm"],
                )
            )
        )

    async def confirm_map_selection(call: ServiceCall) -> dict[str, Any]:
        return _service_response(
            await _application(hass, call.data.get("entry_id")).async_execute(
                VerifyRetainedMapCommand(
                    call.data["robot_entity_id"], call.data["confirm"]
                )
            )
        )

    async def list_legacy_deferrals(call: ServiceCall) -> dict[str, Any]:
        return {
            "legacy_deferrals": _application(
                hass, call.data.get("entry_id")
            ).legacy_deferral_report()
        }

    async def clear_legacy_deferrals(call: ServiceCall) -> dict[str, Any]:
        return _service_response(
            await _application(hass, call.data.get("entry_id")).async_execute(
                ClearLegacyDeferralsCommand(tuple(call.data["area_ids"]))
            )
        )

    async def save_floor_plan(call: ServiceCall) -> dict[str, Any]:
        await _require_admin(hass, call)
        return _service_response(
            await _application(hass, call.data.get("entry_id")).async_execute(
                SaveFloorPlanCommand(
                    decode_floor_plan_write(
                        floor_id=call.data["floor_id"],
                        revision=call.data["revision"],
                        rooms=call.data["rooms"],
                        edges=tuple(tuple(edge) for edge in call.data["edges"]),
                        sensors=call.data["sensors"],
                        forget_area_ids=tuple(call.data.get("forget_area_ids", [])),
                        forget_sensor_registry_ids=tuple(
                            call.data.get("forget_sensor_registry_ids", [])
                        ),
                    )
                )
            )
        )

    async def set_room_adjacency(call: ServiceCall) -> dict[str, Any]:
        await _require_admin(hass, call)
        return _service_response(
            await _application(hass, call.data.get("entry_id")).async_execute(
                SetRoomAdjacencyCommand(
                    area_id=call.data["area_id"],
                    neighbor_area_ids=tuple(call.data.get("neighbor_area_ids", [])),
                )
            )
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_EVALUATE,
        evaluate,
        schema=vol.Schema(
            {
                vol.Optional("dry_run", default=False): cv.boolean,
                vol.Optional("entry_id"): str,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_RETAINED_MAPS,
        list_retained_maps,
        schema=vol.Schema(
            {
                vol.Required("robot_entity_id"): cv.entity_id,
                vol.Optional("entry_id"): str,
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CAPTURE_MAP_SNAPSHOT,
        capture_map_snapshot,
        schema=vol.Schema(
            {
                vol.Required("robot_entity_id"): cv.entity_id,
                vol.Optional("entry_id"): str,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ACTIVATE_RETAINED_MAP,
        activate_retained_map,
        schema=vol.Schema(
            {
                vol.Required("robot_entity_id"): cv.entity_id,
                vol.Required("map_id"): str,
                vol.Required("confirm"): vol.All(cv.boolean, vol.Equal(True)),
                vol.Optional("entry_id"): str,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CONFIRM_MAP_SELECTION,
        confirm_map_selection,
        schema=vol.Schema(
            {
                vol.Required("robot_entity_id"): cv.entity_id,
                vol.Required("confirm"): vol.All(cv.boolean, vol.Equal(True)),
                vol.Optional("entry_id"): str,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_MANUAL_CLEAN_ROOM,
        manual_clean_room,
        schema=vol.Schema(
            {
                vol.Required("area_id"): str,
                vol.Optional("mode", default="configured"): vol.In(
                    ["configured", "vacuum_only", "mop_only"]
                ),
                vol.Optional("entry_id"): str,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LIST_LEGACY_DEFERRALS,
        list_legacy_deferrals,
        schema=vol.Schema({vol.Optional("entry_id"): str}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_LEGACY_DEFERRALS,
        clear_legacy_deferrals,
        schema=vol.Schema(
            {
                vol.Required("area_ids"): vol.All(cv.ensure_list, [str]),
                vol.Required("confirm"): vol.All(cv.boolean, vol.Equal(True)),
                vol.Optional("entry_id"): str,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RECORD_MANUAL_CLEAN,
        manual_clean,
        schema=vol.Schema(
            {
                vol.Required("robot_entity_id"): cv.entity_id,
                vol.Required("area_ids"): vol.All(cv.ensure_list, [str]),
                vol.Optional("operations", default=["vacuum"]): vol.All(
                    cv.ensure_list, [vol.In(["vacuum", "mop"])]
                ),
                vol.Optional("entry_id"): str,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SAVE_FLOOR_PLAN,
        save_floor_plan,
        schema=vol.Schema(
            {
                vol.Required("floor_id"): str,
                vol.Required("revision"): vol.All(int, vol.Range(min=0)),
                vol.Required("rooms"): dict,
                vol.Required("edges"): list,
                vol.Required("sensors"): dict,
                vol.Optional("forget_area_ids", default=[]): vol.All(
                    cv.ensure_list, [str]
                ),
                vol.Optional("forget_sensor_registry_ids", default=[]): vol.All(
                    cv.ensure_list, [str]
                ),
                vol.Optional("entry_id"): str,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_ROOM_ADJACENCY,
        set_room_adjacency,
        schema=vol.Schema(
            {
                vol.Required("area_id"): str,
                vol.Optional("neighbor_area_ids", default=[]): vol.All(
                    cv.ensure_list, [str]
                ),
                vol.Optional("entry_id"): str,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )


async def async_unregister_services(hass: HomeAssistant) -> None:
    """Keep global services available once registered during the HA process."""

    # Home Assistant requires actions to remain registered for validation even
    # when no config entry is loaded. They raise a helpful error until setup.
    return None
