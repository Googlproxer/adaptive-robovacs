"""Home Assistant event adapters for the scheduler command queue."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback

from .commands import (
    EvaluateCommand,
    ObservedManualCleanCommand,
    RefreshDiscoveryCommand,
    SchedulerCommand,
    SchedulerCommandResult,
    StateChangedCommand,
)
from .discovery import DiscoveredRoom, DiscoverySnapshot
from .models import (
    CleaningOperation,
    EvaluationCause,
    EvaluationMode,
    JobPhase,
    JobSource,
    ManualCleanRequest,
    parse_manual_clean_request,
)
from .state import ActiveJob, ManualAuditRecord, SchedulerState


def _now() -> datetime:
    from . import application

    return application._now()


class ApplicationEventsMixin:
    """Translate HA callbacks into typed FIFO commands and observations."""

    hass: HomeAssistant
    entry: ConfigEntry
    state: SchedulerState
    discovery: DiscoverySnapshot
    _lock: asyncio.Lock
    _watch_entity_ids: set[str]

    if TYPE_CHECKING:

        def _async_create_task(
            self, coro: Coroutine[Any, Any, Any], *, name: str | None = None
        ) -> asyncio.Task[Any] | None: ...

        async def async_execute(
            self, command: SchedulerCommand
        ) -> SchedulerCommandResult: ...

        async def _async_save(self) -> None: ...

        async def async_refresh_discovery(self, *, notify: bool = True) -> None: ...

        def _notify_listeners(self) -> None: ...

        def _record_manual_event(self, event: ManualAuditRecord) -> None: ...

        def _effective_duration(
            self,
            room: DiscoveredRoom,
            operation: str,
            passes: int,
            robot_id: str | None = None,
        ) -> tuple[float, int]: ...

        def robot_registry_id(self, entity_id: str) -> str: ...

    @callback
    def _on_home_assistant_started(self, _event: Event) -> None:
        self._async_create_task(
            self.async_execute(
                EvaluateCommand(
                    mode=EvaluationMode.PREVIEW,
                    cause=EvaluationCause.HOME_ASSISTANT_STARTED,
                    coalesce=True,
                )
            )
        )

    @callback
    def _on_call_service(self, event: Event) -> None:
        """Capture explicit user room-clean service calls."""

        service_data = event.data.get("service_data")
        if not isinstance(service_data, Mapping):
            return
        request = parse_manual_clean_request(
            str(event.data.get("domain")),
            str(event.data.get("service")),
            event.context.user_id,
            service_data,
            self.discovery.robots,
            self.discovery.rooms,
        )
        if request is None:
            return
        self._async_create_task(
            self.async_execute(ObservedManualCleanCommand(request, event.context.id))
        )

    async def _async_record_observed_manual_clean(
        self,
        request: ManualCleanRequest,
        context_id: str,
    ) -> None:
        """Persist a manual checkpoint before the vacuum service begins work."""

        async with self._lock:
            robot = self.discovery.robots.get(request.robot_id)
            rooms = [self.discovery.rooms.get(area_id) for area_id in request.area_ids]
            if robot is None or any(room is None for room in rooms):
                return
            registry_id = robot.registry_id
            existing = self.state.active_jobs.get(registry_id)
            if existing:
                reason = (
                    "scheduler job already active"
                    if existing.source == "scheduler"
                    else "manual job already active"
                )
                self._record_manual_event(
                    ManualAuditRecord(
                        at=_now(),
                        robot_registry_id=robot.registry_id,
                        room_ids=tuple(request.area_ids),
                        context_id=context_id,
                        outcome="ignored",
                        reason=reason,
                    )
                )
                await self._async_save()
                return

            expected_minutes = sum(
                self._effective_duration(
                    room,
                    "vacuum",
                    1,
                    self.robot_registry_id(request.robot_id),
                )[0]
                for room in rooms
                if room is not None
            )
            now = _now()
            self.state.active_jobs[registry_id] = ActiveJob(
                room_id=request.area_ids[0],
                room_ids=list(request.area_ids),
                operation=CleaningOperation.VACUUM,
                requested_operations=[CleaningOperation.VACUUM],
                started_at=now,
                seen_cleaning=False,
                phase=JobPhase.MANUAL_REQUESTED,
                source=JobSource.MANUAL_HOME_ASSISTANT,
                manual_context_id=context_id,
                expected_minutes=expected_minutes,
                expected_end=now + timedelta(minutes=expected_minutes),
                last_observed_at=now,
                passes=1,
            )
            self._record_manual_event(
                ManualAuditRecord(
                    at=now,
                    robot_registry_id=robot.registry_id,
                    room_ids=tuple(request.area_ids),
                    operations=(CleaningOperation.VACUUM,),
                    context_id=context_id,
                    outcome="requested",
                )
            )
            await self._async_save()
            self._notify_listeners()
            self._async_create_task(
                self.async_execute(
                    EvaluateCommand(
                        mode=EvaluationMode.PREVIEW,
                        cause=EvaluationCause.MANUAL_REQUEST,
                        detail=f"manual-ha:{request.robot_id}",
                    )
                )
            )

    @callback
    def _on_state_changed(self, event: Event) -> None:
        entity_id = event.data.get("entity_id")
        if entity_id in self._watch_entity_ids:
            old_state = event.data.get("old_state")
            new_state = event.data.get("new_state")
            self._async_create_task(
                self.async_execute(
                    StateChangedCommand(
                        entity_id=entity_id,
                        old_state=old_state.state if old_state else None,
                        new_state=new_state.state if new_state else None,
                        changed_at=(new_state.last_changed if new_state else None),
                    )
                )
            )

    @callback
    def _on_device_registry_updated(self, event: Event) -> None:
        """Refresh occupancy sources when a device's labels change."""

        if event.data.get("action") != "update":
            return
        if "labels" not in event.data.get("changes", {}):
            return
        self._async_create_task(
            self.async_execute(RefreshDiscoveryCommand("device-labels"))
        )

    async def _async_refresh_discovery_after_device_label_change(self) -> None:
        """Immediately apply an occupancy device-label change."""

        async with self._lock:
            await self.async_refresh_discovery()

    async def _async_interval(self, _now_value: datetime) -> None:
        await self.async_execute(
            EvaluateCommand(
                mode=EvaluationMode.DISPATCH,
                cause=EvaluationCause.INTERVAL,
                coalesce=True,
            )
        )
