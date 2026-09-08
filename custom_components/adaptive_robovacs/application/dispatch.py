"""Checkpointed physical dispatch and cleaning-occurrence transactions."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Coroutine
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ..commands import EvaluateCommand, SchedulerCommand, SchedulerCommandResult
from ..discovery import DiscoveredRobot, DiscoveredRoom, DiscoverySnapshot
from ..dispatch import DispatchPipeline
from ..jobs import can_refresh_pending_occurrence_profile
from ..models import (
    CleaningProgram,
    EvaluationCause,
    EvaluationMode,
    JobPhase,
    OccurrenceSource,
    StageStatus,
    scheduled_mop_revalidation_allowed,
)
from ..planner import ScheduleCandidate
from ..state import (
    ActiveJob,
    CleaningOccurrence,
    ManualAuditRecord,
    RoomHistory,
    SchedulerState,
    WaterConfirmation,
    WaterNotificationEpisode,
)

_LOGGER = logging.getLogger(__name__)


class ApplicationDispatchMixin:
    """Prepare stages and execute the durable checkpoint-before-start flow."""

    hass: HomeAssistant
    entry: ConfigEntry
    state: SchedulerState
    discovery: DiscoverySnapshot
    dispatch: DispatchPipeline
    _closing: bool

    if TYPE_CHECKING:

        def _async_create_task(
            self, coro: Coroutine[Any, Any, Any], *, name: str | None = None
        ) -> asyncio.Task[Any] | None: ...

        async def async_execute(
            self, command: SchedulerCommand
        ) -> SchedulerCommandResult: ...

        async def _async_save(self) -> None: ...

        def _notify_listeners(self) -> None: ...

        def _candidate_for_robot(
            self, candidate: ScheduleCandidate, robot: DiscoveredRobot
        ) -> ScheduleCandidate | None: ...

        def _room_data(self, area_id: str) -> RoomHistory: ...

        def _record_manual_event(self, event: ManualAuditRecord) -> None: ...

        async def _async_send_mobile_notification(
            self, payload: dict[str, Any]
        ) -> tuple[int, int]: ...

        def _action_hash(self, action: str) -> str: ...

        def _schedule_water_confirmation(self, request: WaterConfirmation) -> None: ...

        async def _async_latch_scheduler_fault(
            self,
            robot: DiscoveredRobot,
            room: DiscoveredRoom,
            reason_code: str,
            phase: str,
            *,
            native_command_may_have_started: bool,
            outcome_uncertain: bool,
        ) -> None: ...

        def _schedule_start_confirmation(self, robot_id: str) -> None: ...

    async def _async_refresh_pending_profile_if_needed(
        self,
        robot: DiscoveredRobot,
        candidate: ScheduleCandidate,
    ) -> ScheduleCandidate:
        """Refresh one invalid scheduler profile only while its robot is docked."""

        try:
            validation = await self.dispatch.async_validate_profile(robot, candidate)
        except Exception:
            # Dispatch performs and logs the definitive validation, including
            # its scheduler-fault handling.  Recovery must never hide that.
            return candidate
        if validation.ready or validation.code != "profile_option_unsupported":
            return candidate

        occurrence = candidate.occurrence
        if occurrence is None:
            return candidate
        stage_index = candidate.stage_index
        stages = occurrence.stages
        if stage_index >= len(stages):
            return candidate
        stage = stages[stage_index]
        robot_state = self.hass.states.get(robot.entity_id)
        if not can_refresh_pending_occurrence_profile(
            occurrence,
            stage,
            robot_state.state if robot_state else None,
            bool(self.state.active_jobs.get(robot.registry_id)),
        ):
            return candidate

        prior_profile = (
            stage.cleaning_profile,
            stage.requested_profile,
            stage.profile_sources,
        )
        stage.cleaning_profile = None
        stage.requested_profile = None
        stage.profile_sources = ()
        refreshed = self._candidate_for_robot(candidate, robot)
        if refreshed is None:
            (
                stage.cleaning_profile,
                stage.requested_profile,
                stage.profile_sources,
            ) = prior_profile
            return candidate

        stage.cleaning_profile = refreshed.resolved_profile
        stage.requested_profile = refreshed.requested_profile
        stage.profile_sources = refreshed.profile_sources
        await self._async_save()
        _LOGGER.info(
            "Adaptive RoboVacs refreshed an unsupported pending profile: "
            "robot=%s room=%s",
            robot.entity_id,
            candidate.room_id,
        )
        return refreshed

    def _skip_occurrence_stage(
        self,
        area_id: str,
        stage_index: int,
        outcome: StageStatus,
        reason: str,
        when: datetime,
    ) -> bool:
        """Make one current stage terminal and finish cadence when appropriate."""

        occurrence = self.state.occurrences.get(area_id)
        if not occurrence or occurrence.current_stage != stage_index:
            return False
        stages = occurrence.stages
        if stage_index >= len(stages) or stages[stage_index].status != "pending":
            return False
        stage = stages[stage_index]
        stage.status = outcome
        stage.reason = reason
        stage.completed_at = when
        occurrence.current_stage = stage_index + 1
        detail = self._room_data(area_id)
        detail.last_stage_outcome = outcome
        detail.last_stage_reason = reason
        detail.last_stage_at = when
        if stage.operation == "mop" and outcome == "skipped_no_water":
            vacuum_completed = any(
                item.operation == "vacuum" and item.status == "completed"
                for item in stages
            )
            detail.last_stage_summary = (
                "vacuum completed; mop skipped for water"
                if vacuum_completed
                else "mop skipped for water"
            )
        else:
            detail.last_stage_summary = f"{stage.operation} {outcome.replace('_', ' ')}"
        if occurrence.current_stage >= len(stages):
            if occurrence.source == "manual_dashboard":
                if any(item.status == "completed" for item in stages):
                    detail.cleaning_completed_at = when
                self._record_manual_event(
                    ManualAuditRecord(
                        at=when,
                        robot_registry_id=occurrence.robot_registry_id,
                        room_ids=(area_id,),
                        operations=tuple(item.operation for item in stages),
                        context_id=occurrence.manual_context_id,
                        mode=occurrence.manual_mode,
                        outcome=outcome,
                        reason=reason,
                        source="manual_dashboard",
                    )
                )
            else:
                detail.cleaning_completed_at = when
            self.state.occurrences.pop(area_id, None)
            self.state.water_confirmations.pop(occurrence.occurrence_id, None)
        return True

    async def _async_notify_mop_skipped(
        self,
        room: DiscoveredRoom,
        robot: DiscoveredRobot,
        reason: str,
        occurrence: CleaningOccurrence,
        now: datetime,
    ) -> None:
        episodes = self.state.water_notification_episodes
        episode = episodes.get(room.area_id)
        last_sent = episode.last_sent_at if episode else None
        if (
            episode
            and episode.reason == reason
            and last_sent
            and now - last_sent < timedelta(hours=24)
        ):
            return
        first_sent = (
            episode.first_sent_at if episode and episode.reason == reason else now
        )
        episodes[room.area_id] = WaterNotificationEpisode(
            room_id=room.area_id,
            reason=reason,
            first_sent_at=first_sent,
            last_sent_at=now,
        )
        vacuum_ran = any(
            stage.operation == "vacuum" and stage.status == "completed"
            for stage in occurrence.stages
        )
        vacuum_scheduled = any(
            stage.operation == "vacuum" for stage in occurrence.stages
        )
        vacuum_message = (
            "Vacuuming completed. "
            if vacuum_ran
            else "Vacuuming remains scheduled. "
            if vacuum_scheduled
            else "No vacuum stage was scheduled. "
        )
        await self._async_save()
        await self._async_send_mobile_notification(
            {
                "title": "Adaptive RoboVacs skipped mopping",
                "message": (
                    f"{robot.name} could not mop {room.name} because water was "
                    "not ready. "
                    + vacuum_message
                    + "Mopping will be tried at the next scheduled clean."
                ),
                "data": {
                    "channel": "Adaptive RoboVacs - Mop skipped",
                    "tag": (
                        "adaptive_robovacs_mop_skipped_"
                        f"{self.entry.entry_id}_{room.area_id}"
                    ),
                },
            }
        )

    async def _async_prepare_occurrence(
        self,
        robot: DiscoveredRobot,
        candidate: ScheduleCandidate,
        now: datetime,
    ) -> tuple[ScheduleCandidate | None, str | None]:
        """Persist a due occurrence and satisfy the current mop water gate."""

        room = self.discovery.rooms.get(candidate.room_id)
        if room is None:
            return None, "room is no longer discovered"
        occurrence = self.state.occurrences.get(room.area_id)
        if occurrence is None:
            occurrence_id = secrets.token_hex(12)
            occurrence = CleaningOccurrence(
                occurrence_id=occurrence_id,
                room_id=room.area_id,
                robot_registry_id=robot.registry_id,
                robot_entity_id=None,
                program=CleaningProgram(
                    candidate.program or CleaningProgram.VACUUM_ONLY
                ),
                stages=list(candidate.new_stages),
                scheduled_at=candidate.due_at,
                created_at=now,
                adapter_id=robot.adapter_id,
                adapter_schema_version=robot.adapter_schema_version,
                source=OccurrenceSource(candidate.source),
                manual_mode=candidate.manual_mode,
                manual_override=candidate.manual_override,
                bypass_desired_window=candidate.bypass_desired_window,
                manual_context_id=candidate.manual_context_id,
                manual_user_id=candidate.manual_user_id,
            )
            self.state.occurrences[room.area_id] = occurrence
            candidate = replace(
                candidate,
                occurrence=occurrence,
                occurrence_id=occurrence_id,
                stage_index=0,
            )
            await self._async_save()
        else:
            candidate = replace(
                candidate,
                occurrence=occurrence,
                occurrence_id=occurrence.occurrence_id,
                stage_index=occurrence.current_stage,
            )

        if candidate.operation != "mop":
            return candidate, None
        water = robot.adapter_capabilities.water_readiness
        if water.status == "sensor_ready" and water.ready:
            self.state.water_notification_episodes.pop(room.area_id, None)
            return candidate, None
        if scheduled_mop_revalidation_allowed(
            candidate.source,
            candidate.operation,
            water,
        ):
            return replace(candidate, ignore_water_readiness=True), None
        if water.status == "sensor_blocked":
            self._skip_occurrence_stage(
                room.area_id,
                candidate.stage_index,
                StageStatus.SKIPPED_NO_WATER,
                water.reason,
                now,
            )
            await self._async_save()
            await self._async_notify_mop_skipped(
                room,
                robot,
                water.reason,
                occurrence,
                now,
            )
            self._async_create_task(
                self.async_execute(
                    EvaluateCommand(
                        mode=EvaluationMode.DISPATCH,
                        cause=EvaluationCause.STAGE_TRANSITION,
                        detail="mop-stage-skipped-no-water",
                    )
                )
            )
            return None, f"skipped mopping {room.name}: water unavailable"
        if water.status != "confirmation_required":
            return None, "mopping is not supported"

        confirmations = self.state.water_confirmations
        request = confirmations.get(occurrence.occurrence_id)
        if request:
            expires = request.expires_at
            if request.status == "confirmed" and now < expires:
                return replace(candidate, water_confirmed=True), None
            if request.status in {"pending", "confirmed"} and now >= expires:
                request.status = "expired"
                request.responded_at = now
                self._skip_occurrence_stage(
                    room.area_id,
                    candidate.stage_index,
                    StageStatus.SKIPPED_UNCONFIRMED_WATER,
                    "water_confirmation_expired",
                    now,
                )
                await self._async_save()
                self._async_create_task(
                    self.async_execute(
                        EvaluateCommand(
                            mode=EvaluationMode.DISPATCH,
                            cause=EvaluationCause.WATER_CONFIRMATION,
                            detail="water-confirmation-expired",
                        )
                    )
                )
                return None, "mopping cancelled: water confirmation expired"
            return None, "waiting for water confirmation"

        request_id = secrets.token_hex(12)
        confirm_action = f"ARV_CONFIRM_WATER_{secrets.token_urlsafe(24)}"
        cancel_action = f"ARV_CANCEL_MOP_{secrets.token_urlsafe(24)}"
        expires = now + timedelta(hours=1)
        tag = f"adaptive_robovacs_mop_confirm_{self.entry.entry_id}_{request_id}"
        request = WaterConfirmation(
            request_id=request_id,
            occurrence_id=occurrence.occurrence_id,
            room_id=room.area_id,
            robot_registry_id=robot.registry_id,
            stage_index=candidate.stage_index,
            confirm_hash=self._action_hash(confirm_action),
            cancel_hash=self._action_hash(cancel_action),
            tag=tag,
            sent_at=now,
            expires_at=expires,
        )
        confirmations[occurrence.occurrence_id] = request
        await self._async_save()
        delivered, total = await self._async_send_mobile_notification(
            {
                "title": f"{robot.name} wants to mop",
                "message": (
                    f"{robot.name} wants to mop {room.name}, but cannot check "
                    "whether water "
                    "is onboard. Confirm water before mopping starts."
                ),
                "data": {
                    "channel": "Adaptive RoboVacs - Mop confirmation",
                    "tag": tag,
                    "timeout": 3600,
                    "adaptive_robovacs_request_id": request_id,
                    "actions": [
                        {
                            "action": confirm_action,
                            "title": "Confirm water",
                            "authenticationRequired": True,
                        },
                        {
                            "action": cancel_action,
                            "title": "Cancel mopping",
                            "authenticationRequired": True,
                        },
                    ],
                },
            }
        )
        if delivered == 0:
            request.status = "cancelled"
            request.responded_at = now
            self._skip_occurrence_stage(
                room.area_id,
                candidate.stage_index,
                StageStatus.SKIPPED_UNCONFIRMED_WATER,
                "water_confirmation_delivery_failed",
                now,
            )
            await self._async_save()
            self._async_create_task(
                self.async_execute(
                    EvaluateCommand(
                        mode=EvaluationMode.DISPATCH,
                        cause=EvaluationCause.WATER_CONFIRMATION,
                        detail="water-confirmation-unreachable",
                    )
                )
            )
            return None, "mopping cancelled: no notification target"
        if delivered < total:
            _LOGGER.warning(
                "Adaptive RoboVacs water confirmation reached %s of %s "
                "notification targets",
                delivered,
                total,
            )
        self._schedule_water_confirmation(request)
        return None, "waiting for water confirmation"

    async def _async_handle_mop_preflight_blocked(
        self,
        robot: DiscoveredRobot,
        candidate: ScheduleCandidate,
        reason: str,
        now: datetime,
    ) -> None:
        """Treat a just-in-time water block as a normal terminal mop skip."""

        room = self.discovery.rooms.get(candidate.room_id)
        occurrence = self.state.occurrences.get(candidate.room_id)
        if room is None:
            return
        if not occurrence:
            return
        self._skip_occurrence_stage(
            room.area_id,
            candidate.stage_index,
            StageStatus.SKIPPED_NO_WATER,
            reason,
            now,
        )
        await self._async_save()
        await self._async_notify_mop_skipped(
            room,
            robot,
            reason,
            occurrence,
            now,
        )
        self._async_create_task(
            self.async_execute(
                EvaluateCommand(
                    mode=EvaluationMode.DISPATCH,
                    cause=EvaluationCause.STAGE_TRANSITION,
                    detail="mop-final-preflight-skipped",
                )
            )
        )

    async def _async_handle_mop_mode_unconfirmed(
        self,
        robot: DiscoveredRobot,
        candidate: ScheduleCandidate,
        reason: str,
        now: datetime,
    ) -> None:
        """Safely skip one mop stage whose no-vacuum profile is unavailable."""

        room = self.discovery.rooms.get(candidate.room_id)
        occurrence = self.state.occurrences.get(candidate.room_id)
        if room is None or occurrence is None:
            return
        self._skip_occurrence_stage(
            room.area_id,
            candidate.stage_index,
            StageStatus.SKIPPED_NO_MOP,
            reason,
            now,
        )
        await self._async_save()
        _LOGGER.warning(
            "Adaptive RoboVacs skipped mopping because its no-vacuum profile "
            "was unavailable: robot=%s room=%s",
            robot.entity_id,
            room.name,
        )
        self._async_create_task(
            self.async_execute(
                EvaluateCommand(
                    mode=EvaluationMode.DISPATCH,
                    cause=EvaluationCause.STAGE_TRANSITION,
                    detail="mop-only-mode-unconfirmed",
                )
            )
        )

    async def _async_latch_dispatch_fault(
        self,
        robot: DiscoveredRobot,
        room: DiscoveredRoom,
        reason_code: str,
        phase: str,
        native_command_may_have_started: bool,
        outcome_uncertain: bool,
    ) -> None:
        """Adapt the dispatch port to the application-owned fault aggregate."""

        await self._async_latch_scheduler_fault(
            robot,
            room,
            reason_code,
            phase,
            native_command_may_have_started=native_command_may_have_started,
            outcome_uncertain=outcome_uncertain,
        )

    async def _async_checkpoint_dispatch(
        self,
        robot: DiscoveredRobot,
        active: ActiveJob,
    ) -> None:
        """Persist and publish a job checkpoint before any outbound start."""

        self.state.active_jobs[robot.registry_id] = active
        await self._async_save()
        self._notify_listeners()

    async def _async_abandon_dispatch_checkpoint(
        self,
        robot: DiscoveredRobot,
    ) -> None:
        """Clear an unstarted checkpoint after shutdown or a fresh safety block."""

        self.state.active_jobs[robot.registry_id] = None
        await self._async_save()
        self._notify_listeners()

    async def _async_accept_dispatch(
        self,
        robot: DiscoveredRobot,
        room: DiscoveredRoom,
        candidate: ScheduleCandidate,
        active: ActiveJob,
        accepted_at: datetime,
    ) -> None:
        """Commit the accepted stage and schedule confirmation observation."""

        active.phase = JobPhase.ACCEPTED
        active.accepted_at = accepted_at
        if active.source == "manual_dashboard":
            self._record_manual_event(
                ManualAuditRecord(
                    at=accepted_at,
                    robot_registry_id=robot.registry_id,
                    room_ids=(room.area_id,),
                    operations=(candidate.operation,),
                    context_id=candidate.manual_context_id,
                    mode=candidate.manual_mode,
                    outcome="started",
                    source="manual_dashboard",
                )
            )
        occurrence = self.state.occurrences.get(room.area_id)
        stage_index = candidate.stage_index
        if occurrence and stage_index < len(occurrence.stages):
            occurrence.stages[stage_index].status = StageStatus.RUNNING
            occurrence.stages[stage_index].started_at = accepted_at
        history = self._room_data(room.area_id)
        history.map_status = "mapped"
        history.map_error = None
        await self._async_save()
        self._schedule_start_confirmation(robot.entity_id)
        self._notify_listeners()

    async def _async_dispatch(
        self,
        robot: DiscoveredRobot,
        candidate: ScheduleCandidate,
        now: datetime,
    ) -> tuple[bool, str]:
        if self._closing:
            return False, "coordinator shutting down"
        return await self.dispatch.async_dispatch(robot, candidate, now)
