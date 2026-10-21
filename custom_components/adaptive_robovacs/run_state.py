"""Durable cleaning-run records; no Home Assistant or scheduler I/O."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from .models import ResolvedCleaningProfile


def profile_key(profile: ResolvedCleaningProfile) -> str:
    """Use exact operation/profile values, independent of room and entity IDs."""

    return repr(tuple(sorted(profile.to_mapping().items())))


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("run identity must be a nonempty string")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("run measurement must be numeric")
    if not math.isfinite(value) or value < 0:
        raise ValueError("run measurement must be finite and nonnegative")
    return float(value)


def _timestamp(value: object) -> datetime:
    parsed = datetime.fromisoformat(_text(value))
    if parsed.tzinfo is None:
        raise ValueError("run timestamp must have a timezone")
    return parsed


@dataclass(frozen=True, slots=True)
class BatteryUsageSample:
    profile: str
    percent_per_minute: float
    recorded_at: datetime

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> BatteryUsageSample:
        rate = _number(value.get("percent_per_minute"))
        if rate <= 0:
            raise ValueError("battery consumption must be positive")
        return cls(_text(value.get("profile")), rate, _timestamp(value.get("at")))

    def to_store(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "percent_per_minute": self.percent_per_minute,
            "at": self.recorded_at.isoformat(),
        }


@dataclass(slots=True)
class CleaningRun:
    run_id: str
    created_at: datetime
    rooms: list[str]
    occurrences: dict[str, str]
    started_room_id: str | None = None
    expected_minutes: float = 0
    battery_percent: float = 0
    removed: dict[str, str] = field(default_factory=dict)
    started: bool = False
    estimated_end_at: datetime | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CleaningRun:
        raw_rooms = value.get("rooms")
        raw_occurrences = value.get("occurrences")
        raw_removed = value.get("removed")
        if not isinstance(raw_rooms, list) or not raw_rooms:
            raise ValueError("cleaning run must contain rooms")
        rooms = [_text(item) for item in raw_rooms]
        if len(set(rooms)) != len(rooms):
            raise ValueError("cleaning run contains duplicate rooms")
        if not isinstance(raw_occurrences, Mapping) or not isinstance(
            raw_removed, Mapping
        ):
            raise ValueError("cleaning run references must be objects")
        occurrences = {_text(key): _text(item) for key, item in raw_occurrences.items()}
        if set(occurrences) != set(rooms):
            raise ValueError("cleaning run occurrence references do not match rooms")
        started = value.get("started_room_id")
        if started is not None and started != rooms[0]:
            raise ValueError("only the current room may have started")
        if not isinstance(value.get("started", False), bool):
            raise ValueError("run started must be a boolean")
        if started is not None and value.get("started") is not True:
            raise ValueError("current room must belong to a started run")
        return cls(
            _text(value.get("run_id")),
            _timestamp(value.get("created_at")),
            rooms,
            occurrences,
            _text(started) if started is not None else None,
            _number(value.get("expected_minutes", 0)),
            _number(value.get("battery_percent", 0)),
            {_text(key): _text(item) for key, item in raw_removed.items()},
            value.get("started") is True,
            _timestamp(value["estimated_end_at"])
            if value.get("estimated_end_at") is not None
            else None,
        )

    def to_store(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "created_at": self.created_at.isoformat(),
            "rooms": list(self.rooms),
            "occurrences": dict(self.occurrences),
            "started_room_id": self.started_room_id,
            "expected_minutes": self.expected_minutes,
            "battery_percent": self.battery_percent,
            "removed": dict(self.removed),
            "started": self.started,
            "estimated_end_at": self.estimated_end_at.isoformat()
            if self.estimated_end_at
            else None,
        }

    def remove(self, room_id: str, reason: str) -> None:
        self.rooms.remove(room_id)
        self.occurrences.pop(room_id)
        if self.started_room_id == room_id:
            self.started_room_id = None
        self.removed[room_id] = reason
