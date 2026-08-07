"""Pure scheduler models and decisions.

This module deliberately has no Home Assistant imports so the safety-critical
occupancy and due-date behaviour can be tested without a running instance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Mapping


VALID_OCCUPANCY_STATES = {"on", "off"}


@dataclass(frozen=True, slots=True)
class OccupancyResolution:
    """The resolved occupancy for one Home Assistant area."""

    state: str
    source: str
    unavailable_radars: int = 0


@dataclass(frozen=True, slots=True)
class Forecast:
    """Safety result for a potential cleaning start."""

    allowed: bool
    confidence: float
    reason: str


@dataclass(frozen=True, slots=True)
class Candidate:
    """A ready room-cleaning candidate."""

    room_id: str
    robot_entity_id: str
    operation: str
    due_at: datetime
    confidence: float
    reason: str


def resolve_occupancy(
    radar_states: Iterable[str | None], fallback_states: Iterable[str | None]
) -> OccupancyResolution:
    """Resolve occupancy with radars preferred over legacy motion sources.

    All available radars must be clear to establish vacancy. If a radar is
    unavailable, a complete clear fallback set can establish vacancy instead.
    Rooms with no sources are intentionally eligible when due.
    """

    radars = list(radar_states)
    fallbacks = list(fallback_states)
    unavailable = sum(state not in VALID_OCCUPANCY_STATES for state in radars)

    if not radars and not fallbacks:
        return OccupancyResolution("unoccupied", "no_sensor")
    if "on" in radars:
        return OccupancyResolution("occupied", "radars", unavailable)
    if radars and unavailable == 0:
        return OccupancyResolution("unoccupied", "radars")

    if "on" in fallbacks:
        return OccupancyResolution("occupied", "motion_fallback", unavailable)
    if fallbacks and all(state == "off" for state in fallbacks):
        return OccupancyResolution("unoccupied", "motion_fallback", unavailable)
    return OccupancyResolution("unresolved", "unavailable", unavailable)


def due_at(
    last_completed: datetime | None,
    interval_hours: float,
    deferred_until: datetime | None,
    now: datetime,
) -> datetime:
    """Return the due time while retaining the legacy one-day deferral rule."""

    baseline = now if last_completed is None else last_completed + timedelta(hours=interval_hours)
    return max(baseline, deferred_until) if deferred_until else baseline


def forecast_vacancy(
    samples: Iterable[Mapping[str, object]],
    now: datetime,
    clear_since: datetime | None,
    required_minutes: int,
    confidence_percent: float,
    minimum_samples: int,
) -> Forecast:
    """Return whether the current clear period is safe for a new clean."""

    if clear_since is None:
        return Forecast(False, 0.0, "clear period has not started")

    comparable: list[Mapping[str, object]] = []
    weekend = now.weekday() >= 5
    bucket = now.hour // 2
    for sample in samples:
        started = sample.get("start")
        if not isinstance(started, datetime):
            continue
        if (started.weekday() >= 5) == weekend and started.hour // 2 == bucket:
            comparable.append(sample)

    clear_minutes = (now - clear_since).total_seconds() / 60
    if len(comparable) < minimum_samples:
        return Forecast(
            clear_minutes >= required_minutes,
            0.0,
            f"waiting for {required_minutes} clear minutes ({len(comparable)} comparable samples)",
        )

    successes = sum(float(sample.get("minutes", 0)) >= required_minutes for sample in comparable)
    confidence = successes / len(comparable)
    return Forecast(
        confidence >= confidence_percent / 100,
        confidence,
        f"{successes}/{len(comparable)} comparable vacancies",
    )


def manual_deferral(now: datetime, next_due: datetime) -> datetime | None:
    """Delay a known manual clean only if the next scheduled job is within 24h."""

    if now <= next_due <= now + timedelta(hours=24):
        return now + timedelta(days=1)
    return None


def in_daytime_window(now: datetime, start: str, end: str) -> bool:
    """Return whether a local time is in a half-open daytime range."""

    time_text = now.strftime("%H:%M")
    return start <= time_text < end
