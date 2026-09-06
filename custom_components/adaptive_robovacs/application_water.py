"""Water-confirmation orchestration for the scheduler application.

This component owns the Companion-notification transaction flow while the
concrete :class:`SchedulerApplication` remains the owner of the mutable state
aggregate and command queue.  Its host contract is declared explicitly so the
component cannot reach through a coordinator or infrastructure back-reference.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback

from .commands import (
    EvaluateCommand,
    ExpireWaterConfirmationCommand,
    SchedulerCommand,
    SchedulerCommandResult,
    WaterConfirmationResponseCommand,
)
from .models import EvaluationCause, EvaluationMode, StageStatus
from .notifications import NotificationService
from .repair_service import RepairService
from .state import SchedulerState, WaterConfirmation

_LOGGER = logging.getLogger(__name__)


def _application_now() -> datetime:
    """Use the application clock, including its deterministic test override."""

    from . import application

    return application._now()


def _track_deadline(
    hass: HomeAssistant,
    action: Callable[[datetime], None],
    deadline: datetime,
) -> Callable[[], None]:
    """Use the application's Home Assistant timer seam."""

    from . import application

    return application._track_point(hass, action, deadline)


class ApplicationWaterMixin:
    """Implement durable water approval and notification transactions."""

    hass: HomeAssistant
    entry: ConfigEntry
    state: SchedulerState
    notifications: NotificationService
    repairs: RepairService
    _lock: asyncio.Lock
    _water_confirmation_timers: dict[str, Callable[[], None]]

    if TYPE_CHECKING:

        def _async_create_task(
            self, coro: Coroutine[Any, Any, Any], *, name: str | None = None
        ) -> asyncio.Task[Any] | None: ...

        async def async_execute(
            self, command: SchedulerCommand
        ) -> SchedulerCommandResult: ...

        async def _async_save(self) -> None: ...

        def _skip_occurrence_stage(
            self,
            area_id: str,
            stage_index: int,
            outcome: StageStatus,
            reason: str,
            when: datetime,
        ) -> bool: ...

    def _notification_services(self) -> tuple[str, ...]:
        """Resolve current Companion notification targets without persisting them."""

        return self.notifications.targets()

    def has_notification_targets(self) -> bool:
        """Return whether at least one Companion target currently resolves."""

        return bool(self._notification_services())

    async def _async_send_mobile_notification(
        self, payload: dict[str, Any]
    ) -> tuple[int, int]:
        """Deliver to every current Companion target with aggregate diagnostics."""

        result = await self.notifications.async_send(payload)
        if result.failed:
            _LOGGER.warning(
                "Adaptive RoboVacs mobile notification delivery failed for "
                "%s of %s targets",
                result.failed,
                result.targets,
            )
        self.repairs.set_notification_delivery_issue(result.delivered == 0)
        return result.delivered, result.targets

    async def _async_clear_mobile_notification(self, tag: str) -> None:
        await self.notifications.async_clear(tag)

    @staticmethod
    def _action_hash(action: str) -> str:
        return hashlib.sha256(action.encode("utf-8")).hexdigest()

    def _schedule_water_confirmation(self, request: WaterConfirmation) -> None:
        request_id = request.request_id
        unsubscribe = self._water_confirmation_timers.pop(request_id, None)
        if unsubscribe:
            unsubscribe()

        @callback
        def expire(_timestamp: datetime) -> None:
            self._water_confirmation_timers.pop(request_id, None)
            self._async_create_task(
                self.async_execute(ExpireWaterConfirmationCommand(request_id))
            )

        self._water_confirmation_timers[request_id] = _track_deadline(
            self.hass, expire, request.expires_at
        )

    async def _async_restore_water_confirmations(self) -> None:
        now = _application_now()
        for request in tuple(self.state.water_confirmations.values()):
            if request.status not in {"pending", "confirmed"}:
                continue
            if request.expires_at <= now:
                await self._async_expire_water_confirmation(request.request_id)
            else:
                self._schedule_water_confirmation(request)

    @callback
    def _on_mobile_notification_action(self, event: Event) -> None:
        action = event.data.get("action")
        if isinstance(action, str):
            self._async_create_task(
                self.async_execute(WaterConfirmationResponseCommand(action=action))
            )

    @callback
    def _on_mobile_notification_cleared(self, event: Event) -> None:
        request_id = event.data.get("adaptive_robovacs_request_id")
        tag = event.data.get("tag")
        self._async_create_task(
            self.async_execute(
                WaterConfirmationResponseCommand(
                    request_id=str(request_id) if request_id else None,
                    tag=str(tag) if tag else None,
                    dismissed=True,
                )
            )
        )

    async def _async_expire_water_confirmation(self, request_id: str) -> None:
        clear_tag: str | None = None
        async with self._lock:
            request = next(
                (
                    item
                    for item in self.state.water_confirmations.values()
                    if item.request_id == request_id
                ),
                None,
            )
            if not request or request.status not in {"pending", "confirmed"}:
                return
            if request.status == "confirmed":
                active = next(
                    (
                        job
                        for job in self.state.active_jobs.values()
                        if job
                        and job.occurrence_id == request.occurrence_id
                        and job.stage_index == request.stage_index
                        and job.phase
                        in {
                            "accepted",
                            "cleaning",
                            "returning",
                            "completion_pending",
                        }
                    ),
                    None,
                )
                if active:
                    return
            request.status = "expired"
            request.responded_at = _application_now()
            clear_tag = request.tag
            self._skip_occurrence_stage(
                request.room_id,
                request.stage_index,
                StageStatus.SKIPPED_UNCONFIRMED_WATER,
                "water_confirmation_expired",
                _application_now(),
            )
            await self._async_save()
        if clear_tag:
            await self._async_clear_mobile_notification(clear_tag)
        self._async_create_task(
            self.async_execute(
                EvaluateCommand(
                    mode=EvaluationMode.DISPATCH,
                    cause=EvaluationCause.WATER_CONFIRMATION,
                    detail="water-confirmation-expired",
                )
            )
        )

    async def _async_handle_water_confirmation(
        self,
        *,
        action: str | None = None,
        request_id: str | None = None,
        tag: str | None = None,
        dismissed: bool = False,
    ) -> None:
        clear_tag: str | None = None
        result: str | None = None
        async with self._lock:
            now = _application_now()
            action_hash = self._action_hash(action) if action else None
            request = next(
                (
                    item
                    for item in self.state.water_confirmations.values()
                    if item.status == "pending"
                    and (
                        (
                            action_hash
                            and action_hash in {item.confirm_hash, item.cancel_hash}
                        )
                        or (request_id and item.request_id == request_id)
                        or (tag and item.tag == tag)
                    )
                ),
                None,
            )
            if not request:
                return
            expires = request.expires_at
            confirm = bool(
                action_hash
                and action_hash == request.confirm_hash
                and now < expires
                and not dismissed
            )
            request.status = (
                "confirmed"
                if confirm
                else ("expired" if now >= expires else "cancelled")
            )
            request.responded_at = now
            clear_tag = request.tag
            result = request.status
            if not confirm:
                timer = self._water_confirmation_timers.pop(
                    request.request_id,
                    None,
                )
                if timer:
                    timer()
            if not confirm:
                self._skip_occurrence_stage(
                    request.room_id,
                    request.stage_index,
                    StageStatus.SKIPPED_UNCONFIRMED_WATER,
                    "water_confirmation_cancelled",
                    now,
                )
            await self._async_save()
        if clear_tag:
            await self._async_clear_mobile_notification(clear_tag)
        self._async_create_task(
            self.async_execute(
                EvaluateCommand(
                    mode=EvaluationMode.DISPATCH,
                    cause=EvaluationCause.WATER_CONFIRMATION,
                    detail=f"water-confirmation-{result}",
                )
            )
        )
