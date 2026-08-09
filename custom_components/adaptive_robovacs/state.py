"""Typed durable state and Store-schema migration for Adaptive RoboVacs.

This module deliberately has no Home Assistant imports.  It is the sole owner
of the Store wire format so scheduler code can work with dataclasses instead of
the nested, partially optional dictionaries written by the first release.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from .const import (
    CONF_FORECAST_CONFIDENCE,
    CONF_HALL_END,
    CONF_HALL_START,
    CONF_OBSERVE_ONLY,
    CONF_UNRESOLVED_END,
    CONF_UNRESOLVED_START,
    DEFAULT_FORECAST_CONFIDENCE,
    DEFAULT_HALL_END,
    DEFAULT_HALL_START,
    DEFAULT_MINIMUM_BATTERY,
    DEFAULT_MOP_INTERVAL,
    DEFAULT_UNRESOLVED_END,
    DEFAULT_UNRESOLVED_START,
    DEFAULT_BEDROOM_INTERVAL,
    DEFAULT_COMMON_INTERVAL,
    DEFAULT_EXPECTED_MINUTES,
)


SCHEMA_VERSION = 2


class StateSchemaError(ValueError):
    """The persisted state cannot safely be loaded by this version."""


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise StateSchemaError(f"{name} must be an object")
    return value


def _string(value: object, default: str | None = None) -> str | None:
    if value is None:
        return default
    return str(value)


def _number(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _timestamp(value: object) -> datetime | None:
    """Decode an ISO timestamp without accepting naive local timestamps."""

    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [item for item in value if isinstance(item, str)]


def _event_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


@dataclass(slots=True)
class GlobalSettings:
    observe_only: bool = True
    party_mode: bool = False
    forecast_confidence: float = DEFAULT_FORECAST_CONFIDENCE
    hall_start: str = DEFAULT_HALL_START
    hall_end: str = DEFAULT_HALL_END
    unresolved_start: str = DEFAULT_UNRESOLVED_START
    unresolved_end: str = DEFAULT_UNRESOLVED_END

    @classmethod
    def from_entry(cls, entry_data: Mapping[str, object]) -> GlobalSettings:
        return cls(
            observe_only=bool(entry_data.get(CONF_OBSERVE_ONLY, True)),
            forecast_confidence=_number(
                entry_data.get(CONF_FORECAST_CONFIDENCE), DEFAULT_FORECAST_CONFIDENCE
            ),
            hall_start=str(entry_data.get(CONF_HALL_START, DEFAULT_HALL_START)),
            hall_end=str(entry_data.get(CONF_HALL_END, DEFAULT_HALL_END)),
            unresolved_start=str(entry_data.get(CONF_UNRESOLVED_START, DEFAULT_UNRESOLVED_START)),
            unresolved_end=str(entry_data.get(CONF_UNRESOLVED_END, DEFAULT_UNRESOLVED_END)),
        )

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object], defaults: GlobalSettings
    ) -> GlobalSettings:
        return cls(
            observe_only=bool(value.get("observe_only", defaults.observe_only)),
            party_mode=bool(value.get("party_mode", defaults.party_mode)),
            forecast_confidence=_number(
                value.get("forecast_confidence"), defaults.forecast_confidence
            ),
            hall_start=str(value.get("hall_start", defaults.hall_start)),
            hall_end=str(value.get("hall_end", defaults.hall_end)),
            unresolved_start=str(value.get("unresolved_start", defaults.unresolved_start)),
            unresolved_end=str(value.get("unresolved_end", defaults.unresolved_end)),
        )


@dataclass(slots=True)
class RoomSettings:
    enabled: bool
    vacuum_interval: float = DEFAULT_COMMON_INTERVAL
    mop_interval: float = DEFAULT_MOP_INTERVAL
    expected_minutes: float = DEFAULT_EXPECTED_MINUTES
    carpet: bool = False
    ignore_desired_window: bool = False

    @classmethod
    def defaults(cls, is_bedroom: bool) -> RoomSettings:
        return cls(
            enabled=not is_bedroom,
            vacuum_interval=DEFAULT_BEDROOM_INTERVAL if is_bedroom else DEFAULT_COMMON_INTERVAL,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object], default: RoomSettings) -> RoomSettings:
        return cls(
            enabled=bool(value.get("enabled", default.enabled)),
            vacuum_interval=_number(value.get("vacuum_interval"), default.vacuum_interval),
            mop_interval=_number(value.get("mop_interval"), default.mop_interval),
            expected_minutes=_number(value.get("expected_minutes"), default.expected_minutes),
            carpet=bool(value.get("carpet", default.carpet)),
            ignore_desired_window=bool(
                value.get("ignore_desired_window", default.ignore_desired_window)
            ),
        )


@dataclass(slots=True)
class RobotSettings:
    enabled: bool = True
    minimum_battery: float = DEFAULT_MINIMUM_BATTERY
    mopping_enabled: bool = False
    double_pass: bool = False
    mode: str | None = None
    mop_mode: str | None = None
    mop_intensity: str | None = None

    @classmethod
    def defaults(cls, supports_mopping: bool) -> RobotSettings:
        return cls(mopping_enabled=supports_mopping)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object], default: RobotSettings) -> RobotSettings:
        return cls(
            enabled=bool(value.get("enabled", default.enabled)),
            minimum_battery=_number(value.get("minimum_battery"), default.minimum_battery),
            mopping_enabled=bool(value.get("mopping_enabled", default.mopping_enabled)),
            double_pass=bool(value.get("double_pass", default.double_pass)),
            mode=_string(value.get("mode")),
            mop_mode=_string(value.get("mop_mode")),
            mop_intensity=_string(value.get("mop_intensity")),
        )


@dataclass(slots=True)
class OccupancySample:
    started_at: datetime
    minutes: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> OccupancySample | None:
        started = _timestamp(value.get("start"))
        if started is None:
            return None
        return cls(started_at=started, minutes=max(0, _integer(value.get("minutes"), 0)))

    def to_store(self) -> dict[str, object]:
        return {"start": _iso(self.started_at), "minutes": self.minutes}


@dataclass(slots=True)
class DurationSample:
    minutes: float
    operation: str
    passes: int
    robot_id: str
    source: str
    recorded_at: datetime | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> DurationSample | None:
        operation = _string(value.get("operation"))
        robot_id = _string(value.get("robot"))
        source = _string(value.get("source"))
        if not operation or not robot_id or not source:
            return None
        minutes = _number(value.get("minutes"), 0)
        if minutes <= 0:
            return None
        return cls(
            minutes=minutes,
            operation=operation,
            passes=max(1, _integer(value.get("passes"), 1)),
            robot_id=robot_id,
            source=source,
            recorded_at=_timestamp(value.get("at")),
        )

    def to_store(self) -> dict[str, object]:
        return {
            "minutes": self.minutes,
            "operation": self.operation,
            "passes": self.passes,
            "robot": self.robot_id,
            "source": self.source,
            "at": _iso(self.recorded_at),
        }


@dataclass(slots=True)
class RoomHistory:
    vacuum_completed_at: datetime | None = None
    mop_completed_at: datetime | None = None
    deferrals: dict[str, datetime] = field(default_factory=dict)
    occupancy: str = "unresolved"
    occupancy_source: str = "unavailable"
    unavailable_radars: int = 0
    unoccupied_since: datetime | None = None
    occupancy_samples: list[OccupancySample] = field(default_factory=list)
    source_fingerprint: str | None = None
    map_status: str = "unknown"
    map_error: str | None = None
    duration_samples: list[DurationSample] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> RoomHistory:
        raw_deferrals = value.get("deferrals", value.get("defer", {}))
        deferrals = {
            key: parsed
            for key, raw in _mapping_or_empty(raw_deferrals).items()
            if isinstance(key, str) and (parsed := _timestamp(raw)) is not None
        }
        samples = [
            sample
            for item in value.get("occupancy_samples", value.get("samples", []))
            if isinstance(item, Mapping)
            and (sample := OccupancySample.from_mapping(item)) is not None
        ]
        duration_samples = [
            sample
            for item in value.get("duration_samples", [])
            if isinstance(item, Mapping)
            and (sample := DurationSample.from_mapping(item)) is not None
        ]
        return cls(
            vacuum_completed_at=_timestamp(value.get("vacuum_completed_at", value.get("vacuum"))),
            mop_completed_at=_timestamp(value.get("mop_completed_at", value.get("mop"))),
            deferrals=deferrals,
            occupancy=str(value.get("occupancy", "unresolved")),
            occupancy_source=str(value.get("occupancy_source", value.get("source", "unavailable"))),
            unavailable_radars=max(0, _integer(value.get("unavailable_radars"), 0)),
            unoccupied_since=_timestamp(value.get("unoccupied_since")),
            occupancy_samples=samples,
            source_fingerprint=_string(value.get("source_fingerprint")),
            map_status=str(value.get("map_status", "unknown")),
            map_error=_string(value.get("map_error")),
            duration_samples=duration_samples,
        )

    def to_store(self) -> dict[str, object]:
        return {
            "vacuum_completed_at": _iso(self.vacuum_completed_at),
            "mop_completed_at": _iso(self.mop_completed_at),
            "deferrals": {key: _iso(value) for key, value in self.deferrals.items()},
            "occupancy": self.occupancy,
            "occupancy_source": self.occupancy_source,
            "unavailable_radars": self.unavailable_radars,
            "unoccupied_since": _iso(self.unoccupied_since),
            "occupancy_samples": [sample.to_store() for sample in self.occupancy_samples],
            "source_fingerprint": self.source_fingerprint,
            "map_status": self.map_status,
            "map_error": self.map_error,
            "duration_samples": [sample.to_store() for sample in self.duration_samples],
        }


@dataclass(slots=True)
class ActiveJob:
    room_id: str
    room_ids: list[str]
    operation: str
    phase: str
    source: str
    started_at: datetime | None = None
    seen_cleaning: bool = False
    expected_minutes: float | None = None
    expected_end: datetime | None = None
    last_observed_at: datetime | None = None
    passes: int = 1
    requested_operations: list[str] = field(default_factory=list)
    manual_context_id: str | None = None
    accepted_at: datetime | None = None
    observed_started_at: datetime | None = None
    recovered_at: datetime | None = None
    cleaning_finished_at: datetime | None = None
    completion_confidence: str | None = None
    timer_start: float | None = None
    duration_source: str | None = None
    measured_minutes: float | None = None
    interrupted: bool = False
    hold_reason: str | None = None
    held_at: datetime | None = None
    completion_before_hold: bool = False
    cancelling_at: datetime | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ActiveJob | None:
        room_id = _string(value.get("room"))
        room_ids = _string_list(value.get("rooms"))
        if room_id and room_id not in room_ids:
            room_ids.insert(0, room_id)
        if not room_id and room_ids:
            room_id = room_ids[0]
        operation = _string(value.get("operation"))
        phase = _string(value.get("phase"))
        source = _string(value.get("source"), "scheduler")
        if not room_id or not operation or not phase or not source:
            return None
        return cls(
            room_id=room_id,
            room_ids=room_ids or [room_id],
            operation=operation,
            phase=phase,
            source=source,
            started_at=_timestamp(value.get("started")),
            seen_cleaning=bool(value.get("seen_cleaning", False)),
            expected_minutes=_optional_number(value.get("expected_minutes")),
            expected_end=_timestamp(value.get("expected_end")),
            last_observed_at=_timestamp(value.get("last_observed_at")),
            passes=max(1, _integer(value.get("passes"), 1)),
            requested_operations=_string_list(value.get("requested_operations")),
            manual_context_id=_string(value.get("manual_context_id")),
            accepted_at=_timestamp(value.get("accepted_at")),
            observed_started_at=_timestamp(value.get("observed_started")),
            recovered_at=_timestamp(value.get("recovered_at")),
            cleaning_finished_at=_timestamp(value.get("cleaning_finished")),
            completion_confidence=_string(value.get("completion_confidence")),
            timer_start=_optional_number(value.get("timer_start")),
            duration_source=_string(value.get("duration_source")),
            measured_minutes=_optional_number(value.get("measured_minutes")),
            interrupted=bool(value.get("interrupted", False)),
            hold_reason=_string(value.get("hold_reason")),
            held_at=_timestamp(value.get("held_at")),
            completion_before_hold=bool(value.get("completion_before_hold", False)),
            cancelling_at=_timestamp(value.get("cancelling_at")),
        )

    def to_store(self) -> dict[str, object]:
        return {
            "room": self.room_id,
            "rooms": self.room_ids,
            "operation": self.operation,
            "phase": self.phase,
            "source": self.source,
            "started": _iso(self.started_at),
            "seen_cleaning": self.seen_cleaning,
            "expected_minutes": self.expected_minutes,
            "expected_end": _iso(self.expected_end),
            "last_observed_at": _iso(self.last_observed_at),
            "passes": self.passes,
            "requested_operations": self.requested_operations,
            "manual_context_id": self.manual_context_id,
            "accepted_at": _iso(self.accepted_at),
            "observed_started": _iso(self.observed_started_at),
            "recovered_at": _iso(self.recovered_at),
            "cleaning_finished": _iso(self.cleaning_finished_at),
            "completion_confidence": self.completion_confidence,
            "timer_start": self.timer_start,
            "duration_source": self.duration_source,
            "measured_minutes": self.measured_minutes,
            "interrupted": self.interrupted,
            "hold_reason": self.hold_reason,
            "held_at": _iso(self.held_at),
            "completion_before_hold": self.completion_before_hold,
            "cancelling_at": _iso(self.cancelling_at),
        }


@dataclass(slots=True)
class RobotHold:
    reason: str
    phase: str
    held_at: datetime | None = None
    last_observed_at: datetime | None = None
    returning_at: datetime | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> RobotHold | None:
        reason = _string(value.get("reason"))
        if not reason:
            return None
        return cls(
            reason=reason,
            phase=str(value.get("phase", "held")),
            held_at=_timestamp(value.get("held_at")),
            last_observed_at=_timestamp(value.get("last_observed_at")),
            returning_at=_timestamp(value.get("returning_at")),
        )

    def to_store(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            "phase": self.phase,
            "held_at": _iso(self.held_at),
            "last_observed_at": _iso(self.last_observed_at),
            "returning_at": _iso(self.returning_at),
        }


@dataclass(slots=True)
class EvaluationState:
    last_evaluation_at: datetime | None = None
    last_preview: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> EvaluationState:
        preview = value.get("last_preview", {})
        return cls(
            last_evaluation_at=_timestamp(value.get("last_evaluation_at", value.get("last_evaluation"))),
            last_preview=dict(preview) if isinstance(preview, Mapping) else {},
        )

    def to_store(self) -> dict[str, object]:
        return {
            "last_evaluation_at": _iso(self.last_evaluation_at),
            "last_preview": self.last_preview,
        }


@dataclass(slots=True)
class AuditState:
    manual_events: list[dict[str, Any]] = field(default_factory=list)
    recovery_events: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> AuditState:
        return cls(
            manual_events=_event_list(value.get("manual_events")),
            recovery_events=_event_list(value.get("recovery_events")),
        )

    def to_store(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class SchedulerState:
    global_settings: GlobalSettings
    room_settings: dict[str, RoomSettings] = field(default_factory=dict)
    robot_settings: dict[str, RobotSettings] = field(default_factory=dict)
    room_history: dict[str, RoomHistory] = field(default_factory=dict)
    active_jobs: dict[str, ActiveJob | None] = field(default_factory=dict)
    robot_holds: dict[str, RobotHold] = field(default_factory=dict)
    audit: AuditState = field(default_factory=AuditState)
    evaluation: EvaluationState = field(default_factory=EvaluationState)

    @classmethod
    def create(cls, entry_data: Mapping[str, object]) -> SchedulerState:
        return cls(global_settings=GlobalSettings.from_entry(entry_data))

    @classmethod
    def from_store(
        cls, payload: object, entry_data: Mapping[str, object]
    ) -> tuple[SchedulerState, bool]:
        """Load v2 or convert v1, returning whether a v2 save is required."""

        if payload is None:
            return cls.create(entry_data), False
        data = _mapping(payload, "stored scheduler state")
        schema_version = data.get("schema_version")
        if schema_version is None or schema_version == 1:
            return cls._from_v1(data, entry_data), True
        if schema_version != SCHEMA_VERSION:
            raise StateSchemaError(
                f"unsupported scheduler state schema: {schema_version!r}"
            )
        return cls._from_v2(data, entry_data), False

    @classmethod
    def _from_v1(cls, data: Mapping[str, object], entry_data: Mapping[str, object]) -> SchedulerState:
        defaults = GlobalSettings.from_entry(entry_data)
        global_settings = GlobalSettings.from_mapping(data, defaults)
        settings = _mapping_or_empty(data.get("settings"))
        raw_room_settings = _mapping_or_empty(settings.get("rooms"))
        raw_robot_settings = _mapping_or_empty(settings.get("robots"))
        rooms = {
            area_id: RoomHistory.from_mapping(value)
            for area_id, value in _mapping_or_empty(data.get("rooms")).items()
            if isinstance(area_id, str) and isinstance(value, Mapping)
        }
        return cls(
            global_settings=global_settings,
            room_settings={
                area_id: RoomSettings.from_mapping(value, RoomSettings.defaults(False))
                for area_id, value in raw_room_settings.items()
                if isinstance(area_id, str) and isinstance(value, Mapping)
            },
            robot_settings={
                entity_id: RobotSettings.from_mapping(value, RobotSettings.defaults(False))
                for entity_id, value in raw_robot_settings.items()
                if isinstance(entity_id, str) and isinstance(value, Mapping)
            },
            room_history=rooms,
            active_jobs={
                entity_id: ActiveJob.from_mapping(value) if isinstance(value, Mapping) else None
                for entity_id, value in _mapping_or_empty(data.get("active")).items()
                if isinstance(entity_id, str)
            },
            robot_holds={
                entity_id: hold
                for entity_id, value in _mapping_or_empty(data.get("robot_holds")).items()
                if isinstance(entity_id, str)
                and isinstance(value, Mapping)
                and (hold := RobotHold.from_mapping(value)) is not None
            },
            audit=AuditState(
                manual_events=_event_list(data.get("manual_events")),
                recovery_events=_event_list(data.get("recovery_events")),
            ),
            evaluation=EvaluationState.from_mapping(data),
        )

    @classmethod
    def _from_v2(cls, data: Mapping[str, object], entry_data: Mapping[str, object]) -> SchedulerState:
        defaults = GlobalSettings.from_entry(entry_data)
        raw_global = _mapping(data.get("global"), "global")
        raw_room_settings = _mapping(data.get("room_settings"), "room_settings")
        raw_robot_settings = _mapping(data.get("robot_settings"), "robot_settings")
        raw_history = _mapping(data.get("room_history"), "room_history")
        raw_active = _mapping(data.get("active_jobs"), "active_jobs")
        raw_holds = _mapping(data.get("robot_holds"), "robot_holds")
        raw_audit = _mapping(data.get("audit"), "audit")
        raw_evaluation = _mapping(data.get("evaluation"), "evaluation")
        return cls(
            global_settings=GlobalSettings.from_mapping(raw_global, defaults),
            room_settings={
                area_id: RoomSettings.from_mapping(value, RoomSettings.defaults(False))
                for area_id, value in raw_room_settings.items()
                if isinstance(area_id, str) and isinstance(value, Mapping)
            },
            robot_settings={
                entity_id: RobotSettings.from_mapping(value, RobotSettings.defaults(False))
                for entity_id, value in raw_robot_settings.items()
                if isinstance(entity_id, str) and isinstance(value, Mapping)
            },
            room_history={
                area_id: RoomHistory.from_mapping(value)
                for area_id, value in raw_history.items()
                if isinstance(area_id, str) and isinstance(value, Mapping)
            },
            active_jobs={
                entity_id: ActiveJob.from_mapping(value) if isinstance(value, Mapping) else None
                for entity_id, value in raw_active.items()
                if isinstance(entity_id, str)
            },
            robot_holds={
                entity_id: hold
                for entity_id, value in raw_holds.items()
                if isinstance(entity_id, str)
                and isinstance(value, Mapping)
                and (hold := RobotHold.from_mapping(value)) is not None
            },
            audit=AuditState.from_mapping(raw_audit),
            evaluation=EvaluationState.from_mapping(raw_evaluation),
        )

    def ensure_room(self, area_id: str, is_bedroom: bool) -> tuple[RoomSettings, RoomHistory]:
        settings = self.room_settings.setdefault(area_id, RoomSettings.defaults(is_bedroom))
        history = self.room_history.setdefault(area_id, RoomHistory())
        return settings, history

    def ensure_robot(self, entity_id: str, supports_mopping: bool) -> RobotSettings:
        self.active_jobs.setdefault(entity_id, None)
        return self.robot_settings.setdefault(entity_id, RobotSettings.defaults(supports_mopping))

    def to_store(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "global": asdict(self.global_settings),
            "room_settings": {
                area_id: asdict(settings) for area_id, settings in self.room_settings.items()
            },
            "robot_settings": {
                entity_id: asdict(settings)
                for entity_id, settings in self.robot_settings.items()
            },
            "room_history": {
                area_id: history.to_store() for area_id, history in self.room_history.items()
            },
            "active_jobs": {
                entity_id: job.to_store() if job else None
                for entity_id, job in self.active_jobs.items()
            },
            "robot_holds": {
                entity_id: hold.to_store() for entity_id, hold in self.robot_holds.items()
            },
            "audit": self.audit.to_store(),
            "evaluation": self.evaluation.to_store(),
        }

    def to_runtime_data(self) -> dict[str, Any]:
        """Expose a temporary runtime view while scheduler logic is extracted.

        The view is intentionally confined to the coordinator internals.  All
        persistent I/O stays on the typed v2 codec, and platform entities use
        coordinator accessors instead of this compatibility representation.
        """

        return {
            "version": SCHEMA_VERSION,
            "observe_only": self.global_settings.observe_only,
            "party_mode": self.global_settings.party_mode,
            "forecast_confidence": self.global_settings.forecast_confidence,
            "hall_start": self.global_settings.hall_start,
            "hall_end": self.global_settings.hall_end,
            "unresolved_start": self.global_settings.unresolved_start,
            "unresolved_end": self.global_settings.unresolved_end,
            "settings": {
                "rooms": {
                    area_id: asdict(settings)
                    for area_id, settings in self.room_settings.items()
                },
                "robots": {
                    entity_id: asdict(settings)
                    for entity_id, settings in self.robot_settings.items()
                },
            },
            "rooms": {
                area_id: {
                    "vacuum": _iso(history.vacuum_completed_at),
                    "mop": _iso(history.mop_completed_at),
                    "defer": {
                        operation: _iso(deferred)
                        for operation, deferred in history.deferrals.items()
                    },
                    "occupancy": history.occupancy,
                    "source": history.occupancy_source,
                    "unavailable_radars": history.unavailable_radars,
                    "unoccupied_since": _iso(history.unoccupied_since),
                    "samples": [sample.to_store() for sample in history.occupancy_samples],
                    "source_fingerprint": history.source_fingerprint,
                    "map_status": history.map_status,
                    "map_error": history.map_error,
                    "duration_samples": [sample.to_store() for sample in history.duration_samples],
                }
                for area_id, history in self.room_history.items()
            },
            "active": {
                entity_id: job.to_store() if job else None
                for entity_id, job in self.active_jobs.items()
            },
            "robot_holds": {
                entity_id: hold.to_store() for entity_id, hold in self.robot_holds.items()
            },
            "manual_events": self.audit.manual_events,
            "recovery_events": self.audit.recovery_events,
            "last_evaluation": _iso(self.evaluation.last_evaluation_at),
            "last_preview": self.evaluation.last_preview,
        }


def _mapping_or_empty(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _optional_number(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
