"""Typed values used by map recovery.

This module is deliberately independent of Home Assistant.  The separate map
Store codec is the only place where these values are converted to or from raw
mappings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class MapRecoveryError(RuntimeError):
    """A stable, user-presentable map operation failure."""


class MapRecoveryUnavailable(MapRecoveryError):
    """The installed Roborock runtime cannot provide Q10 map capture."""


class RecoveryCapabilityState(StrEnum):
    """Availability states exposed by the optional map bridge."""

    READY = "ready"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class RetainedMap:
    """One map slot reported live by the robot."""

    map_id: str
    name: str
    timestamp: str | None = None

    def as_response(self) -> dict[str, str | None]:
        """Serialize at the Home Assistant service-response boundary."""

        return {
            "map_id": self.map_id,
            "name": self.name,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True, slots=True)
class RecoveryCapability:
    """A stable, redaction-safe description of the optional map bridge."""

    state: RecoveryCapabilityState
    reason: str | None = None

    @property
    def available(self) -> bool:
        """Return whether all map-recovery capabilities are present."""

        return self.state is RecoveryCapabilityState.READY


@dataclass(frozen=True, slots=True)
class ArchivedRoomSummary:
    """Safe decoded room metadata retained with an archived map."""

    room_id: int
    name: str
    order_hint: int
    pixel_count: int


@dataclass(frozen=True, slots=True)
class ArchivedMap:
    """One validated map packet and its cached dashboard preview."""

    map_id: str
    name: str
    robot_timestamp: str | None
    packet_sha256: str
    packet: bytes
    preview_png: bytes
    width: int
    height: int
    rooms: tuple[ArchivedRoomSummary, ...]


@dataclass(frozen=True, slots=True)
class MapCaptureSet:
    """One atomic archive of every retained map slot."""

    snapshot_id: str
    captured_at: datetime
    trigger: str
    combined_sha256: str
    maps: tuple[ArchivedMap, ...]
    active_map_id: str | None = None


@dataclass(slots=True)
class RobotMapArchive:
    """Bounded map history and last safe failure for one registry identity."""

    capture_sets: list[MapCaptureSet] = field(default_factory=list)
    last_error: str | None = None


@dataclass(slots=True)
class MapRecoveryArchive:
    """Typed contents of the map-recovery Store."""

    robots: dict[str, RobotMapArchive] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CaptureSetSummary:
    """Bounded public metadata for one archived capture."""

    snapshot_id: str
    captured_at: datetime
    trigger: str
    map_count: int


@dataclass(frozen=True, slots=True)
class AvailableMapSummary:
    """Public metadata for one map in the newest capture."""

    map_id: str
    name: str
    timestamp: str | None


@dataclass(frozen=True, slots=True)
class MapRecoverySummary:
    """Immutable map-recovery values consumed by presentation."""

    state: str
    reason: str | None
    retention: int
    capture_count: int
    last_capture: datetime | None
    last_error: str | None
    map_selection_pending: bool
    capture_sets: tuple[CaptureSetSummary, ...]
    available_maps: tuple[AvailableMapSummary, ...]

    def as_attributes(self) -> dict[str, object]:
        """Serialize at the Home Assistant entity-attribute boundary."""

        return {
            "state": self.state,
            "reason": self.reason,
            "retention": self.retention,
            "capture_count": self.capture_count,
            "last_capture": (
                self.last_capture.isoformat() if self.last_capture else None
            ),
            "last_error": self.last_error,
            "map_selection_pending": self.map_selection_pending,
            "capture_sets": [
                {
                    "snapshot_id": item.snapshot_id,
                    "captured_at": item.captured_at.isoformat(),
                    "trigger": item.trigger,
                    "map_count": item.map_count,
                }
                for item in self.capture_sets
            ],
            "available_maps": [
                {
                    "map_id": item.map_id,
                    "name": item.name,
                    "timestamp": item.timestamp,
                }
                for item in self.available_maps
            ],
        }


@dataclass(frozen=True, slots=True)
class MapListResult:
    """Typed retained-map list response."""

    summary: MapRecoverySummary
    retained_maps: tuple[RetainedMap, ...]

    def as_response(self) -> dict[str, object]:
        """Serialize at the Home Assistant service-response boundary."""

        return {
            **self.summary.as_attributes(),
            "retained_maps": [item.as_response() for item in self.retained_maps],
        }


@dataclass(frozen=True, slots=True)
class MapCaptureResult:
    """Result of a map capture transaction."""

    snapshot_id: str
    deduplicated: bool
    map_count: int
    digest: str | None = None

    def as_response(self) -> dict[str, object]:
        """Serialize at the Home Assistant service-response boundary."""

        result: dict[str, object] = {
            "snapshot_id": self.snapshot_id,
            "deduplicated": self.deduplicated,
            "map_count": self.map_count,
        }
        if self.digest is not None:
            result["digest"] = self.digest
        return result


@dataclass(frozen=True, slots=True)
class MapActivationResult:
    """Result of the checkpointed retained-map activation transaction."""

    pre_activation_snapshot_id: str
    requested_map_id: str
    confirmed: bool

    def as_response(self) -> dict[str, object]:
        """Serialize at the Home Assistant service-response boundary."""

        return {
            "pre_activation_snapshot_id": self.pre_activation_snapshot_id,
            "requested_map_id": self.requested_map_id,
            "activation": "confirmed" if self.confirmed else "requested_unverified",
            "map_selection_pending": True,
        }


@dataclass(frozen=True, slots=True)
class MapVerificationResult:
    """Result of explicit post-selection verification."""

    verified: bool = True

    def as_response(self, preview: dict[str, object]) -> dict[str, object]:
        """Serialize at the Home Assistant service-response boundary."""

        return {"verified": self.verified, "preview": preview}
