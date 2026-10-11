"""Pure accounting for observed, missed cleaning opportunities."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import isfinite

from .state import OpportunityWindow, RoomOpportunity


def blocker_category(reason: str) -> str:
    """Map internal policy reasons to stable, bounded accounting categories."""
    reason = reason.lower()
    for terms, category in (
        (("adjacen", "neighbour", "neighbor"), "adjacency"),
        (("vacanc", "clear minutes", "forecast"), "forecast"),
        (("occupancy",), "occupancy"),
        (("water",), "water"),
        (("repair", "recovery"), "recovery"),
        (("robot", "dock", "battery", "hold", "cooldown"), "robot"),
        (("desired cleaning window",), "outside_hours"),
        (("assigned", "eligible", "ready"), "eligible"),
    ):
        if any(term in reason for term in terms):
            return category
    return "other"


def missed_cadences(now: datetime, due: datetime, cadence_hours: float) -> int:
    if not isfinite(cadence_hours) or cadence_hours <= 0:
        return 0
    elapsed = (now.astimezone(UTC) - due.astimezone(UTC)).total_seconds()
    return 0 if elapsed < 0 else 1 + int(elapsed // (cadence_hours * 3600))


def advance_opportunity(
    record: RoomOpportunity,
    *,
    now: datetime,
    due: datetime,
    reason: str,
    in_window: bool,
    window_end: datetime | None,
    continuous: bool,
) -> RoomOpportunity:
    """Attribute each observed interval once; never infer evidence across downtime.

    Calls within a minute with an unchanged cause are coalesced. Window closing
    is a durable event even if every rejection in that window was identical.
    """
    now, due = now.astimezone(UTC), due.astimezone(UTC)
    if record.due_at != due:
        record = RoomOpportunity(due_at=due)
    if (
        continuous
        and record.accounted_at is not None
        and timedelta(0) <= now - record.accounted_at < timedelta(minutes=1)
        and reason == record.reason
        and in_window == record.in_window
        and window_end == record.window_end
    ):
        return record
    totals, window_totals = dict(record.totals), dict(record.window_totals)
    windows = record.windows
    window_start = record.window_started_at
    observed = record.observed_window
    if record.accounted_at is not None and now > record.accounted_at:
        # A missing interval is never attributed to its stale last-known blocker.
        end = min(now, record.window_end) if record.window_end else now
        elapsed = max(0.0, (end - record.accounted_at).total_seconds() / 60)
        category = record.reason if continuous else "unknown"
        totals[category] = min(1e9, totals.get(category, 0) + elapsed)
        if record.in_window:
            window_totals[category] = min(1e9, window_totals.get(category, 0) + elapsed)
            if continuous and category not in {"paused", "unknown"} and elapsed > 0:
                observed = True
        if end < now:
            gap = (now - end).total_seconds() / 60
            totals["unknown"] = min(1e9, totals.get("unknown", 0) + gap)
    closed = record.in_window and (
        not in_window
        or record.window_end != window_end
        or (record.window_end is not None and now >= record.window_end)
    )
    if closed and window_start is not None:
        end = min(now, record.window_end) if record.window_end else now
        windows = (
            *windows,
            OpportunityWindow(window_start, end, tuple(sorted(window_totals.items()))),
        )[-14:]
        window_start, window_totals = None, {}
    if in_window and window_start is None:
        window_start = now
    return replace(
        record,
        accounted_at=now,
        reason=reason,
        in_window=in_window,
        window_started_at=window_start,
        window_end=window_end,
        totals=tuple(sorted(totals.items())),
        window_totals=tuple(sorted(window_totals.items())),
        windows=windows,
        observed_window=observed,
    )


def opportunity_advice(
    record: RoomOpportunity, *, repeated_mop: bool = False
) -> tuple[str, str]:
    """Return the dominant observed cause and an actionable suggestion."""
    if repeated_mop:
        return (
            "mop_follow_up",
            "Mop repeatedly missed its follow-up window. "
            "Review permitted hours and mop readiness.",
        )
    blocked = [
        (minutes, cause)
        for cause, minutes in record.totals
        if cause
        not in {"eligible", "in_progress", "paused", "outside_hours", "unknown"}
    ]
    cause = max(blocked, default=(0, "other"))[1]
    suggestions = {
        "forecast": "Review the vacancy evidence and stable-vacancy fallback settings.",
        "occupancy": (
            "Inspect the occupancy source and choose hours when the room "
            "is normally vacant."
        ),
        "adjacency": (
            "Review the named neighbour blockers and the overlap between "
            "cleaning and adjacency-protection hours."
        ),
        "robot": "Check robot readiness, availability, and any recorded hold.",
        "water": "Check mop water readiness or the pending water confirmation.",
        "recovery": "Review the room's recovery or Repair before it can resume.",
        "other": (
            "Review the room's latest scheduling decision and permitted cleaning hours."
        ),
    }
    return cause, suggestions[cause]
