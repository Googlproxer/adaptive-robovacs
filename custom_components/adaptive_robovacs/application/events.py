"""Home Assistant event adapters for the scheduler command queue."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from ..commands import (
    EvaluateCommand,
    ObservedManualCleanCommand,
    RefreshDiscoveryCommand,
    SchedulerCommand,
    SchedulerCommandResult,
    StateChangedCommand,
)
from ..const import DOMAIN
from ..discovery import DiscoveredRoom, DiscoverySnapshot
from ..metrics import RuntimeMetrics
from ..models import (
    CleaningOperation,
    EvaluationCause,
    EvaluationMode,
    JobPhase,
    JobSource,
    ManualCleanRequest,
    parse_manual_clean_request,
)
from ..state import ActiveJob, ManualAuditRecord, SchedulerState
from ..watch import WatchSpecification


def _now() -> datetime:
    from . import core

    return core._now()


class ApplicationEventsMixin:
    """Translate HA callbacks into typed FIFO commands and observations."""

    hass: HomeAssistant
    entry: ConfigEntry
    state: SchedulerState
    discovery: DiscoverySnapshot
    _lock: asyncio.Lock
    _watch_entity_ids: set[str]
    _watch_capability_entity_ids: set[str]
    _watch_specifications: dict[str, WatchSpecification]
    metrics: RuntimeMetrics

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
        self.metrics.state_events["received"] += 1
        specification = (
            self._watch_specifications.get(entity_id)
            if isinstance(entity_id, str)
            else None
        )
        if specification is None:
            self.metrics.state_events["ignored_unwatched"] += 1
            return
        assert isinstance(entity_id, str)
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        old_value = old_state.state if old_state else None
        new_value = new_state.state if new_state else None
        change = specification.classify(old_state, new_state)
        if not change.evaluate:
            self.metrics.state_events["ignored_unchanged"] += 1
            return
        self.metrics.state_events["meaningful"] += 1
        if change.refresh_discovery:
            self.metrics.state_events["capability_changes"] += 1
        self._async_create_task(
            self._async_handle_watched_state_change(
                StateChangedCommand(
                    entity_id=entity_id,
                    old_state=old_value,
                    new_state=new_value,
                    changed_at=(new_state.last_changed if new_state else None),
                ),
                capability_changed=change.refresh_discovery,
            )
        )

    async def _async_handle_watched_state_change(
        self,
        command: StateChangedCommand,
        *,
        capability_changed: bool,
    ) -> None:
        """Apply ordered transition effects, then request one settled evaluation."""

        await self.async_execute(command)
        if capability_changed:
            await self.async_execute(RefreshDiscoveryCommand("capability-options"))
        # Allow callbacks from the same HA event burst to enqueue their cheap
        # transition commands before all submitters share one evaluation.
        await asyncio.sleep(0)
        await self.async_execute(
            EvaluateCommand(
                mode=EvaluationMode.DISPATCH,
                cause=EvaluationCause.STATE_CHANGE,
                coalesce=True,
            )
        )

    @callback
    def _on_registry_updated(self, event: Event) -> None:
        """Refresh only registry mutations that can change discovery."""

        event_type = getattr(event, "event_type", dr.EVENT_DEVICE_REGISTRY_UPDATED)
        changes = set(event.data.get("changes", {}))
        action = event.data.get("action")
        reason = str(event_type).removesuffix("_registry_updated")
        if event_type == dr.EVENT_DEVICE_REGISTRY_UPDATED:
            if action == "update" and changes.isdisjoint(
                {"area_id", "identifiers", "labels", "name", "name_by_user"}
            ):
                return
        elif event_type == er.EVENT_ENTITY_REGISTRY_UPDATED:
            entity_id = event.data.get("entity_id") or event.data.get("old_entity_id")
            if not isinstance(entity_id, str):
                return
            if action == "update" and changes.isdisjoint(
                {
                    "area_id",
                    "device_id",
                    "disabled_by",
                    "entity_id",
                    "labels",
                    "name",
                    "original_name",
                    "platform",
                }
            ):
                return
            registry_entry = er.async_get(self.hass).async_get(entity_id)
            if registry_entry and registry_entry.platform == DOMAIN:
                return
            if entity_id.partition(".")[0] not in {
                "binary_sensor",
                "select",
                "sensor",
                "switch",
                "vacuum",
            }:
                return
        self._async_create_task(self.async_execute(RefreshDiscoveryCommand(reason)))

    @callback
    def _on_device_registry_updated(self, event: Event) -> None:
        """Backward-compatible test seam for device registry callbacks."""

        self._on_registry_updated(event)

    async def _async_refresh_discovery_after_device_label_change(self) -> None:
        """Immediately apply one topology or capability change."""

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
