"""Home Assistant service boundary for Adaptive RoboVacs."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.util import slugify
from homeassistant.util import dt as dt_util

from .adapters.base import AdapterMatchContext
from .adapters.registry import adapter_for_id
from .discovery import DiscoveredRobot, DiscoveredRoom
from .models import AdapterDispatchRequest, AdapterDispatchResult
from .repairs_manager import fault_summary

if TYPE_CHECKING:
    from .coordinator import AdaptiveRoboVacCoordinator


_LOGGER = logging.getLogger(__name__)
SERVICE_CALL_TIMEOUT_SECONDS = 30


def _stage_mode_option(options: tuple[str, ...], operation: str) -> str | None:
    """Select an explicit single-operation mode without using a combined mode."""

    wanted = {"vacuum", "vacuum_only"} if operation == "vacuum" else {"mop", "mop_only"}
    return next((option for option in options if slugify(option) in wanted), None)


class HomeAssistantRuntime:
    """Perform native service calls without owning scheduler decisions or state."""

    def __init__(self, coordinator: AdaptiveRoboVacCoordinator) -> None:
        self._coordinator = coordinator

    def _adapter_context(self, robot: DiscoveredRobot) -> AdapterMatchContext:
        entities = tuple(
            replace(
                evidence,
                state=(state.state if (state := self._coordinator.hass.states.get(evidence.entity_id)) else None),
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
        )

    async def async_apply_profile(
        self, robot: DiscoveredRobot, operation: str, passes: int
    ) -> None:
        """Set optional controls discovered on the selected robot device."""

        coordinator = self._coordinator
        profile = robot.profile
        settings = coordinator._robot_settings(robot)
        stage_mode = _stage_mode_option(profile.mode_options, operation)
        selections = [(profile.mode_select_entity_id, stage_mode)]
        if operation == "mop":
            stage_mop_mode = _stage_mode_option(profile.mop_mode_options, operation)
            selections.extend(
                (
                    (
                        profile.mop_mode_select_entity_id,
                        stage_mop_mode or settings.get("mop_mode"),
                    ),
                    (
                        profile.mop_intensity_select_entity_id,
                        settings.get("mop_intensity"),
                    ),
                )
            )
        for entity_id, option in selections:
            if entity_id and option:
                await coordinator.hass.services.async_call(
                    "select", "select_option", {"entity_id": entity_id, "option": option}, blocking=True
                )
        fan_speed = settings.get("fan_speed")
        if fan_speed and fan_speed in robot.adapter_capabilities.fan_speed_options:
            await coordinator.hass.services.async_call(
                "vacuum",
                "set_fan_speed",
                {"entity_id": robot.entity_id, "fan_speed": fan_speed},
                blocking=True,
            )
        if (
            profile.passes_select_entity_id
            and passes not in robot.adapter_capabilities.native_pass_counts_for(operation)
        ):
            wanted_slugs = (
                {"two_pass", "double_pass"}
                if passes == 2
                else {"one_pass", "single_pass"}
            )
            wanted = next(
                (
                    option
                    for option in profile.passes_options
                    if slugify(option) in wanted_slugs
                ),
                None,
            )
            if wanted:
                await coordinator.hass.services.async_call(
                    "select",
                    "select_option",
                    {"entity_id": profile.passes_select_entity_id, "option": wanted},
                    blocking=True,
                )

    def _request(
        self, robot: DiscoveredRobot, candidate: dict[str, Any]
    ) -> AdapterDispatchRequest:
        settings = dict(self._coordinator._robot_settings(robot))
        settings["water_confirmed"] = bool(candidate.get("water_confirmed", False))
        settings["ignore_water_readiness"] = bool(
            candidate.get("ignore_water_readiness", False)
        )
        return AdapterDispatchRequest(
            robot_entity_id=robot.entity_id,
            area_ids=(candidate["room"].area_id,),
            operation=str(candidate["operation"]),
            passes=int(candidate["passes"]),
            cleaning_profile=settings,
        )

    async def async_preflight(
        self, robot: DiscoveredRobot, candidate: dict[str, Any]
    ) -> AdapterDispatchResult:
        """Recheck adapter prerequisites without changing vacuum state."""

        adapter = adapter_for_id(robot.adapter_id)
        return await adapter.async_preflight(
            self._coordinator.hass,
            self._adapter_context(robot),
            self._request(robot, candidate),
        )

    def profile_is_ready(
        self, robot: DiscoveredRobot, operation: str, passes: int
    ) -> bool:
        """Validate configured profile controls without calling a service."""

        settings = self._coordinator._robot_settings(robot)
        stage_mode = _stage_mode_option(robot.profile.mode_options, operation)
        selections = [(robot.profile.mode_select_entity_id, stage_mode)]
        if operation == "mop":
            stage_mop_mode = _stage_mode_option(
                robot.profile.mop_mode_options, operation
            )
            selections.extend(
                (
                    (
                        robot.profile.mop_mode_select_entity_id,
                        stage_mop_mode or settings.get("mop_mode"),
                    ),
                    (
                        robot.profile.mop_intensity_select_entity_id,
                        settings.get("mop_intensity"),
                    ),
                )
            )
        for entity_id, option in selections:
            if not entity_id or not option:
                continue
            state = self._coordinator.hass.states.get(entity_id)
            if (
                not state
                or state.state in {"unavailable", "unknown"}
                or option not in state.attributes.get("options", [])
            ):
                return False
        if (
            robot.profile.passes_select_entity_id
            and passes not in robot.adapter_capabilities.native_pass_counts_for(operation)
        ):
            state = self._coordinator.hass.states.get(
                robot.profile.passes_select_entity_id
            )
            wanted = (
                {"two_pass", "double_pass"}
                if passes == 2
                else {"one_pass", "single_pass"}
            )
            if not state or not any(
                slugify(option) in wanted
                for option in state.attributes.get("options", [])
            ):
                return False
        fan_speed = settings.get("fan_speed")
        return not fan_speed or fan_speed in robot.adapter_capabilities.fan_speed_options

    async def async_dispatch(
        self, robot: DiscoveredRobot, candidate: dict[str, Any], now: datetime
    ) -> tuple[bool, str]:
        """Checkpoint one adapter dispatch and engage the global fault latch on failure."""

        coordinator = self._coordinator
        room: DiscoveredRoom = candidate["room"]
        if coordinator._closing:
            return False, "coordinator shutting down"
        active = {
            "room": room.area_id,
            "operation": candidate["operation"],
            "started": now.isoformat(),
            "seen_cleaning": False,
            "phase": "dispatching",
            "source": "scheduler",
            "expected_minutes": candidate["duration_minutes"],
            "expected_end": (now + timedelta(minutes=candidate["duration_minutes"])).isoformat(),
            "last_observed_at": now.isoformat(),
            "passes": candidate["passes"],
            "adapter_id": robot.adapter_id,
            "adapter_schema_version": robot.adapter_schema_version,
            "occurrence_id": candidate.get("occurrence_id"),
            "stage_index": candidate.get("stage_index"),
        }

        adapter = adapter_for_id(robot.adapter_id)
        context = self._adapter_context(robot)
        request = self._request(robot, candidate)
        try:
            if coordinator._closing:
                return False, "coordinator shutting down"
            preflight = await adapter.async_preflight(
                coordinator.hass, context, request
            )
        except Exception:
            _LOGGER.exception(
                "Adaptive RoboVacs adapter preflight failed unexpectedly: robot=%s room=%s adapter=%s",
                robot.entity_id,
                room.name,
                robot.adapter_id,
            )
            await coordinator._async_latch_scheduler_fault(
                robot,
                room,
                "adapter_preflight_failed",
                "adapter_preflight",
                native_command_may_have_started=False,
                outcome_uncertain=False,
            )
            return False, fault_summary("adapter_preflight_failed")
        if preflight.blocked and candidate.get("operation") == "mop":
            await coordinator._async_handle_mop_preflight_blocked(
                robot, candidate, preflight.code, now
            )
            return True, f"skipped mopping {room.name}: water unavailable"
        if not preflight.ready:
            await coordinator._async_latch_scheduler_fault(
                robot,
                room,
                preflight.code,
                "adapter_preflight",
                native_command_may_have_started=False,
                outcome_uncertain=False,
            )
            return False, fault_summary(preflight.code)
        try:
            if coordinator._closing:
                return False, "coordinator shutting down"
            async with asyncio.timeout(SERVICE_CALL_TIMEOUT_SECONDS):
                await self.async_apply_profile(
                    robot, candidate["operation"], int(candidate["passes"])
                )
        except Exception:  # ServiceValidationError varies between HA versions.
            _LOGGER.exception(
                "Adaptive RoboVacs profile apply failed: robot=%s room=%s operation=%s adapter=%s",
                robot.entity_id,
                room.name,
                candidate["operation"],
                robot.adapter_id,
            )
            await coordinator._async_latch_scheduler_fault(
                robot,
                room,
                "profile_apply_failed",
                "profile_apply",
                native_command_may_have_started=False,
                outcome_uncertain=False,
            )
            return False, fault_summary("profile_apply_failed")

        coordinator.data["active"][robot.entity_id] = active
        await coordinator._async_save()

        native_attempt = int(candidate["passes"]) in (
            robot.adapter_capabilities.native_pass_counts_for(
                str(candidate["operation"])
            )
        )
        try:
            if coordinator._closing:
                coordinator.data["active"][robot.entity_id] = None
                await coordinator._async_save()
                return False, "coordinator shutting down"
            async with asyncio.timeout(SERVICE_CALL_TIMEOUT_SECONDS):
                result = await adapter.async_dispatch(
                    coordinator.hass, context, request
                )
        except Exception:  # Integration service exceptions vary by HA version.
            code = "native_dispatch_failed" if native_attempt else "generic_dispatch_failed"
            _LOGGER.exception(
                "Adaptive RoboVacs adapter dispatch failed: robot=%s room=%s operation=%s adapter=%s native=%s",
                robot.entity_id,
                room.name,
                candidate["operation"],
                robot.adapter_id,
                native_attempt,
            )
            await coordinator._async_latch_scheduler_fault(
                robot,
                room,
                code,
                "dispatch",
                native_command_may_have_started=native_attempt,
                outcome_uncertain=True,
            )
            return False, fault_summary(code)
        if not result.accepted:
            await coordinator._async_latch_scheduler_fault(
                robot,
                room,
                result.code,
                "dispatch",
                native_command_may_have_started=result.native_attempted,
                outcome_uncertain=result.outcome_uncertain,
            )
            return False, fault_summary(result.code)
        active["phase"] = "accepted"
        active["accepted_at"] = dt_util.utcnow().isoformat()
        occurrence = coordinator.data.get("occurrences", {}).get(room.area_id)
        stage_index = int(candidate.get("stage_index", 0))
        if occurrence and stage_index < len(occurrence.get("stages", [])):
            occurrence["stages"][stage_index]["status"] = "running"
            occurrence["stages"][stage_index]["started_at"] = active["accepted_at"]
        coordinator._room_data(room.area_id)["map_status"] = "mapped"
        coordinator._room_data(room.area_id)["map_error"] = None
        await coordinator._async_save()
        coordinator._schedule_start_confirmation(robot.entity_id)
        return True, f"dispatched {room.name}"
