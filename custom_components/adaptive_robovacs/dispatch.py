"""Checkpointed dispatch pipeline for Adaptive RoboVacs."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from homeassistant.util import dt as dt_util

from .discovery import DiscoveredRobot, DiscoveredRoom
from .gateway import VacuumGateway
from .models import (
    AdapterCleaningProfile,
    AdapterDispatchRequest,
    AdapterDispatchResult,
    CleaningOperation,
    JobPhase,
    JobSource,
)
from .planner import ScheduleCandidate
from .repairs_manager import fault_summary
from .state import ActiveJob

_LOGGER = logging.getLogger(__name__)
SERVICE_CALL_TIMEOUT_SECONDS = 35
SAFE_MOP_PROFILE_BLOCK_CODES = frozenset(
    {
        "mop_only_mode_unconfirmed",
        "native_mop_profile_invalid",
        "native_mop_profile_control_unavailable",
        "native_mop_profile_unconfirmed",
        "native_mop_profile_apply_failed",
        # Retain these legacy values during a rolling upgrade: a stage saved
        # before v1.7.1 must still be handled as a safe mop-only skip.
        "direct_custom_mop_profile_invalid",
        "direct_custom_mop_control_unavailable",
        "direct_custom_mop_unconfirmed",
    }
)


@dataclass(frozen=True, slots=True)
class DispatchDependencies:
    """Explicit application callbacks required by the dispatch transaction."""

    room_for_id: Callable[[str], DiscoveredRoom | None]
    is_closing: Callable[[], bool]
    async_latch_fault: Callable[
        [DiscoveredRobot, DiscoveredRoom, str, str, bool, bool],
        Awaitable[None],
    ]
    async_handle_mop_preflight: Callable[
        [DiscoveredRobot, ScheduleCandidate, str, datetime],
        Awaitable[None],
    ]
    async_handle_mop_mode: Callable[
        [DiscoveredRobot, ScheduleCandidate, str, datetime],
        Awaitable[None],
    ]
    async_downgrade_max_plus: Callable[
        [DiscoveredRobot, DiscoveredRoom, ScheduleCandidate],
        Awaitable[None],
    ]
    async_checkpoint: Callable[[DiscoveredRobot, ActiveJob], Awaitable[None]]
    async_abandon_checkpoint: Callable[[DiscoveredRobot], Awaitable[None]]
    async_accept: Callable[
        [DiscoveredRobot, DiscoveredRoom, ScheduleCandidate, ActiveJob, datetime],
        Awaitable[None],
    ]


class DispatchPipeline:
    """Orchestrate one safe, checkpointed vendor-adapter dispatch."""

    def __init__(
        self,
        gateway: VacuumGateway,
        dependencies: DispatchDependencies,
    ) -> None:
        self._gateway = gateway
        self._dependencies = dependencies

    async def async_apply_profile(
        self,
        robot: DiscoveredRobot,
        operation: str,
        passes: int,
        cleaning_profile: AdapterCleaningProfile | None = None,
    ) -> AdapterDispatchResult:
        """Delegate exact profile application to the selected adapter."""

        profile = robot.profile
        settings = cleaning_profile or AdapterCleaningProfile()
        if operation == "mop":
            if not profile.mop_mode_select_entity_id:
                settings = replace(settings, mop_mode=None)
            if not profile.mop_intensity_select_entity_id:
                settings = replace(settings, mop_intensity=None)
        else:
            settings = replace(settings, mop_mode=None, mop_intensity=None)
        request = AdapterDispatchRequest(
            robot_entity_id=robot.entity_id,
            area_ids=(),
            operation=CleaningOperation(operation),
            passes=passes,
            cleaning_profile=settings,
        )
        return await self._gateway.async_apply_profile(robot, request)

    def _request(
        self,
        robot: DiscoveredRobot,
        candidate: ScheduleCandidate,
    ) -> AdapterDispatchRequest:
        settings = AdapterCleaningProfile.from_resolved(
            candidate.resolved_profile,
            water_confirmed=candidate.water_confirmed,
            ignore_water_readiness=candidate.ignore_water_readiness,
        )
        return AdapterDispatchRequest(
            robot_entity_id=robot.entity_id,
            area_ids=(candidate.room_id,),
            operation=CleaningOperation(candidate.operation),
            passes=candidate.passes,
            cleaning_profile=settings,
        )

    async def async_preflight(
        self,
        robot: DiscoveredRobot,
        candidate: ScheduleCandidate,
    ) -> AdapterDispatchResult:
        """Recheck adapter prerequisites without changing vacuum state."""

        return await self._gateway.async_preflight(
            robot,
            self._request(robot, candidate),
        )

    def profile_is_ready(
        self,
        robot: DiscoveredRobot,
        operation: str,
        passes: int,
        cleaning_profile: AdapterCleaningProfile | None = None,
    ) -> bool:
        """Validate configured profile controls without calling a service."""

        return bool(
            self._gateway.profile_is_ready(
                robot,
                operation,
                passes,
                cleaning_profile or AdapterCleaningProfile(),
            )
        )

    async def async_validate_profile(
        self,
        robot: DiscoveredRobot,
        candidate: ScheduleCandidate,
    ) -> AdapterDispatchResult:
        """Validate one candidate profile without changing the vacuum."""

        return await self._gateway.async_validate_profile(
            robot,
            self._request(robot, candidate),
        )

    async def async_dispatch(
        self,
        robot: DiscoveredRobot,
        candidate: ScheduleCandidate,
        now: datetime,
    ) -> tuple[bool, str]:
        """Checkpoint dispatch and engage the global fault latch on failure."""

        dependencies = self._dependencies
        room = dependencies.room_for_id(candidate.room_id)
        if room is None:
            return False, "room is no longer discovered"
        if dependencies.is_closing():
            return False, "coordinator shutting down"
        active = ActiveJob(
            room_id=room.area_id,
            room_ids=[room.area_id],
            operation=candidate.operation,
            started_at=now,
            seen_cleaning=False,
            phase=JobPhase.DISPATCHING,
            source=JobSource(candidate.source),
            expected_minutes=candidate.duration_minutes,
            expected_end=now + timedelta(minutes=candidate.duration_minutes),
            last_observed_at=now,
            forecast_sample_eligible=True,
            recovery_crossed=False,
            interruption_minutes=0,
            passes=candidate.passes,
            adapter_id=robot.adapter_id,
            adapter_schema_version=robot.adapter_schema_version,
            occurrence_id=candidate.occurrence_id,
            stage_index=candidate.stage_index,
            cleaning_profile=candidate.resolved_profile,
            requested_profile=candidate.requested_profile,
            profile_sources=candidate.profile_sources,
            manual_mode=candidate.manual_mode,
            manual_context_id=candidate.manual_context_id,
            q10_max_plus_fallback=bool(
                robot.adapter_capabilities.cleaning_depth_options
                and candidate.operation == "vacuum"
                and candidate.resolved_profile is not None
                and candidate.resolved_profile.fan_speed == "max_plus"
            ),
        )

        request = self._request(robot, candidate)
        try:
            if dependencies.is_closing():
                return False, "coordinator shutting down"
            preflight = await self._gateway.async_preflight(robot, request)
        except Exception:
            _LOGGER.exception(
                "Adaptive RoboVacs adapter preflight failed unexpectedly: "
                "robot=%s room=%s adapter=%s",
                robot.entity_id,
                room.name,
                robot.adapter_id,
            )
            await dependencies.async_latch_fault(
                robot,
                room,
                "adapter_preflight_failed",
                "adapter_preflight",
                False,
                False,
            )
            return False, fault_summary("adapter_preflight_failed")
        if preflight.blocked and candidate.operation == "mop":
            await dependencies.async_handle_mop_preflight(
                robot, candidate, preflight.code, now
            )
            return True, f"skipped mopping {room.name}: water unavailable"
        if not preflight.ready:
            await dependencies.async_latch_fault(
                robot,
                room,
                preflight.code,
                "adapter_preflight",
                False,
                False,
            )
            return False, fault_summary(preflight.code)
        try:
            profile_preflight = await self._gateway.async_validate_profile(
                robot, request
            )
        except Exception:
            _LOGGER.exception(
                "Adaptive RoboVacs profile validation failed unexpectedly: "
                "robot=%s room=%s adapter=%s",
                robot.entity_id,
                room.name,
                robot.adapter_id,
            )
            await dependencies.async_latch_fault(
                robot,
                room,
                "profile_validation_failed",
                "profile_preflight",
                False,
                False,
            )
            return False, fault_summary("profile_validation_failed")
        if not profile_preflight.ready:
            if (
                profile_preflight.blocked
                and candidate.operation == "mop"
                and profile_preflight.code in SAFE_MOP_PROFILE_BLOCK_CODES
            ):
                await dependencies.async_handle_mop_mode(
                    robot, candidate, profile_preflight.code, now
                )
                return True, f"skipped mopping {room.name}: mop profile unavailable"
            await dependencies.async_latch_fault(
                robot,
                room,
                profile_preflight.code,
                "profile_preflight",
                False,
                False,
            )
            return False, fault_summary(profile_preflight.code)
        try:
            if dependencies.is_closing():
                return False, "coordinator shutting down"
            async with asyncio.timeout(SERVICE_CALL_TIMEOUT_SECONDS):
                profile_apply = await self._gateway.async_apply_profile(robot, request)
        except Exception:  # ServiceValidationError varies between HA versions.
            _LOGGER.exception(
                "Adaptive RoboVacs profile apply failed: robot=%s room=%s "
                "operation=%s adapter=%s",
                robot.entity_id,
                room.name,
                candidate.operation,
                robot.adapter_id,
            )
            await dependencies.async_latch_fault(
                robot,
                room,
                "profile_apply_failed",
                "profile_apply",
                False,
                False,
            )
            return False, fault_summary("profile_apply_failed")

        if not profile_apply.ready:
            if (
                profile_apply.blocked
                and candidate.operation == "mop"
                and profile_apply.code in SAFE_MOP_PROFILE_BLOCK_CODES
            ):
                _LOGGER.warning(
                    "Adaptive RoboVacs skipped an unconfirmed mop profile: "
                    "robot=%s room=%s adapter=%s",
                    robot.entity_id,
                    room.name,
                    robot.adapter_id,
                )
                await dependencies.async_handle_mop_mode(
                    robot, candidate, profile_apply.code, now
                )
                return True, f"skipped mopping {room.name}: mop profile unavailable"
            await dependencies.async_latch_fault(
                robot,
                room,
                profile_apply.code,
                "profile_apply",
                False,
                False,
            )
            return False, fault_summary(profile_apply.code)

        if dependencies.is_closing():
            return False, "coordinator shutting down"

        await dependencies.async_checkpoint(robot, active)

        native_attempt = candidate.passes in (
            robot.adapter_capabilities.native_pass_counts_for(candidate.operation)
        )
        try:
            if dependencies.is_closing():
                await dependencies.async_abandon_checkpoint(robot)
                return False, "coordinator shutting down"
            async with asyncio.timeout(SERVICE_CALL_TIMEOUT_SECONDS):
                result = await self._gateway.async_dispatch(robot, request)
        except Exception:  # Integration service exceptions vary by HA version.
            code = (
                "native_dispatch_failed"
                if native_attempt
                else "generic_dispatch_failed"
            )
            _LOGGER.exception(
                "Adaptive RoboVacs adapter dispatch failed: robot=%s room=%s "
                "operation=%s adapter=%s native=%s",
                robot.entity_id,
                room.name,
                candidate.operation,
                robot.adapter_id,
                native_attempt,
            )
            await dependencies.async_latch_fault(
                robot,
                room,
                code,
                "dispatch",
                native_attempt,
                True,
            )
            return False, fault_summary(code)
        if not result.accepted:
            if result.code in {
                "q10_max_plus_profile_write_failed",
                "q10_max_plus_start_failed",
            }:
                await dependencies.async_downgrade_max_plus(
                    robot,
                    room,
                    candidate,
                )
            await dependencies.async_latch_fault(
                robot,
                room,
                result.code,
                "dispatch",
                result.native_attempted,
                result.outcome_uncertain,
            )
            return False, fault_summary(result.code)
        accepted_at = dt_util.utcnow()
        await dependencies.async_accept(
            robot,
            room,
            candidate,
            active,
            accepted_at,
        )
        return True, f"dispatched {room.name}"
