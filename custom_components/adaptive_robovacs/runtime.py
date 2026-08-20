"""Home Assistant service boundary for Adaptive RoboVacs."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from .adapters.base import AdapterMatchContext
from .adapters.registry import adapter_for_id
from .discovery import DiscoveredRobot, DiscoveredRoom
from .models import AdapterDispatchRequest, AdapterDispatchResult
from .repairs_manager import fault_summary

if TYPE_CHECKING:
    from .coordinator import AdaptiveRoboVacCoordinator


_LOGGER = logging.getLogger(__name__)
SERVICE_CALL_TIMEOUT_SECONDS = 35


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
            can_mutate=lambda: not self._coordinator._closing,
        )

    async def async_apply_profile(
        self,
        robot: DiscoveredRobot,
        operation: str,
        passes: int,
        cleaning_profile: dict[str, object] | None = None,
    ) -> AdapterDispatchResult:
        """Delegate exact profile application to the selected adapter."""

        profile = robot.profile
        settings = dict(cleaning_profile or {})
        if operation == "mop":
            if not profile.mop_mode_select_entity_id:
                settings["mop_mode"] = None
            if not profile.mop_intensity_select_entity_id:
                settings["mop_intensity"] = None
        else:
            settings["mop_mode"] = None
            settings["mop_intensity"] = None
        request = AdapterDispatchRequest(
            robot_entity_id=robot.entity_id,
            area_ids=(),
            operation=operation,
            passes=passes,
            cleaning_profile=settings,
        )
        return await adapter_for_id(robot.adapter_id).async_apply_profile(
            self._coordinator.hass, self._adapter_context(robot), request
        )

    def _request(
        self, robot: DiscoveredRobot, candidate: dict[str, Any]
    ) -> AdapterDispatchRequest:
        settings = dict(candidate.get("resolved_profile") or {})
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
        self,
        robot: DiscoveredRobot,
        operation: str,
        passes: int,
        cleaning_profile: dict[str, object] | None = None,
    ) -> bool:
        """Validate configured profile controls without calling a service."""

        settings = cleaning_profile or {}
        selections = [
            (robot.profile.mode_select_entity_id, settings.get("mode")),
            (robot.profile.mop_mode_select_entity_id, settings.get("mop_mode")),
            (
                robot.profile.mop_intensity_select_entity_id,
                settings.get("mop_intensity"),
            ),
        ]
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

    async def async_validate_profile(
        self, robot: DiscoveredRobot, candidate: dict[str, Any]
    ) -> AdapterDispatchResult:
        """Validate one candidate profile without changing the vacuum."""

        adapter = adapter_for_id(robot.adapter_id)
        return await adapter.async_validate_profile(
            self._coordinator.hass,
            self._adapter_context(robot),
            self._request(robot, candidate),
        )

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
            "source": candidate.get("source", "scheduler"),
            "expected_minutes": candidate["duration_minutes"],
            "expected_end": (now + timedelta(minutes=candidate["duration_minutes"])).isoformat(),
            "last_observed_at": now.isoformat(),
            "passes": candidate["passes"],
            "adapter_id": robot.adapter_id,
            "adapter_schema_version": robot.adapter_schema_version,
            "occurrence_id": candidate.get("occurrence_id"),
            "stage_index": candidate.get("stage_index"),
            "cleaning_profile": dict(candidate.get("resolved_profile") or {}),
            "requested_profile": dict(candidate.get("requested_profile") or {}),
            "profile_sources": dict(candidate.get("profile_sources") or {}),
            "manual_mode": candidate.get("manual_mode"),
            "manual_context_id": candidate.get("manual_context_id"),
            "q10_max_plus_fallback": bool(
                robot.adapter_capabilities.cleaning_depth_options
                and candidate.get("operation") == "vacuum"
                and (candidate.get("resolved_profile") or {}).get("fan_speed")
                == "max_plus"
            ),
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
            profile_preflight = await adapter.async_validate_profile(
                coordinator.hass, context, request
            )
        except Exception:
            _LOGGER.exception(
                "Adaptive RoboVacs profile validation failed unexpectedly: robot=%s room=%s adapter=%s",
                robot.entity_id,
                room.name,
                robot.adapter_id,
            )
            await coordinator._async_latch_scheduler_fault(
                robot,
                room,
                "profile_validation_failed",
                "profile_preflight",
                native_command_may_have_started=False,
                outcome_uncertain=False,
            )
            return False, fault_summary("profile_validation_failed")
        if not profile_preflight.ready:
            await coordinator._async_latch_scheduler_fault(
                robot,
                room,
                profile_preflight.code,
                "profile_preflight",
                native_command_may_have_started=False,
                outcome_uncertain=False,
            )
            return False, fault_summary(profile_preflight.code)
        try:
            if coordinator._closing:
                return False, "coordinator shutting down"
            async with asyncio.timeout(SERVICE_CALL_TIMEOUT_SECONDS):
                profile_apply = await adapter.async_apply_profile(
                    coordinator.hass, context, request
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

        if not profile_apply.ready:
            if (
                profile_apply.blocked
                and candidate.get("operation") == "mop"
                and profile_apply.code == "mop_only_mode_unconfirmed"
            ):
                _LOGGER.warning(
                    "Adaptive RoboVacs skipped an unconfirmed mop-only stage: robot=%s room=%s adapter=%s",
                    robot.entity_id,
                    room.name,
                    robot.adapter_id,
                )
                await coordinator._async_handle_mop_mode_unconfirmed(
                    robot, candidate, profile_apply.code, now
                )
                return True, f"skipped mopping {room.name}: mop-only mode unavailable"
            await coordinator._async_latch_scheduler_fault(
                robot,
                room,
                profile_apply.code,
                "profile_apply",
                native_command_may_have_started=False,
                outcome_uncertain=False,
            )
            return False, fault_summary(profile_apply.code)

        if coordinator._closing:
            return False, "coordinator shutting down"

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
            if result.code in {
                "q10_max_plus_profile_write_failed",
                "q10_max_plus_start_failed",
            }:
                await coordinator._async_downgrade_q10_max_plus(robot, room, candidate)
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
        if active.get("source") == "manual_dashboard":
            coordinator.jobs.record_manual_event(
                {
                    "at": active["accepted_at"],
                    "robot": robot.entity_id,
                    "rooms": [room.area_id],
                    "operations": [candidate["operation"]],
                    "context_id": candidate.get("manual_context_id"),
                    "mode": candidate.get("manual_mode"),
                    "outcome": "started",
                    "source": "manual_dashboard",
                }
            )
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
