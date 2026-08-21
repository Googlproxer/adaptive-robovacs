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
    DEFAULT_UNRESOLVED_END,
    DEFAULT_UNRESOLVED_START,
    DEFAULT_BEDROOM_INTERVAL,
    DEFAULT_COMMON_INTERVAL,
    DEFAULT_EXPECTED_MINUTES,
)
from .models import is_valid_daily_time


SCHEMA_VERSION = 10
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
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bounded_number(
    value: object,
    default: float,
    name: str,
    minimum: float,
    maximum: float,
) -> float:
    """Decode one persisted bounded number without hiding corruption."""

    try:
        parsed = float(value if value is not None else default)
    except (TypeError, ValueError) as err:
        raise StateSchemaError(f"{name} must be a number") from err
    if not minimum <= parsed <= maximum:
        raise StateSchemaError(
            f"{name} must be between {minimum:g} and {maximum:g}"
        )
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
    if set(value) - allowed or any(item not in {"room", "robot"} for item in value.values()):
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


def migrate_runtime_robot_identity(
    data: dict[str, Any],
    current_entities: Mapping[str, str],
    prior_entities: Mapping[str, str] | None = None,
) -> bool:
    """Migrate runtime robot keys to registry IDs and current entity IDs.

    ``current_entities`` and ``prior_entities`` map registry ID to entity ID.
    Settings and learned samples become registry-keyed, while active jobs and
    holds remain current-entity-keyed in memory for authoritative observations.
    """

    changed = False
    prior_entities = prior_entities or {}
    aliases = data.setdefault("robot_entity_aliases", {})
    key_to_registry = {
        entity_id: registry_id
        for registry_id, entity_id in current_entities.items()
    }
    key_to_registry.update(
        {
            entity_id: registry_id
            for registry_id, entity_id in prior_entities.items()
            if registry_id in current_entities
        }
    )
    for registry_id, alias in tuple(aliases.items()):
        if registry_id in current_entities and isinstance(alias, str):
            key_to_registry[alias] = registry_id
    for occurrence in data.get("occurrences", {}).values():
        registry_id = occurrence.get("robot_registry_id")
        legacy_entity_id = occurrence.get("robot_entity_id")
        if registry_id in current_entities and isinstance(legacy_entity_id, str):
            key_to_registry[legacy_entity_id] = registry_id

    settings = data["settings"]["robots"]
    unresolved_keys = [
        key
        for key in settings
        if key not in current_entities and key not in key_to_registry
    ]
    unmatched_registries = [
        registry_id
        for registry_id, entity_id in current_entities.items()
        if registry_id not in settings
        and entity_id not in settings
        and aliases.get(registry_id) not in settings
    ]
    if len(unresolved_keys) == len(unmatched_registries) == 1:
        key_to_registry[unresolved_keys[0]] = unmatched_registries[0]

    for registry_id, entity_id in current_entities.items():
        if registry_id not in aliases:
            legacy_keys = [
                key
                for key, mapped_registry in key_to_registry.items()
                if mapped_registry == registry_id
                and key != entity_id
                and key in settings
            ]
            aliases[registry_id] = legacy_keys[0] if legacy_keys else entity_id
            changed = True
        key_to_registry[str(aliases[registry_id])] = registry_id

    for key in tuple(settings):
        registry_id = key_to_registry.get(key)
        if registry_id is None or key == registry_id:
            continue
        if registry_id not in settings:
            settings[registry_id] = settings[key]
        settings.pop(key, None)
        changed = True

    for detail in data.get("rooms", {}).values():
        for sample in detail.get("duration_samples", []):
            old_key = sample.get("robot")
            if (registry_id := key_to_registry.get(old_key)) is not None:
                if old_key != registry_id:
                    sample["robot"] = registry_id
                    changed = True

    for section in ("active", "robot_holds"):
        current_values = data.get(section, {})
        rebound: dict[str, Any] = {}
        for key, value in current_values.items():
            registry_id = (
                key if key in current_entities else key_to_registry.get(key)
            )
            runtime_key = current_entities.get(registry_id, key)
            if runtime_key not in rebound or rebound[runtime_key] is None:
                rebound[runtime_key] = value
            if runtime_key != key:
                changed = True
        data[section] = rebound

    for occurrence in data.get("occurrences", {}).values():
        registry_id = occurrence.get("robot_registry_id")
        entity_id = current_entities.get(registry_id)
        if entity_id and occurrence.get("robot_entity_id") != entity_id:
            occurrence["robot_entity_id"] = entity_id
            changed = True
    return changed


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
        hall_start = _daily_time(
            value.get("hall_start"), defaults.hall_start, "global hall_start"
        )
        hall_end = _daily_time(
            value.get("hall_end"), defaults.hall_end, "global hall_end"
        )
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
            hall_start=hall_start,
            hall_end=hall_end,
            unresolved_start=unresolved_start,
            unresolved_end=unresolved_end,
        )


@dataclass(slots=True)
class RoomSettings:
    enabled: bool
    cleaning_interval: float = DEFAULT_COMMON_INTERVAL
    expected_minutes: float = DEFAULT_EXPECTED_MINUTES
    ignore_desired_window: bool = False
    desired_window_start: str | None = None
    desired_window_end: str | None = None
    cleaning_program: str | None = None
    vacuum_pass_count: int | None = None
    mop_pass_count: int | None = None
    fan_speed: str | None = None
    mode: str | None = None
    mop_mode: str | None = None
    mop_intensity: str | None = None
    cleaning_depth: str | None = None

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
            cleaning_interval=DEFAULT_BEDROOM_INTERVAL if is_bedroom else DEFAULT_COMMON_INTERVAL,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object], default: RoomSettings) -> RoomSettings:
        raw_window = value.get("daily_window")
        if raw_window is not None:
            window = _mapping(raw_window, "room daily_window")
            if window.get("version") != DAILY_WINDOW_VERSION:
                raise StateSchemaError(
                    f"unsupported room daily-window version: {window.get('version')!r}"
                )
        else:
            window = {}
        raw_start = value.get("desired_window_start", window.get("start"))
        raw_end = value.get("desired_window_end", window.get("end"))
        return cls(
            enabled=bool(value.get("enabled", default.enabled)),
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
            desired_window_end=_optional_daily_time(
                raw_end, "room daily-window end"
            ),
            cleaning_program=(
                str(program)
                if (program := value.get("cleaning_program"))
                in {"vacuum_only", "mop_only", "vacuum_then_mop", "mop_then_vacuum"}
                else None
            ),
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
        )

    def to_store(self) -> dict[str, object]:
        """Encode the first versioned daily-window schedule shape."""

        return {
            "enabled": self.enabled,
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
        }


@dataclass(slots=True)
class RobotSettings:
    enabled: bool = True
    minimum_battery: float = DEFAULT_MINIMUM_BATTERY
    cleaning_program: str = "vacuum_only"
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
            cleaning_program=("vacuum_then_mop" if supports_mopping else "vacuum_only")
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object], default: RobotSettings) -> RobotSettings:
        raw_program = value.get("cleaning_program")
        program = (
            str(raw_program)
            if raw_program
            in {"vacuum_only", "mop_only", "vacuum_then_mop", "mop_then_vacuum"}
            else (
                "vacuum_then_mop"
                if bool(value.get("mopping_enabled", default.cleaning_program != "vacuum_only"))
                else "vacuum_only"
            )
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
        value["mopping_enabled"] = self.cleaning_program != "vacuum_only"
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
    cleaning_completed_at: datetime | None = None
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
    last_stage_outcome: str | None = None
    last_stage_reason: str | None = None
    last_stage_at: datetime | None = None

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
        vacuum_completed = _timestamp(value.get("vacuum_completed_at", value.get("vacuum")))
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
                (item for key, item in deferrals.items() if key in {"vacuum", "mop"}),
                default=None,
            )
            if legacy_deferral:
                deferrals["cleaning"] = legacy_deferral
        return cls(
            cleaning_completed_at=cleaning_completed,
            vacuum_completed_at=vacuum_completed,
            mop_completed_at=mop_completed,
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
            last_stage_outcome=_string(value.get("last_stage_outcome")),
            last_stage_reason=_string(value.get("last_stage_reason")),
            last_stage_at=_timestamp(value.get("last_stage_at")),
        )

    def to_store(self) -> dict[str, object]:
        return {
            "cleaning_completed_at": _iso(self.cleaning_completed_at),
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
            "last_stage_outcome": self.last_stage_outcome,
            "last_stage_reason": self.last_stage_reason,
            "last_stage_at": _iso(self.last_stage_at),
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
    mop_washing_at: datetime | None = None
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
    adapter_id: str = "generic"
    adapter_schema_version: int = 1
    occurrence_id: str | None = None
    stage_index: int | None = None
    cleaning_profile: dict[str, str | None] = field(default_factory=dict)
    requested_profile: dict[str, str | None] = field(default_factory=dict)
    profile_sources: dict[str, str] = field(default_factory=dict)
    manual_mode: str | None = None

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
            mop_washing_at=_timestamp(value.get("mop_washing_at")),
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
            cleaning_profile=_profile_mapping(value.get("cleaning_profile")),
            requested_profile=_profile_mapping(value.get("requested_profile")),
            profile_sources=_profile_sources_mapping(value.get("profile_sources")),
            manual_mode=_string(value.get("manual_mode")),
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
            "duration_source": self.duration_source,
            "measured_minutes": self.measured_minutes,
            "interrupted": self.interrupted,
            "hold_reason": self.hold_reason,
            "held_at": _iso(self.held_at),
            "completion_before_hold": self.completion_before_hold,
            "cancelling_at": _iso(self.cancelling_at),
            "adapter_id": self.adapter_id,
            "adapter_schema_version": self.adapter_schema_version,
            "occurrence_id": self.occurrence_id,
            "stage_index": self.stage_index,
            "cleaning_profile": dict(self.cleaning_profile),
            "requested_profile": dict(self.requested_profile),
            "profile_sources": dict(self.profile_sources),
            "manual_mode": self.manual_mode,
        }


@dataclass(slots=True)
class CleaningStage:
    operation: str
    passes: int
    status: str = "pending"
    reason: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    cleaning_profile: dict[str, str | None] = field(default_factory=dict)
    requested_profile: dict[str, str | None] = field(default_factory=dict)
    profile_sources: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CleaningStage | None:
        operation = _string(value.get("operation"))
        if operation not in {"vacuum", "mop"}:
            return None
        status = str(value.get("status", "pending"))
        if status not in {
            "pending", "running", "completed", "skipped_no_water",
            "skipped_unconfirmed_water", "skipped_no_mop",
        }:
            status = "pending"
        profile = _profile_mapping(value.get("cleaning_profile"))
        if profile.get("operation") not in {None, operation}:
            raise StateSchemaError("stage cleaning profile operation does not match")
        return cls(operation, max(1, _integer(value.get("passes"), 1)), status,
                   _string(value.get("reason")), _timestamp(value.get("started_at")),
                   _timestamp(value.get("completed_at")),
                   profile,
                   _profile_mapping(value.get("requested_profile")),
                   _profile_sources_mapping(value.get("profile_sources")))

    def to_store(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "passes": self.passes,
            "status": self.status,
            "reason": self.reason,
            "started_at": _iso(self.started_at),
            "completed_at": _iso(self.completed_at),
            "cleaning_profile": dict(self.cleaning_profile),
            "requested_profile": dict(self.requested_profile),
            "profile_sources": dict(self.profile_sources),
        }


@dataclass(slots=True)
class CleaningOccurrence:
    occurrence_id: str
    room_id: str
    robot_registry_id: str
    robot_entity_id: str
    program: str
    stages: list[CleaningStage]
    scheduled_at: datetime
    created_at: datetime
    adapter_id: str
    adapter_schema_version: int
    current_stage: int = 0
    source: str = "scheduler"
    manual_mode: str | None = None
    manual_override: bool = False
    bypass_desired_window: bool = False
    manual_context_id: str | None = None
    manual_user_id: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CleaningOccurrence | None:
        stages = [stage for item in value.get("stages", []) if isinstance(item, Mapping)
                  and (stage := CleaningStage.from_mapping(item)) is not None]
        required = [_string(value.get(key)) for key in
                    ("occurrence_id", "room_id", "robot_registry_id", "robot_entity_id", "program")]
        scheduled = _timestamp(value.get("scheduled_at"))
        created = _timestamp(value.get("created_at"))
        if not all((*required, scheduled, created, stages)):
            return None
        current = min(max(0, _integer(value.get("current_stage"), 0)), len(stages))
        source = value.get("source", "scheduler")
        if source not in {"scheduler", "manual_dashboard"}:
            raise StateSchemaError("occurrence source is invalid")
        manual_mode = _optional_string(
            value.get("manual_mode"), "occurrence manual_mode"
        )
        if manual_mode not in {None, "configured", "vacuum_only", "mop_only"}:
            raise StateSchemaError("occurrence manual_mode is invalid")
        return cls(*required, stages, scheduled, created,
                   str(value.get("adapter_id", "generic")),
                   max(1, _integer(value.get("adapter_schema_version"), 1)), current,
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
                   _optional_string(
                       value.get("manual_user_id"), "occurrence manual_user_id"
                   ))

    def to_store(self) -> dict[str, object]:
        return {"occurrence_id": self.occurrence_id, "room_id": self.room_id,
                "robot_registry_id": self.robot_registry_id,
                "robot_entity_id": self.robot_entity_id, "program": self.program,
                "stages": [stage.to_store() for stage in self.stages],
                "scheduled_at": _iso(self.scheduled_at), "created_at": _iso(self.created_at),
                "adapter_id": self.adapter_id,
                "adapter_schema_version": self.adapter_schema_version,
                "current_stage": self.current_stage,
                "source": self.source,
                "manual_mode": self.manual_mode,
                "manual_override": self.manual_override,
                "bypass_desired_window": self.bypass_desired_window,
                "manual_context_id": self.manual_context_id,
                "manual_user_id": self.manual_user_id}


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
        required = [_string(value.get(key)) for key in
                    ("request_id", "occurrence_id", "room_id", "robot_registry_id",
                     "confirm_hash", "cancel_hash", "tag")]
        sent = _timestamp(value.get("sent_at"))
        expires = _timestamp(value.get("expires_at"))
        if not all((*required, sent, expires)):
            return None
        status = str(value.get("status", "pending"))
        if status not in {"pending", "confirmed", "cancelled", "expired"}:
            status = "pending"
        return cls(required[0], required[1], required[2], required[3],
                   max(0, _integer(value.get("stage_index"), 0)), required[4],
                   required[5], required[6], sent, expires, status,
                   _timestamp(value.get("responded_at")))

    def to_store(self) -> dict[str, object]:
        return {"request_id": self.request_id, "occurrence_id": self.occurrence_id,
                "room_id": self.room_id, "robot_registry_id": self.robot_registry_id,
                "stage_index": self.stage_index, "confirm_hash": self.confirm_hash,
                "cancel_hash": self.cancel_hash, "tag": self.tag,
                "sent_at": _iso(self.sent_at), "expires_at": _iso(self.expires_at),
                "status": self.status, "responded_at": _iso(self.responded_at)}


@dataclass(slots=True)
class WaterNotificationEpisode:
    room_id: str
    reason: str
    first_sent_at: datetime
    last_sent_at: datetime

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> WaterNotificationEpisode | None:
        room_id, reason = _string(value.get("room_id")), _string(value.get("reason"))
        first, last = _timestamp(value.get("first_sent_at")), _timestamp(value.get("last_sent_at"))
        return cls(room_id, reason, first, last) if all((room_id, reason, first, last)) else None

    def to_store(self) -> dict[str, object]:
        return {"room_id": self.room_id, "reason": self.reason,
                "first_sent_at": _iso(self.first_sent_at), "last_sent_at": _iso(self.last_sent_at)}


@dataclass(slots=True)
class SchedulerFault:
    """Durable scoped dispatch fault without raw vendor or exception data."""

    reason_code: str
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
        if not all((reason_code, robot_registry_id, room_area_id, occurred_at, phase)):
            return None
        return cls(
            reason_code=reason_code,
            robot_registry_id=robot_registry_id,
            room_area_id=room_area_id,
            occurred_at=occurred_at,
            phase=phase,
            native_command_may_have_started=bool(
                value.get("native_command_may_have_started", False)
            ),
            outcome_uncertain=bool(value.get("outcome_uncertain", False)),
        )

    def to_store(self) -> dict[str, object]:
        return {
            "reason_code": self.reason_code,
            "robot_registry_id": self.robot_registry_id,
            "room_area_id": self.room_area_id,
            "occurred_at": _iso(self.occurred_at),
            "phase": self.phase,
            "native_command_may_have_started": self.native_command_may_have_started,
            "outcome_uncertain": self.outcome_uncertain,
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
    robot_entity_aliases: dict[str, str] = field(default_factory=dict)
    room_history: dict[str, RoomHistory] = field(default_factory=dict)
    active_jobs: dict[str, ActiveJob | None] = field(default_factory=dict)
    robot_holds: dict[str, RobotHold] = field(default_factory=dict)
    audit: AuditState = field(default_factory=AuditState)
    evaluation: EvaluationState = field(default_factory=EvaluationState)
    robot_faults: dict[str, SchedulerFault] = field(default_factory=dict)
    room_faults: dict[str, SchedulerFault] = field(default_factory=dict)
    occurrences: dict[str, CleaningOccurrence] = field(default_factory=dict)
    water_confirmations: dict[str, WaterConfirmation] = field(default_factory=dict)
    water_notification_episodes: dict[str, WaterNotificationEpisode] = field(default_factory=dict)

    @classmethod
    def create(cls, entry_data: Mapping[str, object]) -> SchedulerState:
        return cls(global_settings=GlobalSettings.from_entry(entry_data))

    @classmethod
    def from_store(
        cls, payload: object, entry_data: Mapping[str, object]
    ) -> tuple[SchedulerState, bool]:
        """Load v10 or convert older shapes, returning whether a save is required."""

        if payload is None:
            return cls.create(entry_data), False
        data = _mapping(payload, "stored scheduler state")
        schema_version = data.get("schema_version")
        if schema_version is None or schema_version == 1:
            return cls._from_v1(data, entry_data), True
        if schema_version in {2, 3, 4, 5, 6, 7, 8, 9}:
            return cls._from_versioned(data, entry_data), True
        if schema_version != SCHEMA_VERSION:
            raise StateSchemaError(
                f"unsupported scheduler state schema: {schema_version!r}"
            )
        return cls._from_versioned(data, entry_data), False

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
        raw_occurrences = _mapping_or_empty(data.get("occurrences"))
        raw_confirmations = _mapping_or_empty(data.get("water_confirmations"))
        raw_episodes = _mapping_or_empty(data.get("water_notification_episodes"))
        raw_robot_faults = _mapping_or_empty(data.get("robot_faults"))
        raw_room_faults = _mapping_or_empty(data.get("room_faults"))
        legacy_fault = (
            SchedulerFault.from_mapping(value)
            if isinstance((value := data.get("scheduler_fault")), Mapping)
            else None
        )
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
            robot_faults={
                registry_id: fault
                for registry_id, value in raw_robot_faults.items()
                if isinstance(registry_id, str)
                and isinstance(value, Mapping)
                and (fault := SchedulerFault.from_mapping(value)) is not None
                and fault.robot_registry_id == registry_id
            } or (
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
                if isinstance(area_id, str) and isinstance(value, Mapping)
                and (occurrence := CleaningOccurrence.from_mapping(value)) is not None
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
                if isinstance(occurrence_id, str) and isinstance(value, Mapping)
                and (confirmation := WaterConfirmation.from_mapping(value)) is not None
            },
            water_notification_episodes={
                area_id: episode
                for area_id, value in raw_episodes.items()
                if isinstance(area_id, str) and isinstance(value, Mapping)
                and (episode := WaterNotificationEpisode.from_mapping(value)) is not None
            },
        )

    @classmethod
    def _from_versioned(
        cls, data: Mapping[str, object], entry_data: Mapping[str, object]
    ) -> SchedulerState:
        defaults = GlobalSettings.from_entry(entry_data)
        raw_global = _mapping(data.get("global"), "global")
        raw_room_settings = _mapping(data.get("room_settings"), "room_settings")
        raw_robot_settings = _mapping(data.get("robot_settings"), "robot_settings")
        raw_robot_aliases = (
            _mapping(data.get("robot_entity_aliases"), "robot_entity_aliases")
            if data.get("schema_version") == SCHEMA_VERSION
            else _mapping_or_empty(data.get("robot_entity_aliases"))
        )
        raw_history = _mapping(data.get("room_history"), "room_history")
        raw_active = _mapping(data.get("active_jobs"), "active_jobs")
        raw_holds = _mapping(data.get("robot_holds"), "robot_holds")
        raw_audit = _mapping(data.get("audit"), "audit")
        raw_evaluation = _mapping(data.get("evaluation"), "evaluation")
        raw_occurrences = _mapping_or_empty(data.get("occurrences"))
        raw_confirmations = _mapping_or_empty(data.get("water_confirmations"))
        raw_episodes = _mapping_or_empty(data.get("water_notification_episodes"))
        if data.get("schema_version") == SCHEMA_VERSION:
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
                if isinstance(area_id, str) and isinstance(value, Mapping)
                and (occurrence := CleaningOccurrence.from_mapping(value)) is not None
            },
            robot_entity_aliases={
                key: alias
                for key, alias in raw_robot_aliases.items()
                if isinstance(key, str) and isinstance(alias, str)
            },
            water_confirmations={
                occurrence_id: confirmation
                for occurrence_id, value in raw_confirmations.items()
                if isinstance(occurrence_id, str) and isinstance(value, Mapping)
                and (confirmation := WaterConfirmation.from_mapping(value)) is not None
            },
            water_notification_episodes={
                area_id: episode
                for area_id, value in raw_episodes.items()
                if isinstance(area_id, str) and isinstance(value, Mapping)
                and (episode := WaterNotificationEpisode.from_mapping(value)) is not None
            },
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
                area_id: settings.to_store()
                for area_id, settings in self.room_settings.items()
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
            "robot_faults": {
                registry_id: fault.to_store()
                for registry_id, fault in self.robot_faults.items()
            },
            "room_faults": {
                area_id: fault.to_store()
                for area_id, fault in self.room_faults.items()
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
        }

    def to_runtime_data(self) -> dict[str, Any]:
        """Expose a temporary runtime view while scheduler logic is extracted.

        The view is intentionally confined to the coordinator internals.  All
        persistent I/O stays on the typed v10 codec, and platform entities use
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
                    area_id: settings.to_runtime()
                    for area_id, settings in self.room_settings.items()
                },
                "robots": {
                    entity_id: settings.to_runtime()
                    for entity_id, settings in self.robot_settings.items()
                },
            },
            "rooms": {
                area_id: {
                    "cleaning": _iso(history.cleaning_completed_at),
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
                    "last_stage_outcome": history.last_stage_outcome,
                    "last_stage_reason": history.last_stage_reason,
                    "last_stage_at": _iso(history.last_stage_at),
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
            "robot_faults": {
                registry_id: fault.to_store()
                for registry_id, fault in self.robot_faults.items()
            },
            "room_faults": {
                area_id: fault.to_store()
                for area_id, fault in self.room_faults.items()
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
