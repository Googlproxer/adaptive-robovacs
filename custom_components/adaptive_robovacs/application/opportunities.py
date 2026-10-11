"""Missed-window accounting, bounded notifications, and scheduler deadlines."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry

from ..discovery import DiscoveredRoom, DiscoverySnapshot
from ..lifecycle import SchedulerRuntime
from ..models import (
    ResolvedDailyWindow,
    effective_scheduled_due,
    next_daily_window_boundary,
)
from ..notifications import NotificationService
from ..opportunities import (
    advance_opportunity,
    blocker_category,
    missed_cadences,
    opportunity_advice,
)
from ..planner import RoomEvaluation
from ..state import RoomOpportunity, RoomSettings, SchedulerState


class ApplicationOpportunityMixin:
    """Account only observed time and deliver deduplicated informational alerts."""

    entry: ConfigEntry
    state: SchedulerState
    discovery: DiscoverySnapshot
    lifecycle: SchedulerRuntime
    notifications: NotificationService
    _room_evaluations: dict[str, RoomEvaluation]
    _last_opportunity_observation: datetime | None

    if TYPE_CHECKING:

        @property
        def observe_only(self) -> bool: ...
        @property
        def party_mode(self) -> bool: ...
        @property
        def storage_safe_mode(self) -> bool: ...
        def _room_settings(self, room: DiscoveredRoom) -> RoomSettings: ...
        def _desired_window(self, room: DiscoveredRoom) -> ResolvedDailyWindow: ...
        def _desired_window_allows(
            self, room: DiscoveredRoom, now: datetime
        ) -> bool: ...
        def _room_due(
            self, room: DiscoveredRoom, operation: str, now: datetime
        ) -> datetime: ...
        def _room_deferral(
            self, room: DiscoveredRoom, operation: str
        ) -> datetime | None: ...
        def _active_rooms(self, active: object) -> list[str]: ...
        def _startup_state_settle_reason(self, now: datetime) -> str | None: ...
        def _shutdown_started(self) -> bool: ...
        async def _async_save(self, *, force: bool = False) -> None: ...

    async def _async_update_opportunities(
        self, now: datetime, *, dry_run: bool
    ) -> None:
        from .core import _local

        previous = getattr(self, "_last_opportunity_observation", None)
        continuous = previous is not None and timedelta(
            0
        ) <= now - previous <= timedelta(minutes=20)
        self._last_opportunity_observation = now
        paused = (
            self.observe_only
            or self.party_mode
            or self.storage_safe_mode
            or bool(self._startup_state_settle_reason(now))
            or self._shutdown_started()
        )
        deadlines: list[datetime] = []
        messages: list[dict[str, object]] = []
        clears: list[str] = []
        for room_id, room in self.discovery.rooms.items():
            settings = self._room_settings(room)
            history = self.state.room_history[room_id]
            record = history.opportunity
            occurrence = self.state.occurrences.get(room_id)
            manual = bool(occurrence and occurrence.source == "manual_dashboard")
            due = effective_scheduled_due(
                self._room_due(room, "cleaning", now),
                occurrence.scheduled_at if occurrence else None,
                self._room_deferral(room, "cleaning"),
                manual_request=manual,
            )
            tag = f"adaptive_robovacs_{self.entry.entry_id}_overdue_{room_id}"
            active = any(
                job and room_id in self._active_rooms(job)
                for job in self.state.active_jobs.values()
            )
            if due is None or due > now or not settings.enabled:
                if record.alert_signature and not dry_run:
                    clears.append(tag)
                history.opportunity = RoomOpportunity(
                    alert_signature=record.alert_signature if dry_run else None,
                    alert_at=record.alert_at if dry_run else None,
                )
                if settings.enabled and due and due > now:
                    deadlines.append(due)
                continue
            window = self._desired_window(room)
            bypass = settings.ignore_desired_window or bool(
                occurrence and occurrence.bypass_desired_window
            )
            in_window = bypass or self._desired_window_allows(room, now)
            boundary = (
                (
                    _local(now).replace(hour=0, minute=0, second=0, microsecond=0)
                    + timedelta(days=1)
                ).astimezone(UTC)
                if bypass
                else next_daily_window_boundary(_local(now), window.start, window.end)
            )
            if boundary:
                deadlines.append(boundary)
            decision = self._room_evaluations.get(room_id)
            confirmation = (
                self.state.water_confirmations.get(occurrence.occurrence_id)
                if occurrence
                else None
            )
            waiting_water = bool(confirmation and confirmation.status == "pending")
            category = (
                "paused"
                if paused
                else "in_progress"
                if active
                else "outside_hours"
                if not in_window
                else "water"
                if waiting_water
                else blocker_category(decision.reason if decision else "not evaluated")
            )
            old_signature, old_alert_at = record.alert_signature, record.alert_at
            record = advance_opportunity(
                record,
                now=now,
                due=due,
                reason=category,
                in_window=in_window,
                window_end=boundary if in_window else None,
                continuous=continuous,
            )
            episode_prefix = f"{due.astimezone(UTC).isoformat()}|"
            if old_signature and not old_signature.startswith(episode_prefix):
                if dry_run:
                    record = replace(
                        record, alert_signature=old_signature, alert_at=old_alert_at
                    )
                else:
                    clears.append(tag)
                    record = replace(record, alert_signature=None, alert_at=None)
            repeated_mop = bool(occurrence and occurrence.mop_delay_expirations >= 2)
            missed = missed_cadences(now, due, settings.cleaning_interval)
            if (
                not paused
                and not active
                and not manual
                and not dry_run
                and not (decision and decision.selected_robot_id and not waiting_water)
                and record.observed_window
                and (missed >= 3 or repeated_mop)
            ):
                cause, advice = opportunity_advice(record, repeated_mop=repeated_mop)
                signature = f"{episode_prefix}{cause}"
                changed = signature != record.alert_signature
                throttled = (
                    record.alert_at is not None
                    and now < record.alert_at + timedelta(hours=24)
                )
                if changed and (not throttled or cause == "mop_follow_up"):
                    record = replace(record, alert_signature=signature, alert_at=now)
                    messages.append(
                        {
                            "title": f"{room.name}: cleaning overdue",
                            "message": f"{missed} cadence deadlines missed. {advice}",
                            "data": {"tag": tag},
                        }
                    )
            history.opportunity = record
            threshold = due + timedelta(hours=2 * settings.cleaning_interval)
            if threshold > now:
                deadlines.append(threshold)
            if decision and not paused and not active and in_window:
                for robot_decision in decision.robots:
                    evidence = robot_decision.evidence
                    forecasts = [evidence.vacancy_diagnostic] if evidence else []
                    for forecast in forecasts:
                        if (
                            forecast
                            and not forecast.allowed
                            and forecast.fallback_at
                            and forecast.fallback_at > now
                        ):
                            deadlines.append(forecast.fallback_at)
                    if robot_decision.adjacency:
                        deadlines.extend(
                            item.vacancy.fallback_at
                            for item in robot_decision.adjacency.blockers
                            if item.vacancy
                            and item.vacancy.fallback_at
                            and item.vacancy.fallback_at > now
                        )
        self.lifecycle.update_opportunity_deadline(min(deadlines, default=None))
        # Persist episode identity before delivery so restart cannot duplicate it.
        if messages or clears:
            await self._async_save(force=True)
        else:
            await self._async_save()
        for tag in clears:
            await self.notifications.async_clear(tag)
        for payload in messages:
            await self.notifications.async_send(payload)
