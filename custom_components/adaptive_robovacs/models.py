"""Pure scheduler models and decisions.

This module deliberately has no Home Assistant imports so the safety-critical
occupancy and due-date behaviour can be tested without a running instance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import math
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


@dataclass(frozen=True, slots=True)
class RoomObservation:
    """Home Assistant observations needed for a room scheduling decision."""

    occupancy: str
    source: str
    unavailable_radars: int = 0


@dataclass(frozen=True, slots=True)
class RobotObservation:
    """Home Assistant observations needed to decide whether a robot is ready."""

    state: str | None
    battery: float | None
    cleaning_timer_minutes: float | None = None


@dataclass(frozen=True, slots=True)
class RobotReadiness:
    """A displayable readiness decision for a discovered robot."""

    ready: bool
    reason: str


@dataclass(frozen=True, slots=True)
class RoomCandidate:
    """A pure room candidate before it is assigned to a robot."""

    room_id: str
    operation: str
    due_at: datetime
    confidence: float
    reason: str
    duration_minutes: float
    duration_sample_count: int
    passes: int


@dataclass(frozen=True, slots=True)
class Assignment:
    """A pure robot-to-room assignment produced by a scheduling pass."""

    robot_id: str
    candidate: RoomCandidate


@dataclass(frozen=True, slots=True)
class SchedulePlan:
    """The safe, side-effect-free result of evaluating the house."""

    candidates: tuple[RoomCandidate, ...]
    assignments: tuple[Assignment, ...]
    blocks: Mapping[str, str]
    readiness: Mapping[str, RobotReadiness]


@dataclass(frozen=True, slots=True)
class ManualCleanRequest:
    """A room-targeted clean explicitly initiated by a Home Assistant user."""

    robot_id: str
    area_ids: list[str]


def _service_entity_ids(service_data: Mapping[str, object]) -> list[str]:
    """Return the explicitly targeted entities from a service call."""

    target = service_data.get("target")
    target_data = target if isinstance(target, Mapping) else {}
    value = service_data.get("entity_id", target_data.get("entity_id"))
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [item for item in value if isinstance(item, str)]
    return []


def parse_manual_clean_request(
    domain: str,
    service: str,
    user_id: str | None,
    service_data: Mapping[str, object],
    managed_robot_ids: Iterable[str],
    managed_area_ids: Iterable[str],
) -> ManualCleanRequest | None:
    """Return only an unambiguous, user-initiated HA room-clean request.

    Native-app starts do not produce a Home Assistant call-service event with a
    user context, and whole-home ``vacuum.start`` calls never identify a room.
    Both deliberately remain outside scheduler tracking.
    """

    if domain != "vacuum" or service != "clean_area" or not user_id:
        return None

    raw_area_ids = service_data.get("cleaning_area_id")

    def identifiers(value: object) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple, set)):
            return [item for item in value if isinstance(item, str)]
        return []

    robot_ids = _service_entity_ids(service_data)
    area_ids = list(dict.fromkeys(identifiers(raw_area_ids)))
    managed_robots = set(managed_robot_ids)
    managed_areas = set(managed_area_ids)
    if len(robot_ids) != 1 or robot_ids[0] not in managed_robots:
        return None
    if not area_ids or any(area_id not in managed_areas for area_id in area_ids):
        return None
    return ManualCleanRequest(robot_ids[0], area_ids)


def held_job_transition(
    robot_state: str | None,
    phase: str | None,
    completed_before_hold: bool,
) -> str:
    """Classify only the safe ways an interrupted job can leave its hold.

    A docked or idle robot is not enough to infer user intent because some
    native integrations report it shortly after an error. A live ``returning``
    state is the physical-dock signal; a fresh ``cleaning`` state is a physical
    resume. A job that had already entered returning before its fault has a
    confirmed clean phase and can complete once it reaches the dock.
    """

    if robot_state == "cleaning":
        return "resumed"
    if robot_state == "returning":
        return "completion_pending" if completed_before_hold else "cancelling"
    if phase == "completion_pending" and robot_state in {"docked", "idle"}:
        return "complete"
    if phase == "cancelling" and robot_state in {"docked", "idle"}:
        return "cancelled"
    return "held"


def offline_held_recovery_outcome(
    robot_state: str | None,
    hold_phase: str | None,
    last_observed_at: datetime | None,
    expected_minutes: float | None,
    recovered_at: datetime,
) -> str:
    """Classify an unobserved held-job ending after Home Assistant restarts."""

    if robot_state not in {"docked", "idle"}:
        return "held"
    if hold_phase == "cancelling":
        return "cancelled"
    if (
        last_observed_at
        and expected_minutes
        and expected_minutes > 0
        and recovered_at - last_observed_at >= timedelta(minutes=expected_minutes)
    ):
        return "complete"
    if last_observed_at and expected_minutes and expected_minutes > 0:
        return "cancelled"
    return "held"


def rebase_due_times(
    due_times: Mapping[str, datetime], cooldown_until: datetime
) -> dict[str, datetime]:
    """Move a due queue past a cooldown while retaining its natural spacing."""

    if not due_times:
        return {}
    earliest = min(due_times.values())
    return {
        key: max(due_at, cooldown_until + (due_at - earliest))
        for key, due_at in due_times.items()
    }


def recovery_transition_is_observed(
    old_state: str | None,
    new_state: str | None,
    transition_at: datetime | None,
    recovered_at: datetime | None,
) -> bool:
    """Return whether a live post-restart transition proves a completion.

    Home Assistant can learn a robot's *current* state while starting without
    knowing when that state changed.  That snapshot is not enough to replace a
    stored expected end time.  A transition delivered after recovery, however,
    is a contemporaneous observation and is therefore the authoritative end of
    the cleaning phase.
    """

    return bool(
        transition_at
        and recovered_at
        and transition_at >= recovered_at
        and old_state in {"cleaning", "returning"}
        and new_state in {"returning", "docked", "idle"}
    )


def resolve_occupancy(
    radar_states: Iterable[str | None], fallback_states: Iterable[str | None]
) -> OccupancyResolution:
    """Resolve occupancy with radars preferred over fallback motion sources.

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
    """Return the due time while retaining the established one-day deferral rule."""

    baseline = now if last_completed is None else last_completed + timedelta(hours=interval_hours)
    return max(baseline, deferred_until) if deferred_until else baseline


def format_time_until(due_at: datetime, now: datetime) -> str:
    """Return a concise remaining-time label using its largest whole unit."""

    remaining_minutes = max(0, math.ceil((due_at - now).total_seconds() / 60))
    if remaining_minutes >= 24 * 60:
        days = remaining_minutes // (24 * 60)
        return f"in {days} day" if days == 1 else f"in {days} days"
    if remaining_minutes >= 60:
        hours = remaining_minutes // 60
        return f"in {hours} hour" if hours == 1 else f"in {hours} hours"
    return f"in {remaining_minutes} minute" if remaining_minutes == 1 else f"in {remaining_minutes} minutes"


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
            f"waiting for {required_minutes} clear minutes",
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


def learned_duration_minutes(samples: Iterable[float], fallback: float, minimum: int = 3) -> tuple[float, int]:
    """Return a conservative learned duration without letting outliers dominate.

    The configured duration remains the prior until enough direct observations
    exist.  Thereafter use an upper percentile so vacancy prediction is safe
    rather than optimistic.
    """

    values = sorted(value for value in samples if 0 < value <= 240)
    if len(values) < minimum:
        return fallback, len(values)
    median = values[len(values) // 2]
    deviations = sorted(abs(value - median) for value in values)
    mad = deviations[len(deviations) // 2]
    tolerance = max(2.0, mad * 3)
    values = [value for value in values if abs(value - median) <= tolerance]
    if len(values) < minimum:
        return fallback, len(values)
    index = min(len(values) - 1, max(0, int(len(values) * 0.8 + 0.999999) - 1))
    return values[index], len(values)


def in_daytime_window(now: datetime, start: str, end: str) -> bool:
    """Return whether a local time is in a configured half-open time range.

    The scheduler uses the same helper for the daytime bedroom-transit policy
    and the overnight unresolved-occupancy policy.  Supporting windows that
    cross midnight avoids treating a valid night range as empty.
    """

    if start == end:
        return False
    time_text = now.strftime("%H:%M")
    if start < end:
        return start <= time_text < end
    return time_text >= start or time_text < end


def next_window_start(now: datetime, start: str) -> datetime:
    """Return the next occurrence of a local HH:MM window start."""

    hour, minute = (int(part) for part in start.split(":", maxsplit=1))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate if now < candidate else candidate + timedelta(days=1)


def desired_window_allows(
    ignore_desired_window: bool, now: datetime, start: str, end: str
) -> bool:
    """Return whether a room may start within the preferred cleaning window."""

    return ignore_desired_window or in_daytime_window(now, start, end)


def unresolved_occupancy_allowed(
    occupancy: str,
    is_bedroom_transit: bool,
    now: datetime,
    start: str,
    end: str,
) -> bool:
    """Allow only ordinary unresolved rooms in the desired cleaning window."""

    return (
        occupancy == "unresolved"
        and not is_bedroom_transit
        and in_daytime_window(now, start, end)
    )


def select_operation(
    vacuum_due: datetime,
    mop_due: datetime | None,
    can_mop: bool,
    carpet: bool,
    now: datetime,
) -> tuple[str, datetime]:
    """Choose a safe operation, never selecting mopping for carpeted rooms."""

    if not carpet and can_mop and mop_due is not None and mop_due <= now:
        if vacuum_due <= now:
            return "vac_and_mop", min(vacuum_due, mop_due)
        return "mop", mop_due
    return "vacuum", vacuum_due
