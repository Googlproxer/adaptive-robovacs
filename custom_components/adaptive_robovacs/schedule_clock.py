"""Presentation-only window transitions; no scheduler evaluation or persistence."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .models import next_clean_schedule
from .snapshots import IntegrationSnapshot, RoomView


def advance_room_window(room: RoomView, now: datetime) -> RoomView:
    """Advance only a reached window closing, retaining all settled diagnostics."""

    if room.next_clean_window_end_at is None or now < room.next_clean_window_end_at:
        return room
    timestamp, boundary = next_clean_schedule(
        room.schedule_due_at,
        dt_util.as_local(now),
        room.desired_window_effective_start,
        room.desired_window_effective_end,
    )
    return replace(room, next_clean_at=timestamp, next_clean_window_end_at=boundary)


class SchedulePresentationClock:
    """Maintain one cancellable clock for the immutable published snapshot."""

    def __init__(
        self,
        hass: HomeAssistant,
        publish: Callable[[IntegrationSnapshot], None],
    ) -> None:
        self._hass = hass
        self._publish = publish
        self._snapshot: IntegrationSnapshot | None = None
        self._cancel: Callable[[], None] | None = None

    @callback
    def stop(self) -> None:
        """Release the timer on shutdown or before replacing a snapshot."""

        if self._cancel:
            self._cancel()
            self._cancel = None
        self._snapshot = None

    @callback
    def update(self, snapshot: IntegrationSnapshot) -> None:
        """Schedule only the next real closing, never a countdown tick."""

        self.stop()
        self._snapshot = snapshot
        boundaries = [
            room.next_clean_window_end_at
            for room in snapshot.rooms
            if room.next_clean_window_end_at is not None
        ]
        if boundaries:
            self._cancel = async_track_point_in_utc_time(
                self._hass, self._advance, min(boundaries)
            )

    @callback
    def _advance(self, now: datetime) -> None:
        if self._snapshot is None:
            return
        snapshot = replace(
            self._snapshot,
            rooms=tuple(
                advance_room_window(room, now) for room in self._snapshot.rooms
            ),
        )
        self.update(snapshot)
        self._publish(snapshot)
