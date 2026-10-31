"""Strict, restart-safe robot water records; no Home Assistant dependencies."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .models import DockWaterStatus


def _time(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("water timestamp must be an ISO string")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("water timestamp must have a timezone")
    return result


@dataclass(slots=True)
class DockWaterVerification:
    attempt_key: str
    occurrence_ids: tuple[str, ...]
    requested_at: datetime
    deadline: datetime
    phase: str = "requested"
    reason: str | None = None

    @property
    def pending(self) -> bool:
        return self.phase in {"requested", "washing", "settling"}

    def to_store(self) -> dict[str, object]:
        return {
            "attempt_key": self.attempt_key,
            "occurrence_ids": list(self.occurrence_ids),
            "requested_at": self.requested_at.isoformat(),
            "deadline": self.deadline.isoformat(),
            "phase": self.phase,
            "reason": self.reason,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> DockWaterVerification:
        key = value.get("attempt_key")
        refs = value.get("occurrence_ids")
        phase = value.get("phase")
        requested, deadline = (
            _time(value.get("requested_at")),
            _time(value.get("deadline")),
        )
        reason = value.get("reason")
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(refs, list)
            or not refs
            or any(not isinstance(item, str) or not item for item in refs)
            or phase not in {"requested", "washing", "settling", "succeeded", "failed"}
            or requested is None
            or deadline is None
            or deadline < requested
            or (reason is not None and not isinstance(reason, str))
        ):
            raise ValueError("invalid dock water verification")
        return cls(
            key,
            tuple(str(item) for item in refs),
            requested,
            deadline,
            str(phase),
            reason,
        )


@dataclass(slots=True)
class RobotWaterRecord:
    last_snapshot_at: datetime | None = None
    status: DockWaterStatus = field(default_factory=DockWaterStatus)
    notified_faults: tuple[str, ...] = ()
    verification: DockWaterVerification | None = None

    def to_store(self) -> dict[str, object]:
        return {
            "last_snapshot_at": self.last_snapshot_at.isoformat()
            if self.last_snapshot_at
            else None,
            "status": asdict(self.status),
            "notified_faults": list(self.notified_faults),
            "verification": self.verification.to_store() if self.verification else None,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> RobotWaterRecord:
        status = value.get("status")
        faults = value.get("notified_faults")
        verification = value.get("verification")
        if (
            not isinstance(status, Mapping)
            or not isinstance(faults, list)
            or any(item not in {"clean", "dirty", "onboard"} for item in faults)
            or len(set(faults)) != len(faults)
            or (verification is not None and not isinstance(verification, Mapping))
        ):
            raise ValueError("invalid robot water record")
        values: dict[str, str | None] = {}
        for key in ("clean", "dirty", "onboard", "error"):
            item = status.get(key)
            if item is not None and not isinstance(item, str):
                raise ValueError("invalid reported water condition")
            if key != "error" and item not in {None, "on", "off", "unknown"}:
                raise ValueError("invalid reported water flag")
            values[key] = item
        return cls(
            _time(value.get("last_snapshot_at")),
            DockWaterStatus(**values),
            tuple(str(item) for item in faults),
            DockWaterVerification.from_mapping(verification)
            if verification is not None
            else None,
        )


def decode_robot_water(value: object) -> dict[str, RobotWaterRecord]:
    if not isinstance(value, Mapping):
        raise ValueError("robot_water must be an object")
    result = {}
    for key, record in value.items():
        if not isinstance(key, str) or not key or not isinstance(record, Mapping):
            raise ValueError("invalid robot_water entry")
        result[key] = RobotWaterRecord.from_mapping(record)
    return result
