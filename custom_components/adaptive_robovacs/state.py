"""Typed durable state and Store-schema migration for Adaptive RoboVacs.

This module deliberately has no Home Assistant imports.  It is the sole owner
of the Store wire format so scheduler code can work with dataclasses instead of
the nested, partially optional dictionaries written by the first release.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from .const import (
    CONF_FORECAST_CONFIDENCE,
    CONF_OBSERVE_ONLY,
    CONF_UNRESOLVED_END,
    CONF_UNRESOLVED_START,
    DEFAULT_ADJACENCY_NIGHT_END,
    DEFAULT_ADJACENCY_NIGHT_START,
    DEFAULT_BEDROOM_INTERVAL,
    DEFAULT_COMMON_INTERVAL,
    DEFAULT_EXPECTED_MINUTES,
    DEFAULT_FORECAST_CONFIDENCE,
    DEFAULT_MINIMUM_BATTERY,
    DEFAULT_UNRESOLVED_END,
    DEFAULT_UNRESOLVED_START,
)
from .models import (
    FLOOR_PLAN_MAX_GRID_COORDINATE,
    FLOOR_PLAN_MIN_ROOM_SPAN,
    ROBOT_ERROR_CATEGORIES,
    ROOM_PROFILE_OVERRIDE_KEYS,
    AdjacencyMode,
    CleaningOperation,
    CleaningProgram,
    FaultCode,
    JobPhase,
    JobSource,
    OccurrenceSource,
    RequestedCleaningProfile,
    ResolvedCleaningProfile,
    StageStatus,
    floor_plan_integer,
    is_valid_daily_time,
    normalize_floor_plan_edge,
    room_cleaning_profile_is_custom,
)

SCHEMA_VERSION = 18
RETIRED_GLOBAL_SETTINGS = frozenset({"hall_start", "hall_end"})
DAILY_WINDOW_VERSION = 1


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


def _optional_string(value: object, name: str) -> str | None:
    """Decode a nullable string without coercing current-schema corruption."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise StateSchemaError(f"{name} must be a string or null")
    return value


def _boolean(value: object, default: bool, name: str) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise StateSchemaError(f"{name} must be a boolean")
    return value


def _number(value: object, default: float) -> float:
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return default
    try:
        return float(value)
    except TypeError, ValueError:
        return default


def _integer(value: object, default: int) -> int:
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return default
    try:
        return int(value)
    except TypeError, ValueError:
        return default


def _bounded_number(
    value: object,
    default: float,
    name: str,
    minimum: float,
    maximum: float,
) -> float:
    """Decode one persisted bounded number without hiding corruption."""

    candidate = default if value is None else value
    if not isinstance(candidate, (str, bytes, bytearray, int, float)):
        raise StateSchemaError(f"{name} must be a number")
    try:
        parsed = float(candidate)
    except (TypeError, ValueError) as err:
        raise StateSchemaError(f"{name} must be a number") from err
    if not minimum <= parsed <= maximum:
        raise StateSchemaError(f"{name} must be between {minimum:g} and {maximum:g}")
    return parsed


def _daily_time(value: object, default: str, name: str) -> str:
    """Decode one required zero-padded daily time."""

    candidate = default if value is None else value
    if not is_valid_daily_time(candidate):
        raise StateSchemaError(f"{name} must be a zero-padded HH:MM value")
    return str(candidate)


def _timestamp(value: object) -> datetime | None:
    """Decode an ISO timestamp, treating naive values as UTC."""

    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [item for item in value if isinstance(item, str)]


def _sequence(value: object) -> tuple[object, ...]:
    """Return one persisted JSON array as an immutable decode sequence."""

    return tuple(value) if isinstance(value, (list, tuple)) else ()


def _cleaning_operation(value: object) -> CleaningOperation | None:
    try:
        return CleaningOperation(str(value))
    except ValueError:
        return None


def _cleaning_operations(value: object) -> tuple[CleaningOperation, ...]:
    operations: list[CleaningOperation] = []
    for item in _sequence(value):
        operation = _cleaning_operation(item)
        if operation is not None:
            operations.append(operation)
    return tuple(operations)


def _cleaning_program(value: object) -> CleaningProgram | None:
    try:
        return CleaningProgram(str(value))
    except ValueError:
        return None


def _job_phase(value: object) -> JobPhase | None:
    try:
        return JobPhase(str(value))
    except ValueError:
        return None


def _job_source(value: object) -> JobSource | None:
    try:
        return JobSource(str(value))
    except ValueError:
        return None


def _profile_mapping(value: object) -> dict[str, str | None]:
    """Decode the bounded, string-only cleaning profile snapshot."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise StateSchemaError("cleaning profile must be an object")
    allowed = {
        "operation",
        "fan_speed",
        "mode",
        "mop_mode",
        "mop_intensity",
        "cleaning_depth",
    }
    if set(value) - allowed:
        raise StateSchemaError("cleaning profile has unsupported fields")
    if any(item is not None and not isinstance(item, str) for item in value.values()):
        raise StateSchemaError("cleaning profile values must be strings or null")
    return {str(key): item for key, item in value.items()}


def _profile_sources_mapping(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise StateSchemaError("cleaning profile sources must be an object")
    allowed = {"fan_speed", "mode", "mop_mode", "mop_intensity", "cleaning_depth"}
    if set(value) - allowed or any(
        item not in {"room", "robot"} for item in value.values()
    ):
        raise StateSchemaError("cleaning profile sources are invalid")
    return {str(key): str(item) for key, item in value.items()}


def _optional_daily_time(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not is_valid_daily_time(value):
        raise StateSchemaError(f"{name} must be a zero-padded HH:MM value or null")
    return str(value)


def _optional_pass_count(value: object) -> int | None:
    if value is None:
        return None
    parsed = _integer(value, 0)
    if parsed not in {1, 2}:
        raise StateSchemaError("room pass_count must be 1, 2, or null")
    return parsed


@dataclass(slots=True)
class GlobalSettings:
    observe_only: bool = True
    party_mode: bool = False
    forecast_confidence: float = DEFAULT_FORECAST_CONFIDENCE
    unresolved_start: str = DEFAULT_UNRESOLVED_START
    unresolved_end: str = DEFAULT_UNRESOLVED_END
    adjacency_night_start: str = DEFAULT_ADJACENCY_NIGHT_START
    adjacency_night_end: str = DEFAULT_ADJACENCY_NIGHT_END

    @classmethod
    def from_entry(cls, entry_data: Mapping[str, object]) -> GlobalSettings:
        return cls(
            observe_only=bool(entry_data.get(CONF_OBSERVE_ONLY, True)),
            forecast_confidence=_number(
                entry_data.get(CONF_FORECAST_CONFIDENCE), DEFAULT_FORECAST_CONFIDENCE
            ),
            unresolved_start=str(
                entry_data.get(CONF_UNRESOLVED_START, DEFAULT_UNRESOLVED_START)
            ),
            unresolved_end=str(
                entry_data.get(CONF_UNRESOLVED_END, DEFAULT_UNRESOLVED_END)
            ),
        )

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object], defaults: GlobalSettings
    ) -> GlobalSettings:
        unresolved_start = _daily_time(
            value.get("unresolved_start"),
            defaults.unresolved_start,
            "global unresolved_start",
        )
        unresolved_end = _daily_time(
            value.get("unresolved_end"),
            defaults.unresolved_end,
            "global unresolved_end",
        )
        night_start = _daily_time(
            value.get("adjacency_night_start"),
            defaults.adjacency_night_start,
            "global adjacency_night_start",
        )
        night_end = _daily_time(
            value.get("adjacency_night_end"),
            defaults.adjacency_night_end,
            "global adjacency_night_end",
        )
        if night_start == night_end:
            raise StateSchemaError("adjacency night start and end must differ")
        return cls(
            observe_only=bool(value.get("observe_only", defaults.observe_only)),
            party_mode=bool(value.get("party_mode", defaults.party_mode)),
            forecast_confidence=_bounded_number(
                value.get("forecast_confidence"),
                defaults.forecast_confidence,
                "global forecast_confidence",
                50,
                95,
            ),
            unresolved_start=unresolved_start,
            unresolved_end=unresolved_end,
            adjacency_night_start=night_start,
            adjacency_night_end=night_end,
        )


@dataclass(slots=True)
class RoomSettings:
    enabled: bool
    cleaning_interval: float = DEFAULT_COMMON_INTERVAL
    expected_minutes: float = DEFAULT_EXPECTED_MINUTES
    ignore_desired_window: bool = False
    desired_window_start: str | None = None
    desired_window_end: str | None = None
    cleaning_program: CleaningProgram | None = None
    vacuum_pass_count: int | None = None
    mop_pass_count: int | None = None
    fan_speed: str | None = None
    mode: str | None = None
    mop_mode: str | None = None
    mop_intensity: str | None = None
    cleaning_depth: str | None = None
    profile_custom: bool = False
    adjacency_mode: AdjacencyMode = AdjacencyMode.NIGHT_ONLY

    @property
    def vacuum_interval(self) -> float:
        """Compatibility alias for the surviving cadence entity."""

        return self.cleaning_interval

    @vacuum_interval.setter
    def vacuum_interval(self, value: float) -> None:
        self.cleaning_interval = value

    @property
    def mop_interval(self) -> float:
        """Compatibility read while consumers migrate to one cadence."""

        return self.cleaning_interval

    @mop_interval.setter
    def mop_interval(self, value: float) -> None:
        self.cleaning_interval = value

    @property
    def pass_count(self) -> int | None:
        """Compatibility alias for the vacuum-pass override."""

        return self.vacuum_pass_count

    @pass_count.setter
    def pass_count(self, value: int | None) -> None:
        self.vacuum_pass_count = value

    @classmethod
    def defaults(cls, is_bedroom: bool) -> RoomSettings:
        return cls(
            enabled=not is_bedroom,
            cleaning_interval=DEFAULT_BEDROOM_INTERVAL
            if is_bedroom
            else DEFAULT_COMMON_INTERVAL,
        )

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object], default: RoomSettings
    ) -> RoomSettings:
        raw_window = value.get("daily_window")
        if raw_window is not None:
            window = _mapping(raw_window, "room daily_window")
            if window.get("version") != DAILY_WINDOW_VERSION:
                raise StateSchemaError(
                    f"unsupported room daily-window version: {window.get('version')!r}"
                )
        else:
            window = {}
        raw_adjacency_mode = value.get("adjacency_mode", default.adjacency_mode)
        try:
            if not isinstance(raw_adjacency_mode, str):
                raise ValueError("mode must be a string")
            adjacency_mode = AdjacencyMode(raw_adjacency_mode)
        except ValueError as err:
            raise StateSchemaError("invalid room adjacency_mode") from err
        raw_start = value.get("desired_window_start", window.get("start"))
        raw_end = value.get("desired_window_end", window.get("end"))
        profile_custom = _boolean(
            value.get("profile_custom"),
            False,
            "room profile_custom",
        ) or any(value.get(key) is not None for key in ROOM_PROFILE_OVERRIDE_KEYS)
        return cls(
            enabled=bool(value.get("enabled", default.enabled)),
            adjacency_mode=adjacency_mode,
            cleaning_interval=_bounded_number(
                value.get("cleaning_interval", value.get("vacuum_interval")),
                default.cleaning_interval,
                "room cleaning_interval",
                12,
                336,
            ),
            expected_minutes=_bounded_number(
                value.get("expected_minutes"),
                default.expected_minutes,
                "room expected_minutes",
                5,
                180,
            ),
            ignore_desired_window=bool(
                value.get("ignore_desired_window", default.ignore_desired_window)
            ),
            desired_window_start=_optional_daily_time(
                raw_start, "room daily-window start"
            ),
            desired_window_end=_optional_daily_time(raw_end, "room daily-window end"),
            cleaning_program=_cleaning_program(value.get("cleaning_program")),
            vacuum_pass_count=_optional_pass_count(
                value.get("vacuum_pass_count", value.get("pass_count"))
            ),
            mop_pass_count=_optional_pass_count(value.get("mop_pass_count")),
            fan_speed=_optional_string(value.get("fan_speed"), "room fan_speed"),
            mode=_optional_string(value.get("mode"), "room mode"),
            mop_mode=_optional_string(value.get("mop_mode"), "room mop_mode"),
            mop_intensity=_optional_string(
                value.get("mop_intensity"), "room mop_intensity"
            ),
            cleaning_depth=_optional_string(
                value.get("cleaning_depth"), "room cleaning_depth"
            ),
            profile_custom=profile_custom,
        )

    def to_store(self) -> dict[str, object]:
        """Encode the first versioned daily-window schedule shape."""

        return {
            "enabled": self.enabled,
            "adjacency_mode": self.adjacency_mode.value,
            "cleaning_interval": self.cleaning_interval,
            "expected_minutes": self.expected_minutes,
            "ignore_desired_window": self.ignore_desired_window,
            "daily_window": {
                "version": DAILY_WINDOW_VERSION,
                "start": self.desired_window_start,
                "end": self.desired_window_end,
            },
            "cleaning_program": self.cleaning_program,
            "vacuum_pass_count": self.vacuum_pass_count,
            "mop_pass_count": self.mop_pass_count,
            "pass_count": self.vacuum_pass_count,
            "fan_speed": self.fan_speed,
            "mode": self.mode,
            "mop_mode": self.mop_mode,
            "mop_intensity": self.mop_intensity,
            "cleaning_depth": self.cleaning_depth,
            "profile_custom": room_cleaning_profile_is_custom(self),
        }

    def to_runtime(self) -> dict[str, object]:
        """Expose flat compatibility keys to the coordinator runtime view."""

        return {
            "enabled": self.enabled,
            "cleaning_interval": self.cleaning_interval,
            # Compatibility aliases retain the surviving entity IDs during v6.
            "vacuum_interval": self.cleaning_interval,
            "mop_interval": self.cleaning_interval,
            "expected_minutes": self.expected_minutes,
            "ignore_desired_window": self.ignore_desired_window,
            "desired_window_start": self.desired_window_start,
            "desired_window_end": self.desired_window_end,
            "cleaning_program": self.cleaning_program,
            "vacuum_pass_count": self.vacuum_pass_count,
            "mop_pass_count": self.mop_pass_count,
            "pass_count": self.vacuum_pass_count,
            "fan_speed": self.fan_speed,
            "mode": self.mode,
            "mop_mode": self.mop_mode,
            "mop_intensity": self.mop_intensity,
            "cleaning_depth": self.cleaning_depth,
            "profile_custom": room_cleaning_profile_is_custom(self),
        }


@dataclass(slots=True)
class RobotSettings:
    enabled: bool = True
    minimum_battery: float = DEFAULT_MINIMUM_BATTERY
    cleaning_program: CleaningProgram = CleaningProgram.VACUUM_ONLY
    double_pass: bool = False
    mop_double_pass: bool = False
    mode: str | None = None
    mop_mode: str | None = None
    mop_intensity: str | None = None
    fan_speed: str | None = None
    cleaning_depth: str | None = None
    cleaning_depth_configured: bool = False
    direct_custom_mop_migrated: bool = False

    @classmethod
    def defaults(cls, supports_mopping: bool) -> RobotSettings:
        return cls(
            cleaning_program=(
                CleaningProgram.VACUUM_THEN_MOP
                if supports_mopping
                else CleaningProgram.VACUUM_ONLY
            )
        )

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object], default: RobotSettings
    ) -> RobotSettings:
        raw_program = value.get("cleaning_program")
        program = _cleaning_program(raw_program) or (
            CleaningProgram.VACUUM_THEN_MOP
            if bool(
                value.get(
                    "mopping_enabled",
                    default.cleaning_program is not CleaningProgram.VACUUM_ONLY,
                )
            )
            else CleaningProgram.VACUUM_ONLY
        )
        return cls(
            enabled=bool(value.get("enabled", default.enabled)),
            minimum_battery=_bounded_number(
                value.get("minimum_battery"),
                default.minimum_battery,
                "robot minimum_battery",
                20,
                100,
            ),
            cleaning_program=program,
            double_pass=bool(value.get("double_pass", default.double_pass)),
            mop_double_pass=bool(value.get("mop_double_pass", default.mop_double_pass)),
            mode=_string(value.get("mode")),
            mop_mode=_string(value.get("mop_mode")),
            mop_intensity=_string(value.get("mop_intensity")),
            fan_speed=_string(value.get("fan_speed")),
            cleaning_depth=_string(value.get("cleaning_depth")),
            cleaning_depth_configured=bool(
                value.get(
                    "cleaning_depth_configured",
                    value.get("cleaning_depth") is not None,
                )
            ),
            direct_custom_mop_migrated=bool(
                value.get("direct_custom_mop_migrated", False)
            ),
        )

    def to_runtime(self) -> dict[str, object]:
        value = asdict(self)
        value["mopping_enabled"] = (
            self.cleaning_program is not CleaningProgram.VACUUM_ONLY
        )
        return value


@dataclass(slots=True)
class OccupancySample:
    started_at: datetime
    minutes: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> OccupancySample | None:
        started = _timestamp(value.get("start"))
        if started is None:
            return None
        return cls(
            started_at=started, minutes=max(0, _integer(value.get("minutes"), 0))
        )

    def to_store(self) -> dict[str, object]:
        return {"start": _iso(self.started_at), "minutes": self.minutes}


@dataclass(slots=True)
class DurationSample:
    minutes: float
    operation: CleaningOperation
    passes: int
    robot_registry_id: str
    source: str
    recorded_at: datetime | None = None
    measurement_version: int = 1

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> DurationSample | None:
        operation = _cleaning_operation(value.get("operation"))
        robot_registry_id = _string(value.get("robot_registry_id", value.get("robot")))
        source = _string(value.get("source"))
        if operation is None or not robot_registry_id or not source:
            return None
        minutes = _number(value.get("minutes"), 0)
        if minutes <= 0:
            return None
        return cls(
            minutes=minutes,
            operation=operation,
            passes=max(1, _integer(value.get("passes"), 1)),
            robot_registry_id=robot_registry_id,
            source=source,
            recorded_at=_timestamp(value.get("at")),
            measurement_version=max(1, _integer(value.get("measurement_version"), 1)),
        )

    def to_store(self) -> dict[str, object]:
        return {
            "minutes": self.minutes,
            "operation": self.operation,
            "passes": self.passes,
            "robot_registry_id": self.robot_registry_id,
            "source": self.source,
            "at": _iso(self.recorded_at),
            "measurement_version": self.measurement_version,
        }


@dataclass(slots=True)
class Deferral:
    """One room-scoped, explainable cadence deferral."""

    until: datetime
    source: str = "legacy_unknown"
    created_at: datetime | None = None
    room_area_id: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> Deferral | None:
        until = _timestamp(value.get("until", value.get("deferred_until")))
        if until is None:
            return None
        return cls(
            until=until,
            source=str(value.get("source", "legacy_unknown")),
            created_at=_timestamp(value.get("created_at")),
            room_area_id=_optional_string(
                value.get("room_area_id"), "deferral room_area_id"
            ),
        )

    def to_store(self) -> dict[str, object]:
        return {
            "until": _iso(self.until),
            "source": self.source,
            "created_at": _iso(self.created_at),
            "room_area_id": self.room_area_id,
        }


@dataclass(slots=True)
class RoomHistory:
    cleaning_completed_at: datetime | None = None
    vacuum_completed_at: datetime | None = None
    mop_completed_at: datetime | None = None
    deferrals: dict[str, Deferral] = field(default_factory=dict)
    occupancy: str = "unresolved"
    occupancy_source: str = "unavailable"
    unavailable_radars: int = 0
    unoccupied_since: datetime | None = None
    occupancy_samples: list[OccupancySample] = field(default_factory=list)
    source_fingerprint: str | None = None
    map_status: str = "unknown"
    map_error: str | None = None
    duration_samples: list[DurationSample] = field(default_factory=list)
    last_stage_outcome: str | None = None
    last_stage_reason: str | None = None
    last_stage_at: datetime | None = None
    last_stage_summary: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> RoomHistory:
        raw_deferrals = _mapping_or_empty(
            value.get("deferrals", value.get("defer", {}))
        )
        raw_metadata = _mapping_or_empty(value.get("deferral_meta"))
        deferrals: dict[str, Deferral] = {}
        for key, raw in raw_deferrals.items():
            record = Deferral.from_mapping(raw) if isinstance(raw, Mapping) else None
            metadata = raw_metadata.get(key)
            if record is None and isinstance(metadata, Mapping):
                merged = dict(metadata)
                merged.setdefault("until", raw)
                record = Deferral.from_mapping(merged)
            if record is None and (until := _timestamp(raw)) is not None:
                record = Deferral(until=until)
            if record is not None:
                deferrals[key] = record
        samples: list[OccupancySample] = []
        for item in _sequence(value.get("occupancy_samples", value.get("samples", []))):
            if isinstance(item, Mapping):
                occupancy_sample = OccupancySample.from_mapping(item)
                if occupancy_sample is not None:
                    samples.append(occupancy_sample)
        duration_samples: list[DurationSample] = []
        for item in _sequence(value.get("duration_samples", [])):
            if isinstance(item, Mapping):
                duration_sample = DurationSample.from_mapping(item)
                if duration_sample is not None:
                    duration_samples.append(duration_sample)
        vacuum_completed = _timestamp(
            value.get("vacuum_completed_at", value.get("vacuum"))
        )
        mop_completed = _timestamp(value.get("mop_completed_at", value.get("mop")))
        cleaning_completed = _timestamp(
            value.get("cleaning_completed_at", value.get("cleaning"))
        )
        if cleaning_completed is None:
            cleaning_completed = max(
                (item for item in (vacuum_completed, mop_completed) if item),
                default=None,
            )
        if "cleaning" not in deferrals:
            legacy_deferral = max(
                (
                    item.until
                    for key, item in deferrals.items()
                    if key in {"vacuum", "mop"}
                ),
                default=None,
            )
            if legacy_deferral:
                deferrals["cleaning"] = Deferral(until=legacy_deferral)
        return cls(
            cleaning_completed_at=cleaning_completed,
            vacuum_completed_at=vacuum_completed,
            mop_completed_at=mop_completed,
            deferrals=deferrals,
            occupancy=str(value.get("occupancy", "unresolved")),
            occupancy_source=str(
                value.get("occupancy_source", value.get("source", "unavailable"))
            ),
            unavailable_radars=max(0, _integer(value.get("unavailable_radars"), 0)),
            unoccupied_since=_timestamp(value.get("unoccupied_since")),
            occupancy_samples=samples,
            source_fingerprint=_string(value.get("source_fingerprint")),
            map_status=str(value.get("map_status", "unknown")),
            map_error=_string(value.get("map_error")),
            duration_samples=duration_samples,
            last_stage_outcome=_string(value.get("last_stage_outcome")),
            last_stage_reason=_string(value.get("last_stage_reason")),
            last_stage_at=_timestamp(value.get("last_stage_at")),
            last_stage_summary=_string(value.get("last_stage_summary")),
        )

    def to_store(self) -> dict[str, object]:
        return {
            "cleaning_completed_at": _iso(self.cleaning_completed_at),
            "vacuum_completed_at": _iso(self.vacuum_completed_at),
            "mop_completed_at": _iso(self.mop_completed_at),
            "deferrals": {
                key: (
                    value.to_store()
                    if isinstance(value, Deferral)
                    else Deferral(until=value).to_store()
                )
                for key, value in self.deferrals.items()
            },
            "occupancy": self.occupancy,
            "occupancy_source": self.occupancy_source,
            "unavailable_radars": self.unavailable_radars,
            "unoccupied_since": _iso(self.unoccupied_since),
            "occupancy_samples": [
                sample.to_store() for sample in self.occupancy_samples
            ],
            "source_fingerprint": self.source_fingerprint,
            "map_status": self.map_status,
            "map_error": self.map_error,
            "duration_samples": [sample.to_store() for sample in self.duration_samples],
            "last_stage_outcome": self.last_stage_outcome,
            "last_stage_reason": self.last_stage_reason,
            "last_stage_at": _iso(self.last_stage_at),
            "last_stage_summary": self.last_stage_summary,
        }

    def to_runtime(self) -> dict[str, object]:
        """Encode the legacy method-facing shape from typed live state."""

        return {
            "cleaning": _iso(self.cleaning_completed_at),
            "vacuum": _iso(self.vacuum_completed_at),
            "mop": _iso(self.mop_completed_at),
            "defer": {
                operation: _iso(deferral.until)
                for operation, deferral in self.deferrals.items()
            },
            "deferral_meta": {
                operation: deferral.to_store()
                for operation, deferral in self.deferrals.items()
            },
            "occupancy": self.occupancy,
            "source": self.occupancy_source,
            "unavailable_radars": self.unavailable_radars,
            "unoccupied_since": _iso(self.unoccupied_since),
            "samples": [sample.to_store() for sample in self.occupancy_samples],
            "source_fingerprint": self.source_fingerprint,
            "map_status": self.map_status,
            "map_error": self.map_error,
            "duration_samples": [sample.to_store() for sample in self.duration_samples],
            "last_stage_outcome": self.last_stage_outcome,
            "last_stage_reason": self.last_stage_reason,
            "last_stage_at": _iso(self.last_stage_at),
            "last_stage_summary": self.last_stage_summary,
        }


@dataclass(slots=True)
class ActiveJob:
    room_id: str
    room_ids: list[str]
    operation: CleaningOperation
    phase: JobPhase
    source: JobSource
    started_at: datetime | None = None
    seen_cleaning: bool = False
    expected_minutes: float | None = None
    expected_end: datetime | None = None
    last_observed_at: datetime | None = None
    passes: int = 1
    requested_operations: list[CleaningOperation] = field(default_factory=list)
    manual_context_id: str | None = None
    accepted_at: datetime | None = None
    mop_washing_at: datetime | None = None
    observed_started_at: datetime | None = None
    recovered_at: datetime | None = None
    cleaning_finished_at: datetime | None = None
    completion_confidence: str | None = None
    timer_start: float | None = None
    native_timer_elapsed: float | None = None
    duration_source: str | None = None
    measured_minutes: float | None = None
    docked_at: datetime | None = None
    interruption_started_at: datetime | None = None
    interruption_minutes: float = 0
    forecast_sample_eligible: bool = False
    recovery_crossed: bool = False
    interrupted: bool = False
    hold_reason: str | None = None
    held_at: datetime | None = None
    completion_before_hold: bool = False
    cancelling_at: datetime | None = None
    adapter_id: str = "generic"
    adapter_schema_version: int = 1
    occurrence_id: str | None = None
    stage_index: int | None = None
    cleaning_profile: ResolvedCleaningProfile | None = None
    requested_profile: RequestedCleaningProfile | None = None
    profile_sources: tuple[tuple[str, str], ...] = ()
    manual_mode: str | None = None
    q10_max_plus_fallback: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ActiveJob | None:
        room_id = _string(value.get("room"))
        room_ids = _string_list(value.get("rooms"))
        if room_id and room_id not in room_ids:
            room_ids.insert(0, room_id)
        if not room_id and room_ids:
            room_id = room_ids[0]
        operation = _cleaning_operation(value.get("operation"))
        phase = _job_phase(value.get("phase"))
        source = _job_source(value.get("source", JobSource.SCHEDULER))
        if not room_id or operation is None or phase is None or source is None:
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
            requested_operations=[
                requested_operation
                for item in _string_list(value.get("requested_operations"))
                if (requested_operation := _cleaning_operation(item)) is not None
            ],
            manual_context_id=_string(value.get("manual_context_id")),
            accepted_at=_timestamp(value.get("accepted_at")),
            mop_washing_at=_timestamp(value.get("mop_washing_at")),
            observed_started_at=_timestamp(value.get("observed_started")),
            recovered_at=_timestamp(value.get("recovered_at")),
            cleaning_finished_at=_timestamp(value.get("cleaning_finished")),
            completion_confidence=_string(value.get("completion_confidence")),
            timer_start=_optional_number(value.get("timer_start")),
            native_timer_elapsed=_optional_number(value.get("native_timer_elapsed")),
            duration_source=_string(value.get("duration_source")),
            measured_minutes=_optional_number(value.get("measured_minutes")),
            docked_at=_timestamp(value.get("docked_at")),
            interruption_started_at=_timestamp(value.get("interruption_started_at")),
            interruption_minutes=max(0, _number(value.get("interruption_minutes"), 0)),
            forecast_sample_eligible=bool(value.get("forecast_sample_eligible", False)),
            recovery_crossed=bool(value.get("recovery_crossed", False)),
            interrupted=bool(value.get("interrupted", False)),
            hold_reason=_string(value.get("hold_reason")),
            held_at=_timestamp(value.get("held_at")),
            completion_before_hold=bool(value.get("completion_before_hold", False)),
            cancelling_at=_timestamp(value.get("cancelling_at")),
            adapter_id=str(value.get("adapter_id", "generic")),
            adapter_schema_version=max(
                1, _integer(value.get("adapter_schema_version"), 1)
            ),
            occurrence_id=_string(value.get("occurrence_id")),
            stage_index=(
                max(0, _integer(value.get("stage_index"), 0))
                if value.get("stage_index") is not None
                else None
            ),
            cleaning_profile=(
                ResolvedCleaningProfile.from_mapping(
                    operation,
                    profile,
                )
                if (profile := _profile_mapping(value.get("cleaning_profile")))
                else None
            ),
            requested_profile=(
                RequestedCleaningProfile.from_mapping(requested)
                if (requested := _profile_mapping(value.get("requested_profile")))
                else None
            ),
            profile_sources=tuple(
                sorted(_profile_sources_mapping(value.get("profile_sources")).items())
            ),
            manual_mode=_string(value.get("manual_mode")),
            q10_max_plus_fallback=bool(value.get("q10_max_plus_fallback", False)),
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
            "mop_washing_at": _iso(self.mop_washing_at),
            "observed_started": _iso(self.observed_started_at),
            "recovered_at": _iso(self.recovered_at),
            "cleaning_finished": _iso(self.cleaning_finished_at),
            "completion_confidence": self.completion_confidence,
            "timer_start": self.timer_start,
            "native_timer_elapsed": self.native_timer_elapsed,
            "duration_source": self.duration_source,
            "measured_minutes": self.measured_minutes,
            "docked_at": _iso(self.docked_at),
            "interruption_started_at": _iso(self.interruption_started_at),
            "interruption_minutes": self.interruption_minutes,
            "forecast_sample_eligible": self.forecast_sample_eligible,
            "recovery_crossed": self.recovery_crossed,
            "interrupted": self.interrupted,
            "hold_reason": self.hold_reason,
            "held_at": _iso(self.held_at),
            "completion_before_hold": self.completion_before_hold,
            "cancelling_at": _iso(self.cancelling_at),
            "adapter_id": self.adapter_id,
            "adapter_schema_version": self.adapter_schema_version,
            "occurrence_id": self.occurrence_id,
            "stage_index": self.stage_index,
            "cleaning_profile": (
                self.cleaning_profile.to_mapping() if self.cleaning_profile else {}
            ),
            "requested_profile": (
                self.requested_profile.to_mapping() if self.requested_profile else {}
            ),
            "profile_sources": dict(self.profile_sources),
            "manual_mode": self.manual_mode,
            "q10_max_plus_fallback": self.q10_max_plus_fallback,
        }


@dataclass(slots=True)
class CleaningStage:
    operation: CleaningOperation
    passes: int
    status: StageStatus = StageStatus.PENDING
    reason: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    cleaning_profile: ResolvedCleaningProfile | None = None
    requested_profile: RequestedCleaningProfile | None = None
    profile_sources: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CleaningStage | None:
        operation = _cleaning_operation(value.get("operation"))
        if operation is None:
            return None
        try:
            status = StageStatus(str(value.get("status", StageStatus.PENDING)))
        except ValueError:
            status = StageStatus.PENDING
        profile = _profile_mapping(value.get("cleaning_profile"))
        if profile.get("operation") not in {None, operation}:
            raise StateSchemaError("stage cleaning profile operation does not match")
        requested = _profile_mapping(value.get("requested_profile"))
        return cls(
            operation,
            max(1, _integer(value.get("passes"), 1)),
            status,
            _string(value.get("reason")),
            _timestamp(value.get("started_at")),
            _timestamp(value.get("completed_at")),
            ResolvedCleaningProfile.from_mapping(operation, profile)
            if profile
            else None,
            RequestedCleaningProfile.from_mapping(requested) if requested else None,
            tuple(
                sorted(_profile_sources_mapping(value.get("profile_sources")).items())
            ),
        )

    def to_store(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "passes": self.passes,
            "status": self.status,
            "reason": self.reason,
            "started_at": _iso(self.started_at),
            "completed_at": _iso(self.completed_at),
            "cleaning_profile": (
                self.cleaning_profile.to_mapping() if self.cleaning_profile else {}
            ),
            "requested_profile": (
                self.requested_profile.to_mapping() if self.requested_profile else {}
            ),
            "profile_sources": dict(self.profile_sources),
        }


@dataclass(slots=True)
class CleaningOccurrence:
    occurrence_id: str
    room_id: str
    robot_registry_id: str
    robot_entity_id: str | None
    program: CleaningProgram
    stages: list[CleaningStage]
    scheduled_at: datetime
    created_at: datetime
    adapter_id: str
    adapter_schema_version: int
    current_stage: int = 0
    source: OccurrenceSource = OccurrenceSource.SCHEDULER
    manual_mode: str | None = None
    manual_override: bool = False
    bypass_desired_window: bool = False
    manual_context_id: str | None = None
    manual_user_id: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CleaningOccurrence | None:
        stages: list[CleaningStage] = []
        for item in _sequence(value.get("stages", [])):
            if isinstance(item, Mapping):
                stage = CleaningStage.from_mapping(item)
                if stage is not None:
                    stages.append(stage)
        occurrence_id = _string(value.get("occurrence_id"))
        room_id = _string(value.get("room_id"))
        robot_registry_id = _string(value.get("robot_registry_id"))
        program = _cleaning_program(value.get("program"))
        robot_entity_id = _string(value.get("robot_entity_id"))
        scheduled = _timestamp(value.get("scheduled_at"))
        created = _timestamp(value.get("created_at"))
        if (
            not occurrence_id
            or not room_id
            or not robot_registry_id
            or program is None
            or scheduled is None
            or created is None
            or not stages
        ):
            return None
        current = min(max(0, _integer(value.get("current_stage"), 0)), len(stages))
        try:
            source = OccurrenceSource(
                str(value.get("source", OccurrenceSource.SCHEDULER))
            )
        except ValueError as err:
            raise StateSchemaError("occurrence source is invalid") from err
        manual_mode = _optional_string(
            value.get("manual_mode"), "occurrence manual_mode"
        )
        if manual_mode not in {None, "configured", "vacuum_only", "mop_only"}:
            raise StateSchemaError("occurrence manual_mode is invalid")
        return cls(
            occurrence_id,
            room_id,
            robot_registry_id,
            robot_entity_id,
            program,
            stages,
            scheduled,
            created,
            str(value.get("adapter_id", "generic")),
            max(1, _integer(value.get("adapter_schema_version"), 1)),
            current,
            source,
            manual_mode,
            _boolean(
                value.get("manual_override"),
                False,
                "occurrence manual_override",
            ),
            _boolean(
                value.get("bypass_desired_window"),
                False,
                "occurrence bypass_desired_window",
            ),
            _optional_string(
                value.get("manual_context_id"),
                "occurrence manual_context_id",
            ),
            _optional_string(value.get("manual_user_id"), "occurrence manual_user_id"),
        )

    def to_store(self) -> dict[str, object]:
        return {
            "occurrence_id": self.occurrence_id,
            "room_id": self.room_id,
            "robot_registry_id": self.robot_registry_id,
            "program": self.program,
            "stages": [stage.to_store() for stage in self.stages],
            "scheduled_at": _iso(self.scheduled_at),
            "created_at": _iso(self.created_at),
            "adapter_id": self.adapter_id,
            "adapter_schema_version": self.adapter_schema_version,
            "current_stage": self.current_stage,
            "source": self.source,
            "manual_mode": self.manual_mode,
            "manual_override": self.manual_override,
            "bypass_desired_window": self.bypass_desired_window,
            "manual_context_id": self.manual_context_id,
            "manual_user_id": self.manual_user_id,
        }

    def to_runtime(self, robot_entity_id: str | None = None) -> dict[str, object]:
        """Add the current transient entity ID to the durable occurrence."""

        return {
            **self.to_store(),
            "robot_entity_id": robot_entity_id or self.robot_entity_id,
        }


@dataclass(slots=True)
class WaterConfirmation:
    request_id: str
    occurrence_id: str
    room_id: str
    robot_registry_id: str
    stage_index: int
    confirm_hash: str
    cancel_hash: str
    tag: str
    sent_at: datetime
    expires_at: datetime
    status: str = "pending"
    responded_at: datetime | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> WaterConfirmation | None:
        request_id = _string(value.get("request_id"))
        occurrence_id = _string(value.get("occurrence_id"))
        room_id = _string(value.get("room_id"))
        robot_registry_id = _string(value.get("robot_registry_id"))
        confirm_hash = _string(value.get("confirm_hash"))
        cancel_hash = _string(value.get("cancel_hash"))
        tag = _string(value.get("tag"))
        sent = _timestamp(value.get("sent_at"))
        expires = _timestamp(value.get("expires_at"))
        if (
            not request_id
            or not occurrence_id
            or not room_id
            or not robot_registry_id
            or not confirm_hash
            or not cancel_hash
            or not tag
            or sent is None
            or expires is None
        ):
            return None
        status = str(value.get("status", "pending"))
        if status not in {"pending", "confirmed", "cancelled", "expired"}:
            status = "pending"
        return cls(
            request_id,
            occurrence_id,
            room_id,
            robot_registry_id,
            max(0, _integer(value.get("stage_index"), 0)),
            confirm_hash,
            cancel_hash,
            tag,
            sent,
            expires,
            status,
            _timestamp(value.get("responded_at")),
        )

    def to_store(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "occurrence_id": self.occurrence_id,
            "room_id": self.room_id,
            "robot_registry_id": self.robot_registry_id,
            "stage_index": self.stage_index,
            "confirm_hash": self.confirm_hash,
            "cancel_hash": self.cancel_hash,
            "tag": self.tag,
            "sent_at": _iso(self.sent_at),
            "expires_at": _iso(self.expires_at),
            "status": self.status,
            "responded_at": _iso(self.responded_at),
        }


@dataclass(slots=True)
class WaterNotificationEpisode:
    room_id: str
    reason: str
    first_sent_at: datetime
    last_sent_at: datetime

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object]
    ) -> WaterNotificationEpisode | None:
        room_id, reason = _string(value.get("room_id")), _string(value.get("reason"))
        first, last = (
            _timestamp(value.get("first_sent_at")),
            _timestamp(value.get("last_sent_at")),
        )
        if not room_id or not reason or first is None or last is None:
            return None
        return cls(room_id, reason, first, last)

    def to_store(self) -> dict[str, object]:
        return {
            "room_id": self.room_id,
            "reason": self.reason,
            "first_sent_at": _iso(self.first_sent_at),
            "last_sent_at": _iso(self.last_sent_at),
        }


@dataclass(slots=True)
class SchedulerFault:
    """Durable scoped dispatch fault without raw vendor or exception data."""

    reason_code: FaultCode
    robot_registry_id: str
    room_area_id: str
    occurred_at: datetime
    phase: str
    native_command_may_have_started: bool = False
    outcome_uncertain: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SchedulerFault | None:
        reason_code = _string(value.get("reason_code"))
        robot_registry_id = _string(value.get("robot_registry_id"))
        room_area_id = _string(value.get("room_area_id"))
        occurred_at = _timestamp(value.get("occurred_at"))
        phase = _string(value.get("phase"))
        if (
            not reason_code
            or not robot_registry_id
            or not room_area_id
            or occurred_at is None
            or not phase
        ):
            return None
        try:
            return cls(
                reason_code=FaultCode(reason_code),
                robot_registry_id=robot_registry_id,
                room_area_id=room_area_id,
                occurred_at=occurred_at,
                phase=phase,
                native_command_may_have_started=bool(
                    value.get("native_command_may_have_started", False)
                ),
                outcome_uncertain=bool(value.get("outcome_uncertain", False)),
            )
        except ValueError:
            return None

    def to_store(self) -> dict[str, object]:
        return {
            "reason_code": str(self.reason_code),
            "robot_registry_id": self.robot_registry_id,
            "room_area_id": self.room_area_id,
            "occurred_at": _iso(self.occurred_at),
            "phase": self.phase,
            "native_command_may_have_started": self.native_command_may_have_started,
            "outcome_uncertain": self.outcome_uncertain,
        }


@dataclass(frozen=True, slots=True)
class RoomRecovery:
    """One interrupted room attempt awaiting explicit retry acknowledgement."""

    recovery_id: str
    room_area_id: str
    robot_registry_id: str
    occurrence_id: str
    stage_index: int
    operation: CleaningOperation
    interrupted_at: datetime
    error_category: str = "robot_error"
    detached_at: datetime | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> RoomRecovery:
        identifiers = {}
        for key in (
            "recovery_id",
            "room_area_id",
            "robot_registry_id",
            "occurrence_id",
        ):
            item = value.get(key)
            if not isinstance(item, str) or not item:
                raise StateSchemaError(f"room recovery {key} is invalid")
            identifiers[key] = item
        index = value.get("stage_index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise StateSchemaError("room recovery stage_index is invalid")
        interrupted = _timestamp(value.get("interrupted_at"))
        detached = _timestamp(value.get("detached_at"))
        if interrupted is None or (
            value.get("detached_at") is not None and detached is None
        ):
            raise StateSchemaError("room recovery timestamp is invalid")
        category = value.get("error_category")
        if not isinstance(category, str) or category not in ROBOT_ERROR_CATEGORIES:
            raise StateSchemaError("room recovery error_category is invalid")
        try:
            operation = CleaningOperation(str(value.get("operation")))
        except ValueError as err:
            raise StateSchemaError("room recovery operation is invalid") from err
        return cls(
            **identifiers,
            stage_index=index,
            operation=operation,
            interrupted_at=interrupted,
            error_category=category,
            detached_at=detached,
        )

    def to_store(self) -> dict[str, object]:
        return {
            "recovery_id": self.recovery_id,
            "room_area_id": self.room_area_id,
            "robot_registry_id": self.robot_registry_id,
            "occurrence_id": self.occurrence_id,
            "stage_index": self.stage_index,
            "operation": str(self.operation),
            "interrupted_at": _iso(self.interrupted_at),
            "error_category": self.error_category,
            "detached_at": _iso(self.detached_at),
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
class RobotCooldown:
    """A short post-cancellation robot hold that leaves room cadence intact."""

    until: datetime
    cancelled_at: datetime
    reason: str = "physical_cancelled"

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> RobotCooldown | None:
        until = _timestamp(value.get("until"))
        cancelled_at = _timestamp(value.get("cancelled_at"))
        if until is None or cancelled_at is None:
            return None
        return cls(
            until=until,
            cancelled_at=cancelled_at,
            reason=str(value.get("reason", "physical_cancelled")),
        )

    def to_store(self) -> dict[str, object]:
        return {
            "until": _iso(self.until),
            "cancelled_at": _iso(self.cancelled_at),
            "reason": self.reason,
        }


@dataclass(slots=True)
class UnresolvedRobotReference:
    """Durable robot-owned records that cannot yet bind to a registry ID."""

    legacy_key: str
    reason: str
    first_seen_at: datetime
    settings: RobotSettings | None = None
    active_job: ActiveJob | None = None
    hold: RobotHold | None = None
    cooldown: RobotCooldown | None = None
    occurrence_room_ids: tuple[str, ...] = ()

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object]
    ) -> UnresolvedRobotReference | None:
        legacy_key = _string(value.get("legacy_key"))
        reason = _string(value.get("reason"))
        first_seen_at = _timestamp(value.get("first_seen_at"))
        if not legacy_key or not reason or first_seen_at is None:
            return None
        raw_settings = value.get("settings")
        raw_active = value.get("active_job")
        raw_hold = value.get("hold")
        raw_cooldown = value.get("cooldown")
        return cls(
            legacy_key=legacy_key,
            reason=reason,
            first_seen_at=first_seen_at,
            settings=(
                RobotSettings.from_mapping(
                    raw_settings,
                    RobotSettings.defaults(False),
                )
                if isinstance(raw_settings, Mapping)
                else None
            ),
            active_job=(
                ActiveJob.from_mapping(raw_active)
                if isinstance(raw_active, Mapping)
                else None
            ),
            hold=(
                RobotHold.from_mapping(raw_hold)
                if isinstance(raw_hold, Mapping)
                else None
            ),
            cooldown=(
                RobotCooldown.from_mapping(raw_cooldown)
                if isinstance(raw_cooldown, Mapping)
                else None
            ),
            occurrence_room_ids=tuple(_string_list(value.get("occurrence_room_ids"))),
        )

    def to_store(self) -> dict[str, object]:
        return {
            "legacy_key": self.legacy_key,
            "reason": self.reason,
            "first_seen_at": _iso(self.first_seen_at),
            "settings": self.settings.to_runtime() if self.settings else None,
            "active_job": self.active_job.to_store() if self.active_job else None,
            "hold": self.hold.to_store() if self.hold else None,
            "cooldown": self.cooldown.to_store() if self.cooldown else None,
            "occurrence_room_ids": list(self.occurrence_room_ids),
        }


@dataclass(frozen=True, slots=True)
class FrozenJsonObject:
    """Immutable JSON object retained only at a Store/presentation boundary."""

    items: tuple[tuple[str, object], ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> FrozenJsonObject:
        return cls(
            tuple(
                sorted(
                    ((str(key), _freeze_json(item)) for key, item in value.items()),
                    key=lambda pair: pair[0],
                )
            )
        )

    def to_mapping(self) -> dict[str, object]:
        return {key: _thaw_json(value) for key, value in self.items}


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return FrozenJsonObject.from_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise StateSchemaError(f"unsupported persisted JSON value: {type(value).__name__}")


def _thaw_json(value: object) -> object:
    if isinstance(value, FrozenJsonObject):
        return value.to_mapping()
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ManualAuditRecord:
    """One typed audit of an explicit or observed manual clean."""

    at: datetime | None = None
    robot_registry_id: str | None = None
    room_ids: tuple[str, ...] = ()
    operations: tuple[CleaningOperation, ...] = ()
    context_id: str | None = None
    user_id: str | None = None
    mode: str | None = None
    source: str | None = None
    outcome: str | None = None
    reason: str | None = None
    confidence: str | None = None
    changed: tuple[str, ...] = ()
    deferred: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ManualAuditRecord:
        return cls(
            at=_timestamp(value.get("at")),
            robot_registry_id=_string(
                value.get("robot_registry_id", value.get("robot"))
            ),
            room_ids=tuple(_string_list(value.get("rooms"))),
            operations=_cleaning_operations(value.get("operations")),
            context_id=_string(value.get("context_id")),
            user_id=_string(value.get("user_id")),
            mode=_string(value.get("mode")),
            source=_string(value.get("source")),
            outcome=_string(value.get("outcome")),
            reason=_string(value.get("reason")),
            confidence=_string(value.get("confidence")),
            changed=tuple(_string_list(value.get("changed"))),
            deferred=tuple(_string_list(value.get("deferred"))),
        )

    def to_store(self) -> dict[str, object]:
        return {
            "at": _iso(self.at),
            "robot_registry_id": self.robot_registry_id,
            "rooms": list(self.room_ids),
            "operations": list(self.operations),
            "context_id": self.context_id,
            "user_id": self.user_id,
            "mode": self.mode,
            "source": self.source,
            "outcome": self.outcome,
            "reason": self.reason,
            "confidence": self.confidence,
            "changed": list(self.changed),
            "deferred": list(self.deferred),
        }


@dataclass(frozen=True, slots=True)
class RecoveryAuditRecord:
    """One typed restart or job-recovery decision."""

    robot_registry_id: str | None = None
    room_ids: tuple[str, ...] = ()
    at: datetime | None = None
    reason: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> RecoveryAuditRecord:
        return cls(
            robot_registry_id=_string(
                value.get("robot_registry_id", value.get("robot"))
            ),
            room_ids=tuple(_string_list(value.get("rooms"))),
            at=_timestamp(value.get("at")),
            reason=_string(value.get("reason")),
        )

    def to_store(self) -> dict[str, object]:
        return {
            "robot_registry_id": self.robot_registry_id,
            "rooms": list(self.room_ids),
            "at": _iso(self.at),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class RoomDecisionRecord:
    """One bounded explanation of a room scheduling decision."""

    at: datetime | None = None
    room_area_id: str | None = None
    reason: str | None = None
    occupancy_source: str | None = None
    required_clear_minutes: int = 0
    clear_minutes: float | None = None
    forecast_confidence: float = 0.0
    comparable_sample_count: int = 0
    forecast_reason: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> RoomDecisionRecord:
        return cls(
            at=_timestamp(value.get("at")),
            room_area_id=_string(value.get("room_area_id")),
            reason=_string(value.get("reason")),
            occupancy_source=_string(value.get("occupancy_source")),
            required_clear_minutes=max(
                0,
                _integer(value.get("required_clear_minutes"), 0),
            ),
            clear_minutes=_optional_number(value.get("clear_minutes")),
            forecast_confidence=_number(value.get("forecast_confidence"), 0),
            comparable_sample_count=max(
                0,
                _integer(value.get("comparable_sample_count"), 0),
            ),
            forecast_reason=_string(value.get("forecast_reason")),
        )

    def to_store(self) -> dict[str, object]:
        return {
            "at": _iso(self.at),
            "room_area_id": self.room_area_id,
            "reason": self.reason,
            "occupancy_source": self.occupancy_source,
            "required_clear_minutes": self.required_clear_minutes,
            "clear_minutes": self.clear_minutes,
            "forecast_confidence": self.forecast_confidence,
            "comparable_sample_count": self.comparable_sample_count,
            "forecast_reason": self.forecast_reason,
        }


@dataclass(slots=True)
class EvaluationState:
    last_evaluation_at: datetime | None = None
    last_preview: FrozenJsonObject = field(default_factory=FrozenJsonObject)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> EvaluationState:
        preview = value.get("last_preview", {})
        return cls(
            last_evaluation_at=_timestamp(
                value.get("last_evaluation_at", value.get("last_evaluation"))
            ),
            last_preview=FrozenJsonObject.from_mapping(preview)
            if isinstance(preview, Mapping)
            else FrozenJsonObject(),
        )

    def to_store(self) -> dict[str, object]:
        return {
            "last_evaluation_at": _iso(self.last_evaluation_at),
            "last_preview": self.last_preview.to_mapping(),
        }


@dataclass(slots=True)
class AuditState:
    manual_events: list[ManualAuditRecord] = field(default_factory=list)
    recovery_events: list[RecoveryAuditRecord] = field(default_factory=list)
    room_decisions: list[RoomDecisionRecord] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> AuditState:
        return cls(
            manual_events=[
                ManualAuditRecord.from_mapping(item)
                for item in _sequence(value.get("manual_events", []))
                if isinstance(item, Mapping)
            ],
            recovery_events=[
                RecoveryAuditRecord.from_mapping(item)
                for item in _sequence(value.get("recovery_events", []))
                if isinstance(item, Mapping)
            ],
            room_decisions=[
                RoomDecisionRecord.from_mapping(item)
                for item in _sequence(value.get("room_decisions", []))
                if isinstance(item, Mapping)
            ],
        )

    def to_store(self) -> dict[str, object]:
        return {
            "manual_events": [item.to_store() for item in self.manual_events],
            "recovery_events": [item.to_store() for item in self.recovery_events],
            "room_decisions": [item.to_store() for item in self.room_decisions],
        }


@dataclass(frozen=True, slots=True)
class FloorPlanRectangle:
    """One room rectangle on a named Home Assistant floor grid."""

    floor_id: str
    x: int
    y: int
    width: int
    height: int

    @classmethod
    def from_mapping(cls, value: object) -> FloorPlanRectangle:
        data = _mapping(value, "floor-plan room rectangle")
        floor_id = data.get("floor_id")
        if not isinstance(floor_id, str) or not floor_id:
            raise StateSchemaError(
                "floor-plan rectangle floor_id must be a non-empty string"
            )
        try:
            return cls(
                floor_id=floor_id,
                x=floor_plan_integer(
                    data.get("x"),
                    "floor-plan rectangle x",
                    0,
                    FLOOR_PLAN_MAX_GRID_COORDINATE,
                ),
                y=floor_plan_integer(
                    data.get("y"),
                    "floor-plan rectangle y",
                    0,
                    FLOOR_PLAN_MAX_GRID_COORDINATE,
                ),
                width=floor_plan_integer(
                    data.get("width"),
                    "floor-plan rectangle width",
                    FLOOR_PLAN_MIN_ROOM_SPAN,
                    FLOOR_PLAN_MAX_GRID_COORDINATE,
                ),
                height=floor_plan_integer(
                    data.get("height"),
                    "floor-plan rectangle height",
                    FLOOR_PLAN_MIN_ROOM_SPAN,
                    FLOOR_PLAN_MAX_GRID_COORDINATE,
                ),
            )
        except ValueError as err:
            raise StateSchemaError(str(err)) from err

    def to_store(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class FloorPlanSensorMarker:
    """A normalized sensor marker kept inside its discovered room rectangle."""

    area_id: str
    x: int
    y: int

    @classmethod
    def from_mapping(cls, value: object) -> FloorPlanSensorMarker:
        data = _mapping(value, "floor-plan sensor marker")
        area_id = data.get("area_id")
        if not isinstance(area_id, str) or not area_id:
            raise StateSchemaError(
                "floor-plan sensor marker area_id must be a non-empty string"
            )
        try:
            return cls(
                area_id=area_id,
                x=floor_plan_integer(data.get("x"), "floor-plan sensor x", 0, 1000),
                y=floor_plan_integer(data.get("y"), "floor-plan sensor y", 0, 1000),
            )
        except ValueError as err:
            raise StateSchemaError(str(err)) from err

    def to_store(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class FloorPlanState:
    """Durable user-authored floor-plan geometry and room topology."""

    revision: int = 0
    rooms: dict[str, FloorPlanRectangle] = field(default_factory=dict)
    edges: set[tuple[str, str]] = field(default_factory=set)
    sensors: dict[str, FloorPlanSensorMarker] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: object) -> FloorPlanState:
        data = _mapping(value, "floor_plan")
        revision = data.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise StateSchemaError("floor-plan revision must be a non-negative integer")
        raw_rooms = _mapping(data.get("rooms"), "floor_plan.rooms")
        raw_sensors = _mapping(data.get("sensors"), "floor_plan.sensors")
        raw_edges = data.get("edges")
        if not isinstance(raw_edges, list):
            raise StateSchemaError("floor_plan.edges must be an array")
        rooms: dict[str, FloorPlanRectangle] = {}
        for area_id, item in raw_rooms.items():
            if not isinstance(area_id, str) or not area_id:
                raise StateSchemaError(
                    "floor-plan room keys must be non-empty area IDs"
                )
            rooms[area_id] = FloorPlanRectangle.from_mapping(item)
        sensors: dict[str, FloorPlanSensorMarker] = {}
        for registry_id, item in raw_sensors.items():
            if not isinstance(registry_id, str) or not registry_id:
                raise StateSchemaError(
                    "floor-plan sensor keys must be non-empty registry IDs"
                )
            sensors[registry_id] = FloorPlanSensorMarker.from_mapping(item)
        edges: set[tuple[str, str]] = set()
        for item in raw_edges:
            if not isinstance(item, list) or len(item) != 2:
                raise StateSchemaError(
                    "each floor-plan edge must contain exactly two area IDs"
                )
            try:
                edge = normalize_floor_plan_edge(item[0], item[1])
            except ValueError as err:
                raise StateSchemaError(str(err)) from err
            if edge in edges or item != [edge[0], edge[1]]:
                raise StateSchemaError(
                    "floor-plan edges must be unique canonical area-ID pairs"
                )
            edges.add(edge)
        return cls(revision=revision, rooms=rooms, edges=edges, sensors=sensors)

    def to_store(self) -> dict[str, object]:
        return {
            "revision": self.revision,
            "rooms": {area_id: room.to_store() for area_id, room in self.rooms.items()},
            "edges": [list(edge) for edge in sorted(self.edges)],
            "sensors": {
                registry_id: marker.to_store()
                for registry_id, marker in self.sensors.items()
            },
        }


@dataclass(slots=True)
class SchedulerState:
    global_settings: GlobalSettings
    floor_plan: FloorPlanState = field(default_factory=FloorPlanState)
    room_settings: dict[str, RoomSettings] = field(default_factory=dict)
    robot_settings: dict[str, RobotSettings] = field(default_factory=dict)
    robot_entity_aliases: dict[str, str] = field(default_factory=dict)
    room_history: dict[str, RoomHistory] = field(default_factory=dict)
    active_jobs: dict[str, ActiveJob | None] = field(default_factory=dict)
    robot_holds: dict[str, RobotHold] = field(default_factory=dict)
    robot_cooldowns: dict[str, RobotCooldown] = field(default_factory=dict)
    audit: AuditState = field(default_factory=AuditState)
    evaluation: EvaluationState = field(default_factory=EvaluationState)
    robot_faults: dict[str, SchedulerFault] = field(default_factory=dict)
    room_faults: dict[str, SchedulerFault] = field(default_factory=dict)
    room_recoveries: dict[str, RoomRecovery] = field(default_factory=dict)
    occurrences: dict[str, CleaningOccurrence] = field(default_factory=dict)
    water_confirmations: dict[str, WaterConfirmation] = field(default_factory=dict)
    water_notification_episodes: dict[str, WaterNotificationEpisode] = field(
        default_factory=dict
    )
    unresolved_robot_references: dict[str, UnresolvedRobotReference] = field(
        default_factory=dict
    )
    first_scheduler_online_at: datetime | None = None

    @classmethod
    def create(cls, entry_data: Mapping[str, object]) -> SchedulerState:
        return cls(global_settings=GlobalSettings.from_entry(entry_data))

    @classmethod
    def from_store(
        cls, payload: object, entry_data: Mapping[str, object]
    ) -> tuple[SchedulerState, bool]:
        """Load current or convert older shapes after validating retained data."""

        if payload is None:
            return cls.create(entry_data), False
        data = _mapping(payload, "stored scheduler state")
        schema_version = data.get("schema_version")
        if schema_version is not None and (
            isinstance(schema_version, bool) or not isinstance(schema_version, int)
        ):
            raise StateSchemaError("schema_version must be an integer")
        if schema_version in {16, 17}:
            # Validate the full former current schema before any permissive
            # legacy parsing. Retired settings remain ignored by that validator.
            upgraded = {**data, "schema_version": SCHEMA_VERSION}
            if schema_version == 16:
                upgraded["room_recoveries"] = {}
            cls._validate_current_schema(upgraded)
            return cls._from_versioned(upgraded, entry_data), True
        if schema_version is None or schema_version == 1:
            return cls._from_v1(data, entry_data), True
        if schema_version in {
            2,
            3,
            4,
            5,
            6,
            7,
            8,
            9,
            10,
            11,
            12,
            13,
            14,
            15,
        }:
            return cls._from_versioned(data, entry_data), True
        if schema_version != SCHEMA_VERSION:
            raise StateSchemaError(
                f"unsupported scheduler state schema: {schema_version!r}"
            )
        cls._validate_current_schema(data)
        retired_settings = RETIRED_GLOBAL_SETTINGS.intersection(
            _mapping(data.get("global"), "global")
        )
        retired_hold_fields = any(
            isinstance(hold, Mapping) and "requested_map_id" in hold
            for hold in (
                *_mapping(data.get("robot_holds"), "robot_holds").values(),
                *(
                    _mapping(reference, "unresolved reference").get("hold")
                    for reference in _mapping(
                        data.get("unresolved_robot_references"),
                        "unresolved_robot_references",
                    ).values()
                ),
            )
        )
        return cls._from_versioned(data, entry_data), bool(
            retired_settings or retired_hold_fields
        )

    @classmethod
    def _validate_current_schema(cls, data: Mapping[str, object]) -> None:
        """Reject malformed current records instead of silently dropping them."""

        if data.get("schema_version") != SCHEMA_VERSION:
            raise StateSchemaError(f"schema_version must be {SCHEMA_VERSION}")

        room_settings = _mapping(data.get("room_settings"), "room_settings")
        robot_settings = _mapping(data.get("robot_settings"), "robot_settings")
        room_history = _mapping(data.get("room_history"), "room_history")
        active_jobs = _mapping(data.get("active_jobs"), "active_jobs")
        robot_holds = _mapping(data.get("robot_holds"), "robot_holds")
        robot_cooldowns = _mapping(data.get("robot_cooldowns"), "robot_cooldowns")
        robot_faults = _mapping(data.get("robot_faults"), "robot_faults")
        room_faults = _mapping(data.get("room_faults"), "room_faults")
        recoveries = _mapping(data.get("room_recoveries"), "room_recoveries")
        occurrences = _mapping(data.get("occurrences"), "occurrences")
        confirmations = _mapping(data.get("water_confirmations"), "water_confirmations")
        episodes = _mapping(
            data.get("water_notification_episodes"),
            "water_notification_episodes",
        )
        aliases = _mapping(data.get("robot_entity_aliases"), "robot_entity_aliases")
        unresolved = _mapping(
            data.get("unresolved_robot_references"),
            "unresolved_robot_references",
        )
        _mapping(data.get("global"), "global")
        FloorPlanState.from_mapping(_mapping(data.get("floor_plan"), "floor_plan"))
        audit = _mapping(data.get("audit"), "audit")
        evaluation = _mapping(data.get("evaluation"), "evaluation")

        for name, section in (
            ("room_settings", room_settings),
            ("robot_settings", robot_settings),
            ("room_history", room_history),
            ("active_jobs", active_jobs),
            ("robot_holds", robot_holds),
            ("robot_cooldowns", robot_cooldowns),
            ("robot_faults", robot_faults),
            ("room_faults", room_faults),
            ("room_recoveries", recoveries),
            ("occurrences", occurrences),
            ("water_confirmations", confirmations),
            ("water_notification_episodes", episodes),
            ("robot_entity_aliases", aliases),
            ("unresolved_robot_references", unresolved),
        ):
            if any(not isinstance(key, str) or not key for key in section):
                raise StateSchemaError(f"{name} keys must be non-empty strings")

        def require_records(
            name: str,
            section: Mapping[str, object],
            parser: Callable[[Mapping[str, object]], object | None],
            *,
            allow_none: bool = False,
        ) -> None:
            for key, value in section.items():
                if value is None and allow_none:
                    continue
                if not isinstance(value, Mapping):
                    raise StateSchemaError(f"{name}.{key} must be an object")
                if parser(value) is None:
                    raise StateSchemaError(f"{name}.{key} is invalid")

        require_records(
            "room_settings",
            room_settings,
            lambda value: RoomSettings.from_mapping(
                value, RoomSettings.defaults(False)
            ),
        )
        require_records(
            "robot_settings",
            robot_settings,
            lambda value: RobotSettings.from_mapping(
                value, RobotSettings.defaults(False)
            ),
        )
        require_records("room_history", room_history, RoomHistory.from_mapping)
        require_records(
            "active_jobs",
            active_jobs,
            ActiveJob.from_mapping,
            allow_none=True,
        )
        require_records("robot_holds", robot_holds, RobotHold.from_mapping)
        require_records("robot_cooldowns", robot_cooldowns, RobotCooldown.from_mapping)
        require_records("robot_faults", robot_faults, SchedulerFault.from_mapping)
        require_records("room_faults", room_faults, SchedulerFault.from_mapping)
        require_records("room_recoveries", recoveries, RoomRecovery.from_mapping)
        for area_id, value in recoveries.items():
            recovery = RoomRecovery.from_mapping(_mapping(value, "room recovery"))
            if recovery.room_area_id != area_id:
                raise StateSchemaError("room recovery identity does not match its key")
        require_records("occurrences", occurrences, CleaningOccurrence.from_mapping)
        require_records(
            "water_confirmations", confirmations, WaterConfirmation.from_mapping
        )
        require_records(
            "water_notification_episodes",
            episodes,
            WaterNotificationEpisode.from_mapping,
        )
        require_records(
            "unresolved_robot_references",
            unresolved,
            UnresolvedRobotReference.from_mapping,
        )

        def require_enum(
            path: str,
            value: object,
            enum_type: type[CleaningOperation]
            | type[CleaningProgram]
            | type[JobPhase]
            | type[JobSource]
            | type[OccurrenceSource]
            | type[StageStatus],
            *,
            optional: bool = False,
        ) -> None:
            if value is None and optional:
                return
            if not isinstance(value, str):
                raise StateSchemaError(f"{path} is invalid")
            try:
                enum_type(value)
            except (TypeError, ValueError) as err:
                raise StateSchemaError(f"{path} is invalid") from err

        for key, value in room_settings.items():
            record = _mapping(value, f"room_settings.{key}")
            require_enum(
                f"room_settings.{key}.cleaning_program",
                record.get("cleaning_program"),
                CleaningProgram,
                optional=True,
            )
        for key, value in robot_settings.items():
            record = _mapping(value, f"robot_settings.{key}")
            require_enum(
                f"robot_settings.{key}.cleaning_program",
                record.get("cleaning_program"),
                CleaningProgram,
                optional=True,
            )
        for key, value in active_jobs.items():
            if value is None:
                continue
            record = _mapping(value, f"active_jobs.{key}")
            require_enum(
                f"active_jobs.{key}.operation",
                record.get("operation"),
                CleaningOperation,
            )
            require_enum(
                f"active_jobs.{key}.phase",
                record.get("phase"),
                JobPhase,
            )
            require_enum(
                f"active_jobs.{key}.source",
                record.get("source"),
                JobSource,
            )
            requested = record.get("requested_operations", [])
            if not isinstance(requested, list):
                raise StateSchemaError(
                    f"active_jobs.{key}.requested_operations must be an array"
                )
            for index, operation in enumerate(requested):
                require_enum(
                    f"active_jobs.{key}.requested_operations.{index}",
                    operation,
                    CleaningOperation,
                )
        for key, value in occurrences.items():
            record = _mapping(value, f"occurrences.{key}")
            require_enum(
                f"occurrences.{key}.program",
                record.get("program"),
                CleaningProgram,
            )
            require_enum(
                f"occurrences.{key}.source",
                record.get("source"),
                OccurrenceSource,
            )
            stages = record.get("stages")
            if not isinstance(stages, list) or not stages:
                raise StateSchemaError(f"occurrences.{key}.stages is invalid")
            for index, raw_stage in enumerate(stages):
                stage = _mapping(
                    raw_stage,
                    f"occurrences.{key}.stages.{index}",
                )
                require_enum(
                    f"occurrences.{key}.stages.{index}.operation",
                    stage.get("operation"),
                    CleaningOperation,
                )
                require_enum(
                    f"occurrences.{key}.stages.{index}.status",
                    stage.get("status"),
                    StageStatus,
                )
        if any(not isinstance(alias, str) for alias in aliases.values()):
            raise StateSchemaError("robot_entity_aliases values must be strings")
        audit_sections: dict[str, list[object]] = {}
        for name in ("manual_events", "recovery_events", "room_decisions"):
            values = audit.get(name)
            if not isinstance(values, list) or any(
                not isinstance(value, Mapping) for value in values
            ):
                raise StateSchemaError(f"audit.{name} must contain objects")
            audit_sections[name] = values
        for index, value in enumerate(audit_sections["manual_events"]):
            record = _mapping(value, f"audit.manual_events.{index}")
            if "robot" in record:
                raise StateSchemaError(
                    f"audit.manual_events.{index}.robot is a legacy entity ID"
                )
            robot_registry_id = record.get("robot_registry_id")
            if robot_registry_id is not None and (
                not isinstance(robot_registry_id, str) or not robot_registry_id
            ):
                raise StateSchemaError(
                    f"audit.manual_events.{index}.robot_registry_id is invalid"
                )
            operations = record.get("operations")
            if not isinstance(operations, list):
                raise StateSchemaError(
                    f"audit.manual_events.{index}.operations must be an array"
                )
            for operation_index, operation in enumerate(operations):
                require_enum(
                    f"audit.manual_events.{index}.operations.{operation_index}",
                    operation,
                    CleaningOperation,
                )
            ManualAuditRecord.from_mapping(record)
        for index, value in enumerate(audit_sections["recovery_events"]):
            record = _mapping(value, f"audit.recovery_events.{index}")
            if "robot" in record:
                raise StateSchemaError(
                    f"audit.recovery_events.{index}.robot is a legacy entity ID"
                )
            robot_registry_id = record.get("robot_registry_id")
            if robot_registry_id is not None and (
                not isinstance(robot_registry_id, str) or not robot_registry_id
            ):
                raise StateSchemaError(
                    f"audit.recovery_events.{index}.robot_registry_id is invalid"
                )
            RecoveryAuditRecord.from_mapping(record)
        for index, value in enumerate(audit_sections["room_decisions"]):
            RoomDecisionRecord.from_mapping(
                _mapping(value, f"audit.room_decisions.{index}")
            )
        for key, value in confirmations.items():
            record = _mapping(value, f"water_confirmations.{key}")
            if record.get("status") not in {
                "pending",
                "confirmed",
                "cancelled",
                "expired",
            }:
                raise StateSchemaError(f"water_confirmations.{key}.status is invalid")
        if not isinstance(evaluation.get("last_preview"), Mapping):
            raise StateSchemaError("evaluation.last_preview must be an object")

    @classmethod
    def _from_v1(
        cls, data: Mapping[str, object], entry_data: Mapping[str, object]
    ) -> SchedulerState:
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
        raw_occurrences = _mapping_or_empty(data.get("occurrences"))
        raw_recoveries = _mapping_or_empty(data.get("room_recoveries"))
        raw_confirmations = _mapping_or_empty(data.get("water_confirmations"))
        raw_episodes = _mapping_or_empty(data.get("water_notification_episodes"))
        raw_robot_faults = _mapping_or_empty(data.get("robot_faults"))
        raw_room_faults = _mapping_or_empty(data.get("room_faults"))
        raw_floor_plan = data.get("floor_plan")
        legacy_fault = (
            SchedulerFault.from_mapping(value)
            if isinstance((value := data.get("scheduler_fault")), Mapping)
            else None
        )
        return cls(
            global_settings=global_settings,
            floor_plan=(
                FloorPlanState.from_mapping(raw_floor_plan)
                if isinstance(raw_floor_plan, Mapping)
                else FloorPlanState()
            ),
            room_settings={
                area_id: RoomSettings.from_mapping(value, RoomSettings.defaults(False))
                for area_id, value in raw_room_settings.items()
                if isinstance(area_id, str) and isinstance(value, Mapping)
            },
            robot_settings={
                entity_id: RobotSettings.from_mapping(
                    value, RobotSettings.defaults(False)
                )
                for entity_id, value in raw_robot_settings.items()
                if isinstance(entity_id, str) and isinstance(value, Mapping)
            },
            room_history=rooms,
            active_jobs={
                entity_id: ActiveJob.from_mapping(value)
                if isinstance(value, Mapping)
                else None
                for entity_id, value in _mapping_or_empty(data.get("active")).items()
                if isinstance(entity_id, str)
            },
            robot_holds={
                entity_id: hold
                for entity_id, value in _mapping_or_empty(
                    data.get("robot_holds")
                ).items()
                if isinstance(entity_id, str)
                and isinstance(value, Mapping)
                and (hold := RobotHold.from_mapping(value)) is not None
            },
            robot_cooldowns={
                entity_id: cooldown
                for entity_id, value in _mapping_or_empty(
                    data.get("robot_cooldowns")
                ).items()
                if isinstance(entity_id, str)
                and isinstance(value, Mapping)
                and (cooldown := RobotCooldown.from_mapping(value)) is not None
            },
            audit=AuditState.from_mapping(data),
            evaluation=EvaluationState.from_mapping(data),
            robot_faults={
                registry_id: fault
                for registry_id, value in raw_robot_faults.items()
                if isinstance(registry_id, str)
                and isinstance(value, Mapping)
                and (fault := SchedulerFault.from_mapping(value)) is not None
                and fault.robot_registry_id == registry_id
            }
            or (
                {legacy_fault.robot_registry_id: legacy_fault}
                if legacy_fault is not None
                else {}
            ),
            room_faults={
                area_id: fault
                for area_id, value in raw_room_faults.items()
                if isinstance(area_id, str)
                and isinstance(value, Mapping)
                and (fault := SchedulerFault.from_mapping(value)) is not None
                and fault.room_area_id == area_id
            },
            occurrences={
                area_id: occurrence
                for area_id, value in raw_occurrences.items()
                if isinstance(area_id, str)
                and isinstance(value, Mapping)
                and (occurrence := CleaningOccurrence.from_mapping(value)) is not None
            },
            room_recoveries={
                area_id: RoomRecovery.from_mapping(value)
                for area_id, value in raw_recoveries.items()
                if isinstance(area_id, str) and isinstance(value, Mapping)
            },
            robot_entity_aliases={
                key: alias
                for key, alias in _mapping_or_empty(
                    data.get("robot_entity_aliases")
                ).items()
                if isinstance(key, str) and isinstance(alias, str)
            },
            water_confirmations={
                occurrence_id: confirmation
                for occurrence_id, value in raw_confirmations.items()
                if isinstance(occurrence_id, str)
                and isinstance(value, Mapping)
                and (confirmation := WaterConfirmation.from_mapping(value)) is not None
            },
            water_notification_episodes={
                area_id: episode
                for area_id, value in raw_episodes.items()
                if isinstance(area_id, str)
                and isinstance(value, Mapping)
                and (episode := WaterNotificationEpisode.from_mapping(value))
                is not None
            },
            first_scheduler_online_at=_timestamp(data.get("first_scheduler_online_at")),
        )

    @classmethod
    def _from_versioned(
        cls, data: Mapping[str, object], entry_data: Mapping[str, object]
    ) -> SchedulerState:
        defaults = GlobalSettings.from_entry(entry_data)
        raw_global = _mapping(data.get("global"), "global")
        raw_floor_plan = (
            _mapping(data.get("floor_plan"), "floor_plan")
            if data.get("schema_version") in {15, SCHEMA_VERSION}
            else None
        )
        raw_room_settings = _mapping(data.get("room_settings"), "room_settings")
        raw_robot_settings = _mapping(data.get("robot_settings"), "robot_settings")
        raw_robot_aliases = (
            _mapping(data.get("robot_entity_aliases"), "robot_entity_aliases")
            if data.get("schema_version")
            in {
                10,
                11,
                12,
                13,
                14,
                15,
                SCHEMA_VERSION,
            }
            else _mapping_or_empty(data.get("robot_entity_aliases"))
        )
        raw_history = _mapping(data.get("room_history"), "room_history")
        raw_active = _mapping(data.get("active_jobs"), "active_jobs")
        raw_holds = _mapping(data.get("robot_holds"), "robot_holds")
        raw_cooldowns = _mapping_or_empty(data.get("robot_cooldowns"))
        raw_audit = _mapping(data.get("audit"), "audit")
        raw_evaluation = _mapping(data.get("evaluation"), "evaluation")
        raw_occurrences = _mapping_or_empty(data.get("occurrences"))
        raw_recoveries = _mapping_or_empty(data.get("room_recoveries"))
        raw_confirmations = _mapping_or_empty(data.get("water_confirmations"))
        raw_episodes = _mapping_or_empty(data.get("water_notification_episodes"))
        if data.get("schema_version") in {
            10,
            11,
            12,
            13,
            14,
            15,
            SCHEMA_VERSION,
        }:
            raw_robot_faults = _mapping(data.get("robot_faults"), "robot_faults")
            raw_room_faults = _mapping(data.get("room_faults"), "room_faults")
        else:
            legacy_fault = (
                SchedulerFault.from_mapping(value)
                if isinstance((value := data.get("scheduler_fault")), Mapping)
                else None
            )
            raw_robot_faults = (
                {legacy_fault.robot_registry_id: legacy_fault.to_store()}
                if legacy_fault is not None
                else {}
            )
            raw_room_faults = {}
        raw_unresolved_references = (
            _mapping(
                data.get("unresolved_robot_references"),
                "unresolved_robot_references",
            )
            if data.get("schema_version") == SCHEMA_VERSION
            else {}
        )
        return cls(
            global_settings=GlobalSettings.from_mapping(raw_global, defaults),
            floor_plan=(
                FloorPlanState.from_mapping(raw_floor_plan)
                if raw_floor_plan is not None
                else FloorPlanState()
            ),
            room_settings={
                area_id: RoomSettings.from_mapping(value, RoomSettings.defaults(False))
                for area_id, value in raw_room_settings.items()
                if isinstance(area_id, str) and isinstance(value, Mapping)
            },
            robot_settings={
                entity_id: RobotSettings.from_mapping(
                    value, RobotSettings.defaults(False)
                )
                for entity_id, value in raw_robot_settings.items()
                if isinstance(entity_id, str) and isinstance(value, Mapping)
            },
            room_history={
                area_id: RoomHistory.from_mapping(value)
                for area_id, value in raw_history.items()
                if isinstance(area_id, str) and isinstance(value, Mapping)
            },
            active_jobs={
                entity_id: ActiveJob.from_mapping(value)
                if isinstance(value, Mapping)
                else None
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
            robot_cooldowns={
                entity_id: cooldown
                for entity_id, value in raw_cooldowns.items()
                if isinstance(entity_id, str)
                and isinstance(value, Mapping)
                and (cooldown := RobotCooldown.from_mapping(value)) is not None
            },
            audit=AuditState.from_mapping(raw_audit),
            evaluation=EvaluationState.from_mapping(raw_evaluation),
            robot_faults={
                registry_id: fault
                for registry_id, value in raw_robot_faults.items()
                if isinstance(registry_id, str)
                and isinstance(value, Mapping)
                and (fault := SchedulerFault.from_mapping(value)) is not None
                and fault.robot_registry_id == registry_id
            },
            room_faults={
                area_id: fault
                for area_id, value in raw_room_faults.items()
                if isinstance(area_id, str)
                and isinstance(value, Mapping)
                and (fault := SchedulerFault.from_mapping(value)) is not None
                and fault.room_area_id == area_id
            },
            occurrences={
                area_id: occurrence
                for area_id, value in raw_occurrences.items()
                if isinstance(area_id, str)
                and isinstance(value, Mapping)
                and (occurrence := CleaningOccurrence.from_mapping(value)) is not None
            },
            robot_entity_aliases={
                key: alias
                for key, alias in raw_robot_aliases.items()
                if isinstance(key, str) and isinstance(alias, str)
            },
            room_recoveries={
                area_id: RoomRecovery.from_mapping(value)
                for area_id, value in raw_recoveries.items()
                if isinstance(area_id, str) and isinstance(value, Mapping)
            },
            water_confirmations={
                occurrence_id: confirmation
                for occurrence_id, value in raw_confirmations.items()
                if isinstance(occurrence_id, str)
                and isinstance(value, Mapping)
                and (confirmation := WaterConfirmation.from_mapping(value)) is not None
            },
            water_notification_episodes={
                area_id: episode
                for area_id, value in raw_episodes.items()
                if isinstance(area_id, str)
                and isinstance(value, Mapping)
                and (episode := WaterNotificationEpisode.from_mapping(value))
                is not None
            },
            unresolved_robot_references={
                legacy_key: reference
                for legacy_key, value in raw_unresolved_references.items()
                if isinstance(legacy_key, str)
                and isinstance(value, Mapping)
                and (reference := UnresolvedRobotReference.from_mapping(value))
                is not None
                and reference.legacy_key == legacy_key
            },
            first_scheduler_online_at=_timestamp(data.get("first_scheduler_online_at")),
        )

    def ensure_room(
        self, area_id: str, is_bedroom: bool
    ) -> tuple[RoomSettings, RoomHistory]:
        settings = self.room_settings.setdefault(
            area_id, RoomSettings.defaults(is_bedroom)
        )
        history = self.room_history.setdefault(area_id, RoomHistory())
        return settings, history

    def ensure_robot(self, registry_id: str, supports_mopping: bool) -> RobotSettings:
        """Ensure state exists under a durable entity-registry identity."""

        self.active_jobs.setdefault(registry_id, None)
        return self.robot_settings.setdefault(
            registry_id,
            RobotSettings.defaults(supports_mopping),
        )

    def to_store(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "global": asdict(self.global_settings),
            "floor_plan": self.floor_plan.to_store(),
            "room_settings": {
                area_id: settings.to_store()
                for area_id, settings in self.room_settings.items()
            },
            "robot_settings": {
                entity_id: asdict(settings)
                for entity_id, settings in self.robot_settings.items()
            },
            "room_history": {
                area_id: history.to_store()
                for area_id, history in self.room_history.items()
            },
            "active_jobs": {
                entity_id: job.to_store() if job else None
                for entity_id, job in self.active_jobs.items()
            },
            "robot_holds": {
                entity_id: hold.to_store()
                for entity_id, hold in self.robot_holds.items()
            },
            "robot_cooldowns": {
                entity_id: cooldown.to_store()
                for entity_id, cooldown in self.robot_cooldowns.items()
            },
            "audit": self.audit.to_store(),
            "evaluation": self.evaluation.to_store(),
            "robot_faults": {
                registry_id: fault.to_store()
                for registry_id, fault in self.robot_faults.items()
            },
            "room_faults": {
                area_id: fault.to_store() for area_id, fault in self.room_faults.items()
            },
            "room_recoveries": {
                area_id: recovery.to_store()
                for area_id, recovery in self.room_recoveries.items()
            },
            "occurrences": {
                area_id: occurrence.to_store()
                for area_id, occurrence in self.occurrences.items()
            },
            "robot_entity_aliases": dict(self.robot_entity_aliases),
            "water_confirmations": {
                occurrence_id: confirmation.to_store()
                for occurrence_id, confirmation in self.water_confirmations.items()
            },
            "water_notification_episodes": {
                area_id: episode.to_store()
                for area_id, episode in self.water_notification_episodes.items()
            },
            "unresolved_robot_references": {
                legacy_key: reference.to_store()
                for legacy_key, reference in self.unresolved_robot_references.items()
            },
            "first_scheduler_online_at": _iso(self.first_scheduler_online_at),
        }

    def encode(self) -> dict[str, object]:
        """Serialize and validate a complete scheduler payload atomically."""

        payload = self.to_store()
        self._validate_current_schema(payload)
        return payload


def migrate_robot_identity(
    state: SchedulerState,
    current_entities: Mapping[str, str],
    prior_entities: Mapping[str, str] | None = None,
) -> bool:
    """Bind every durable robot-owned record to an entity-registry ID.

    Entity IDs are accepted only as legacy aliases or live lookup values. The
    returned aggregate always keeps settings, jobs, holds, cooldowns, samples,
    and occurrences keyed by stable registry identity.
    """

    changed = False
    prior_entities = prior_entities or {}
    key_to_registry = {
        entity_id: registry_id for registry_id, entity_id in current_entities.items()
    }
    key_to_registry.update(
        {
            entity_id: registry_id
            for registry_id, entity_id in prior_entities.items()
            if registry_id in current_entities
        }
    )
    for registry_id, alias in state.robot_entity_aliases.items():
        if registry_id in current_entities:
            key_to_registry[alias] = registry_id
    for occurrence in state.occurrences.values():
        if (
            occurrence.robot_registry_id in current_entities
            and occurrence.robot_entity_id
        ):
            key_to_registry[occurrence.robot_entity_id] = occurrence.robot_registry_id

    for registry_id, entity_id in current_entities.items():
        if registry_id not in state.robot_entity_aliases:
            legacy_keys = [
                key
                for key, mapped_registry in key_to_registry.items()
                if mapped_registry == registry_id
                and key != entity_id
                and key in state.robot_settings
            ]
            state.robot_entity_aliases[registry_id] = (
                legacy_keys[0] if legacy_keys else entity_id
            )
            changed = True
        key_to_registry[state.robot_entity_aliases[registry_id]] = registry_id

    def rekey(section: dict[str, Any]) -> None:
        nonlocal changed
        rebound: dict[str, Any] = {}
        for key, value in section.items():
            registry_id = (
                key if key in current_entities else key_to_registry.get(key, key)
            )
            if registry_id not in rebound or rebound[registry_id] is None:
                rebound[registry_id] = value
            if registry_id != key:
                changed = True
        section.clear()
        section.update(rebound)

    rekey(state.robot_settings)
    rekey(state.active_jobs)
    rekey(state.robot_holds)
    rekey(state.robot_cooldowns)

    for history in state.room_history.values():
        for sample in history.duration_samples:
            sample_registry_id = key_to_registry.get(sample.robot_registry_id)
            if (
                sample_registry_id is not None
                and sample.robot_registry_id != sample_registry_id
            ):
                sample.robot_registry_id = sample_registry_id
                changed = True

    for occurrence in state.occurrences.values():
        occurrence_registry_id = (
            occurrence.robot_registry_id
            if occurrence.robot_registry_id in current_entities
            else key_to_registry.get(occurrence.robot_registry_id)
            or (
                key_to_registry.get(occurrence.robot_entity_id)
                if occurrence.robot_entity_id
                else None
            )
        )
        if occurrence_registry_id is None:
            continue
        if occurrence.robot_registry_id != occurrence_registry_id:
            occurrence.robot_registry_id = occurrence_registry_id
            changed = True
        current_entity_id = current_entities[occurrence_registry_id]
        if occurrence.robot_entity_id != current_entity_id:
            occurrence.robot_entity_id = current_entity_id
            changed = True

    audit_identity_changed = any(
        record.robot_registry_id in key_to_registry
        and key_to_registry[record.robot_registry_id] != record.robot_registry_id
        for record in state.audit.manual_events
        if record.robot_registry_id
    ) or any(
        record.robot_registry_id in key_to_registry
        and key_to_registry[record.robot_registry_id] != record.robot_registry_id
        for record in state.audit.recovery_events
        if record.robot_registry_id
    )

    def migrate_audit_record[T: (ManualAuditRecord, RecoveryAuditRecord)](
        record: T,
    ) -> T:
        legacy_key = record.robot_registry_id
        if not legacy_key:
            return record
        audit_registry_id = key_to_registry.get(legacy_key)
        if audit_registry_id is None or audit_registry_id == legacy_key:
            return record
        return replace(record, robot_registry_id=audit_registry_id)

    state.audit.manual_events = [
        migrate_audit_record(record) for record in state.audit.manual_events
    ]
    state.audit.recovery_events = [
        migrate_audit_record(record) for record in state.audit.recovery_events
    ]
    if audit_identity_changed:
        changed = True
    return changed


def _mapping_or_empty(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _optional_number(value: object) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return None
    try:
        return float(value)
    except TypeError, ValueError:
        return None
