"""Pure scheduler models and decisions.

This module deliberately has no Home Assistant imports so the safety-critical
occupancy and due-date behaviour can be tested without a running instance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import math
import re
from typing import Iterable, Literal, Mapping


VALID_OCCUPANCY_STATES = {"on", "off"}
DAILY_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
NATIVE_MOP_PROFILE_ROUTES = frozenset({"standard", "deep", "deep_plus", "fast"})
NATIVE_MOP_PROFILE_INTENSITIES = frozenset({"low", "medium", "high"})
ROBOROCK_DISPATCHABLE_STATES = frozenset(
    {"idle", "docked", "charging", "charging_complete"}
)


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


type SchedulerHaltRecheckReason = Literal[
    "cleared_docked",
    "cleared_cleaning",
    "no_scheduler_halt",
    "recovery_target_unavailable",
    "robot_state_unavailable",
    "robot_not_docked_or_cleaning",
]


@dataclass(frozen=True, slots=True)
class SchedulerHaltRecheckResult:
    """The safe outcome of acknowledging a scheduler halt."""

    cleared: bool
    reason: SchedulerHaltRecheckReason
    robot_state: str | None = None


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
class WaterReadiness:
    """Vendor-neutral decision for starting one mop stage."""

    status: str
    reason: str
    ready: bool = False
    authoritative: bool = False

    @classmethod
    def unsupported(cls) -> "WaterReadiness":
        return cls("unsupported", "mopping_unsupported")

    @classmethod
    def confirmation_required(cls) -> "WaterReadiness":
        return cls("confirmation_required", "water_confirmation_required")


@dataclass(frozen=True, slots=True)
class AdapterCapabilities:
    """Vendor-neutral capabilities advertised by one vacuum adapter."""

    adapter_id: str
    schema_version: int
    portable_area_clean: bool
    supported_pass_counts: frozenset[int]
    native_area_pass_counts: frozenset[int] = frozenset()
    supported_operations: frozenset[str] = frozenset({"vacuum"})
    fan_speed_options: tuple[str, ...] = ()
    mode_options: tuple[str, ...] = ()
    mop_mode_options: tuple[str, ...] = ()
    mop_intensity_options: tuple[str, ...] = ()
    cleaning_depth_options: tuple[str, ...] = ()
    water_readiness: WaterReadiness | str = WaterReadiness.unsupported()
    vacuum_pass_counts: frozenset[int] = frozenset()
    mop_pass_counts: frozenset[int] = frozenset()
    native_vacuum_pass_counts: frozenset[int] = frozenset()
    native_mop_pass_counts: frozenset[int] = frozenset()
    watched_entity_ids: tuple[str, ...] = ()
    native_mop_profile: bool = False
    readiness_entity_id: str | None = None
    readiness_states: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        """Normalize schema-one adapter snapshots during a rolling upgrade."""

        water = self.water_readiness
        if isinstance(water, str):
            normalized = (
                WaterReadiness.confirmation_required()
                if water in {"unknown", "confirmation_required"}
                else WaterReadiness.unsupported()
            )
            object.__setattr__(self, "water_readiness", normalized)
        if not self.vacuum_pass_counts:
            object.__setattr__(self, "vacuum_pass_counts", self.supported_pass_counts)
        if not self.mop_pass_counts and "mop" in self.supported_operations:
            object.__setattr__(self, "mop_pass_counts", self.supported_pass_counts)
        if not self.native_vacuum_pass_counts and not self.native_mop_pass_counts:
            # Schema-one snapshots exposed one native pass-count set shared by
            # both operations. Preserve that legacy interpretation only when a
            # newer adapter has not supplied either operation-specific set.
            object.__setattr__(
                self, "native_vacuum_pass_counts", self.native_area_pass_counts
            )
            if "mop" in self.supported_operations:
                object.__setattr__(
                    self, "native_mop_pass_counts", self.native_area_pass_counts
                )

    def supports(self, operation: str, passes: int) -> bool:
        """Return whether the normalized request can be attempted."""

        pass_counts = (
            self.mop_pass_counts if operation == "mop" else self.vacuum_pass_counts
        )
        return operation in self.supported_operations and passes in pass_counts

    def native_pass_counts_for(self, operation: str) -> frozenset[int]:
        """Return pass counts handled by a vendor-native command."""

        return (
            self.native_mop_pass_counts
            if operation == "mop"
            else self.native_vacuum_pass_counts
        )


@dataclass(frozen=True, slots=True)
class AdapterDispatchRequest:
    """A vendor-neutral room-clean request passed to an adapter."""

    robot_entity_id: str
    area_ids: tuple[str, ...]
    operation: str
    passes: int
    cleaning_profile: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class AdapterDispatchResult:
    """Normalized adapter preflight or dispatch result."""

    status: str
    code: str
    summary: str
    native_attempted: bool = False
    outcome_uncertain: bool = False

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"

    @property
    def ready(self) -> bool:
        return self.status in {"ready", "accepted"}

    @property
    def blocked(self) -> bool:
        return self.status == "blocked"


PROFILE_SETTING_KEYS = (
    "fan_speed",
    "mode",
    "mop_mode",
    "mop_intensity",
    "cleaning_depth",
)
type CleaningProgram = Literal[
    "vacuum_only", "mop_only", "vacuum_then_mop", "mop_then_vacuum"
]


@dataclass(frozen=True, slots=True)
class RequestedCleaningProfile:
    """Robot defaults with optional room-owned replacements."""

    fan_speed: str | None = None
    mode: str | None = None
    mop_mode: str | None = None
    mop_intensity: str | None = None
    cleaning_depth: str | None = None

    def to_mapping(self) -> dict[str, str | None]:
        return {
            "fan_speed": self.fan_speed,
            "mode": self.mode,
            "mop_mode": self.mop_mode,
            "mop_intensity": self.mop_intensity,
            "cleaning_depth": self.cleaning_depth,
        }


@dataclass(frozen=True, slots=True)
class ResolvedCleaningProfile:
    """Exact adapter-facing settings resolved for one physical stage."""

    operation: str
    fan_speed: str | None = None
    mode: str | None = None
    mop_mode: str | None = None
    mop_intensity: str | None = None
    cleaning_depth: str | None = None

    def to_mapping(self) -> dict[str, str | None]:
        """Return a Store- and adapter-safe snapshot."""

        return {
            "operation": self.operation,
            "fan_speed": self.fan_speed,
            "mode": self.mode,
            "mop_mode": self.mop_mode,
            "mop_intensity": self.mop_intensity,
            "cleaning_depth": self.cleaning_depth,
        }


def _profile_value(
    room_settings: Mapping[str, object],
    robot_settings: Mapping[str, object],
    key: str,
) -> str | None:
    value = room_settings.get(key)
    if value is None:
        value = robot_settings.get(key)
    return value if isinstance(value, str) and value else None


def requested_cleaning_profile(
    room_settings: Mapping[str, object], robot_settings: Mapping[str, object]
) -> RequestedCleaningProfile:
    """Resolve raw requested values without applying operation semantics."""

    return RequestedCleaningProfile(
        **{
            key: _profile_value(room_settings, robot_settings, key)
            for key in PROFILE_SETTING_KEYS
        }
    )


def native_mop_profile_default_migration(
    robot_settings: Mapping[str, object],
) -> dict[str, str | bool] | None:
    """Return the one-time native mop-profile defaults for one robot's settings.

    This deliberately accepts no room data: those overrides can belong to
    another vacuum and must remain untouched.  A caller keys the marker by the
    robot's stable registry identity, so a user's later concrete choices are
    never rewritten.
    """

    if bool(robot_settings.get("direct_custom_mop_migrated", False)):
        return None
    migration: dict[str, str | bool] = {"direct_custom_mop_migrated": True}
    route = robot_settings.get("mop_mode")
    if not is_native_mop_profile_value("mop_mode", route):
        migration["mop_mode"] = "standard"
    intensity = robot_settings.get("mop_intensity")
    if not is_native_mop_profile_value("mop_intensity", intensity):
        migration["mop_intensity"] = "medium"
    return migration


def is_native_mop_profile_value(key: str, value: object) -> bool:
    """Return whether one native mop-profile control has a concrete value."""

    allowed = (
        NATIVE_MOP_PROFILE_ROUTES
        if key == "mop_mode"
        else NATIVE_MOP_PROFILE_INTENSITIES
        if key == "mop_intensity"
        else frozenset()
    )
    return isinstance(value, str) and _normalized_profile_option(value) in allowed


def cleaning_profile_sources(
    room_settings: Mapping[str, object],
) -> dict[str, str]:
    """Describe whether each effective value is room-owned or inherited."""

    return {
        key: "room" if room_settings.get(key) is not None else "robot"
        for key in PROFILE_SETTING_KEYS
    }


def _normalized_profile_option(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _operation_mode_option(
    options: Iterable[str], operation: str
) -> str | None:
    wanted = (
        ("vacuum_only", "vacuum")
        if operation == "vacuum"
        else ("mop_only", "mop")
    )
    normalized = {
        _normalized_profile_option(option): option
        for option in options
    }
    return next((normalized[option] for option in wanted if option in normalized), None)


def resolve_cleaning_profile(
    operation: str,
    room_settings: Mapping[str, object],
    robot_settings: Mapping[str, object],
    capabilities: AdapterCapabilities,
) -> ResolvedCleaningProfile | None:
    """Resolve one exact stage profile, rejecting stale saved options.

    The normalized program owns a vendor operation selector. A room mode
    override is therefore accepted only when it describes the current stage;
    otherwise the request is incompatible instead of silently changing the
    user's requested operation.
    """

    if operation not in {"vacuum", "mop"}:
        return None
    option_sets = {
        "fan_speed": capabilities.fan_speed_options,
        "mode": capabilities.mode_options,
        "mop_mode": capabilities.mop_mode_options,
        "mop_intensity": capabilities.mop_intensity_options,
        "cleaning_depth": capabilities.cleaning_depth_options,
    }
    requested = requested_cleaning_profile(room_settings, robot_settings)
    values = {key: getattr(requested, key) for key in PROFILE_SETTING_KEYS}
    if operation == "mop" and capabilities.native_mop_profile:
        # Rob's exposed route, intensity, operation, and fan controls are
        # linked at the device protocol.  The stable, physical no-vacuum
        # contract is therefore native Mop plus suction Off, with concrete
        # route and water settings. Non-concrete shared room values survive
        # into the stage so the adapter can skip only mopping without
        # preventing an earlier vacuum stage from being scheduled.
        mop_mode = next(
            (
                option
                for option in capabilities.mode_options
                if _normalized_profile_option(option) == "mop"
            ),
            None,
        )
        fan_off = next(
            (
                option
                for option in capabilities.fan_speed_options
                if _normalized_profile_option(option) == "off"
            ),
            None,
        )
        if not mop_mode or not fan_off:
            return None
        # Robot defaults are migrated when the integration discovers this
        # capability.  These fallbacks also make a newly discovered robot's
        # first direct-custom mop explicit without modifying a shared room
        # override.  A non-concrete override still reaches the adapter and is
        # blocked safely there.
        mop_route = values["mop_mode"] or next(
            (
                option
                for option in capabilities.mop_mode_options
                if _normalized_profile_option(option) == "standard"
            ),
            None,
        )
        mop_intensity = values["mop_intensity"] or next(
            (
                option
                for option in capabilities.mop_intensity_options
                if _normalized_profile_option(option) == "medium"
            ),
            None,
        )
        return ResolvedCleaningProfile(
            operation=operation,
            fan_speed=fan_off,
            mode=mop_mode,
            mop_mode=mop_route,
            mop_intensity=mop_intensity,
        )
    applicable_keys = (
        ("fan_speed", "mode", "mop_mode", "mop_intensity")
        if operation == "mop"
        else ("fan_speed", "mode")
    )
    depth_applicable = operation == "vacuum" and bool(
        capabilities.cleaning_depth_options
    )
    if depth_applicable:
        applicable_keys += ("cleaning_depth",)
    for key in applicable_keys:
        value = values[key]
        if value is not None and value not in option_sets[key]:
            return None
    if not depth_applicable and values["cleaning_depth"] is not None:
        return None

    operation_mode = _operation_mode_option(capabilities.mode_options, operation)
    explicit_modes = {
        "vacuum", "vacuum_only", "mop", "mop_only", "vacuum_and_mop",
        "vac_and_mop",
    }
    wanted = (
        {"vacuum", "vacuum_only"}
        if operation == "vacuum"
        else {"mop", "mop_only"}
    )
    room_mode = room_settings.get("mode")
    if isinstance(room_mode, str) and room_mode:
        normalized = _normalized_profile_option(room_mode)
        operation_selector = any(
            _normalized_profile_option(option) in explicit_modes
            for option in capabilities.mode_options
        )
        if (operation_selector and normalized not in wanted) or (
            normalized in explicit_modes and normalized not in wanted
        ):
            return None
        mode = room_mode
    else:
        saved_mode = values["mode"]
        if (
            saved_mode
            and _normalized_profile_option(saved_mode) in explicit_modes
            and _normalized_profile_option(saved_mode) not in wanted
            and operation_mode is None
        ):
            return None
        mode = operation_mode or values["mode"]

    mop_mode = values["mop_mode"] if operation == "mop" else None
    if (
        operation == "mop"
        and mop_mode is not None
        and _normalized_profile_option(mop_mode) in explicit_modes
        and _normalized_profile_option(mop_mode) not in wanted
    ):
        return None
    if operation == "mop" and mop_mode is None:
        mop_mode = _operation_mode_option(capabilities.mop_mode_options, operation)

    required = (
        (capabilities.fan_speed_options, values["fan_speed"]),
        (capabilities.mode_options, mode),
    )
    if operation == "mop":
        required += (
            (capabilities.mop_mode_options, mop_mode),
            (capabilities.mop_intensity_options, values["mop_intensity"]),
        )
    if any(options and value is None for options, value in required):
        return None

    return ResolvedCleaningProfile(
        operation=operation,
        fan_speed=values["fan_speed"],
        mode=mode,
        mop_mode=mop_mode,
        mop_intensity=(values["mop_intensity"] if operation == "mop" else None),
        cleaning_depth=(values["cleaning_depth"] if depth_applicable else None),
    )


def cleaning_profile_is_supported(
    profile: Mapping[str, object], capabilities: AdapterCapabilities
) -> bool:
    """Return whether a persisted exact profile still exists in capabilities."""

    option_sets = {
        "fan_speed": capabilities.fan_speed_options,
        "mode": capabilities.mode_options,
        "mop_mode": capabilities.mop_mode_options,
        "mop_intensity": capabilities.mop_intensity_options,
        "cleaning_depth": capabilities.cleaning_depth_options,
    }
    if not all(
        value is None or isinstance(value, str) and value in option_sets[key]
        for key, value in (
            (key, profile.get(key)) for key in PROFILE_SETTING_KEYS
        )
    ):
        return False
    operation = profile.get("operation")
    if operation not in {"vacuum", "mop"}:
        return False
    required = (
        (capabilities.fan_speed_options, profile.get("fan_speed")),
        (capabilities.mode_options, profile.get("mode")),
    )
    if operation == "mop":
        required += (
            (capabilities.mop_mode_options, profile.get("mop_mode")),
            (capabilities.mop_intensity_options, profile.get("mop_intensity")),
        )
    return not any(options and value is None for options, value in required)


def can_refresh_pending_occurrence_profile(
    occurrence: Mapping[str, object] | None,
    stage: Mapping[str, object] | None,
    robot_state: str | None,
    has_active_job: bool,
) -> bool:
    """Return whether a stale scheduler profile may be refreshed safely.

    A persisted occurrence normally keeps its exact resolved profile so later
    default changes cannot alter scheduled work.  The sole recovery exception
    is an unstarted scheduler stage whose assigned robot is observed docked;
    no active or manual work may be changed this way.
    """

    return bool(
        occurrence
        and occurrence.get("source", "scheduler") == "scheduler"
        and stage
        and stage.get("status") == "pending"
        and not stage.get("started_at")
        and robot_state == "docked"
        and not has_active_job
    )


CLEANING_PROGRAMS: tuple[CleaningProgram, ...] = (
    "vacuum_only",
    "mop_only",
    "vacuum_then_mop",
    "mop_then_vacuum",
)


def expand_cleaning_program(program: str) -> tuple[str, ...]:
    """Expand a public cleaning program into ordered physical starts."""

    return {
        "vacuum_only": ("vacuum",),
        "mop_only": ("mop",),
        "vacuum_then_mop": ("vacuum", "mop"),
        "mop_then_vacuum": ("mop", "vacuum"),
    }.get(program, ())


def effective_cleaning_program(
    room_program: str | None, robot_program: str
) -> CleaningProgram | None:
    """Resolve a room override while rejecting malformed stored values."""

    program = room_program or robot_program
    return program if program in CLEANING_PROGRAMS else None


def stage_pass_count(
    operation: str,
    room_vacuum_passes: int | None,
    room_mop_passes: int | None,
    robot_vacuum_double_pass: bool,
    robot_mop_double_pass: bool,
    capabilities: AdapterCapabilities,
) -> int | None:
    """Resolve an operation-specific pass count without silent downgrade."""

    room_value = room_mop_passes if operation == "mop" else room_vacuum_passes
    default_double = (
        robot_mop_double_pass if operation == "mop" else robot_vacuum_double_pass
    )
    supported = (
        capabilities.mop_pass_counts
        if operation == "mop"
        else capabilities.vacuum_pass_counts
    )
    return resolve_pass_count(room_value, default_double, supported)


def resolve_pass_count(
    room_pass_count: int | None,
    robot_default_double_pass: bool,
    supported_pass_counts: Iterable[int],
) -> int | None:
    """Resolve a room override against one robot without downgrading it."""

    supported = frozenset(int(value) for value in supported_pass_counts)
    requested = room_pass_count if room_pass_count is not None else (
        2 if robot_default_double_pass else 1
    )
    return requested if requested in supported else None


@dataclass(frozen=True, slots=True)
class ManualCleanRequest:
    """A room-targeted clean explicitly initiated by a Home Assistant user."""

    robot_id: str
    area_ids: list[str]


@dataclass(frozen=True, slots=True)
class ResolvedDailyWindow:
    """One room's configured and effective daily cleaning window."""

    configured_start: str | None
    configured_end: str | None
    start: str
    end: str
    start_inherited: bool
    end_inherited: bool

    @property
    def valid(self) -> bool:
        """Return whether the effective half-open interval is usable."""

        return self.start != self.end


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

    A docked robot is not enough to infer user intent because some
    native integrations report it shortly after an error. A live ``returning``
    state is the physical-dock signal; a fresh ``cleaning`` state is a physical
    resume. A job that had already entered returning before its fault has a
    confirmed clean phase and can complete once it is observed at the dock,
    including when it was placed there manually.
    """

    if pending_completion_is_docked(robot_state, phase):
        return "complete"
    if robot_state == "cleaning":
        return "resumed"
    if robot_state == "returning":
        return "completion_pending" if completed_before_hold else "cancelling"
    if phase == "cancelling" and robot_state == "docked":
        return "cancelled"
    return "held"


def pending_completion_is_docked(robot_state: str | None, phase: str | None) -> bool:
    """Return whether a pending completion has reached the dock.

    A completion-pending job has already established that its room clean
    finished.  Its final dock observation is therefore sufficient regardless
    of whether the robot navigated there by itself or was placed on the dock.
    """

    return (
        phase in {"completion_pending", "completion_held", "recovery_waiting"}
        and robot_state == "docked"
    )


def can_start_scheduled_clean(robot_state: str | None) -> bool:
    """Return whether the robot is physically safe to start scheduled work."""

    return robot_state == "docked"


def detailed_status_is_dispatchable(
    status: str | None,
    *,
    required: bool,
    ready_states: frozenset[str] = ROBOROCK_DISPATCHABLE_STATES,
) -> bool:
    """Return whether an adapter's detailed post-dock state permits dispatch.

    Generic robots do not expose a reliable post-dock servicing sensor and
    retain the established docked-state behaviour.  A discovered Roborock
    status sensor is authoritative: unavailable and unfamiliar values are
    deliberately treated as not ready rather than risking a second command
    while the dock is emptying or washing the robot.
    """

    if not required:
        return True
    return str(status or "").strip().lower() in ready_states


def ready_confirmation_elapsed(
    ready_since: datetime | None, now: datetime, delay: timedelta
) -> bool:
    """Require one uninterrupted observed-ready interval before dispatch."""

    return bool(ready_since and now - ready_since >= delay)


def scheduler_halt_recheck_result(
    robot_state: str | None,
) -> SchedulerHaltRecheckResult:
    """Classify whether a physical state can acknowledge a scheduler halt.

    Acknowledging a halt never dispatches cleaning work, so it must not apply
    future-dispatch gates such as battery level, occupancy, or map readiness.
    Those safeguards are re-evaluated when the scheduler later considers a
    new clean.
    """

    if robot_state == "docked":
        return SchedulerHaltRecheckResult(True, "cleared_docked", robot_state)
    if robot_state == "cleaning":
        return SchedulerHaltRecheckResult(True, "cleared_cleaning", robot_state)
    if robot_state in {None, "unknown", "unavailable"}:
        return SchedulerHaltRecheckResult(False, "robot_state_unavailable", robot_state)
    return SchedulerHaltRecheckResult(
        False, "robot_not_docked_or_cleaning", robot_state
    )


def should_assume_native_app_clean(
    robot_state: str | None,
    scheduler_fault: Mapping[str, object] | None,
    robot_registry_id: str,
    active: Mapping[str, object] | None,
) -> bool:
    """Return whether a live clean cannot be attributed to a scheduler job.

    A start whose outcome was uncertain has no authoritative room observation.
    If the vacuum is subsequently cleaning, retaining the planned scheduler room
    would incorrectly record an unproven clean. Treat that physical clean as a
    native-app clean instead.
    """

    return bool(
        robot_state == "cleaning"
        and scheduler_fault
        and scheduler_fault.get("robot_registry_id") == robot_registry_id
        and active
        and active.get("source") in {"scheduler", "manual_dashboard"}
        and not active.get("seen_cleaning")
    )


def offline_held_recovery_outcome(
    robot_state: str | None,
    hold_phase: str | None,
    last_observed_at: datetime | None,
    expected_minutes: float | None,
    recovered_at: datetime,
) -> str:
    """Classify an unobserved held-job ending after Home Assistant restarts."""

    if robot_state != "docked":
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


def profile_control_kind(
    translation_key: str | None,
    options: Iterable[str],
    labels: Iterable[str] = (),
) -> str | None:
    """Classify a same-device profile select from stable metadata first."""

    key = (translation_key or "").strip().lower().replace("-", "_")
    metadata_kinds = {
        "mop_intensity": "mop_intensity",
        "water_flow": "mop_intensity",
        "water_level": "mop_intensity",
        "mop_mode": "mop_mode",
        "cleaning_mode": "mode",
        "clean_mode": "mode",
        "operation_mode": "mode",
        "cleaning_passes": "passes",
        "pass_count": "passes",
    }
    if key in metadata_kinds:
        return metadata_kinds[key]

    normalised = {
        str(option).strip().lower().replace(" ", "-").replace("_", "-")
        for option in options
    }
    normalised_labels = {
        str(label).strip().lower().replace(" ", "-").replace("_", "-")
        for label in labels
    }
    if (
        {"one-pass", "two-pass"}.issubset(normalised)
        or {"single-pass", "double-pass"}.issubset(normalised)
        or "robovac-double-pass" in normalised_labels
    ):
        return "passes"
    if (
        {"low", "medium", "high"}.issubset(normalised)
        and any("water" in option or "mop" in option for option in normalised)
    ):
        return "mop_intensity"
    if "vacuum" in normalised and normalised.intersection({"mop", "mop-only"}):
        return "mode"
    if any("mop" in option or "deep" in option for option in normalised):
        return "mop_mode"
    if normalised.intersection({"vacuum", "vac-and-mop", "mop"}):
        return "mode"
    return None


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
        and new_state in {"returning", "docked"}
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
    """Return the cadence due time, accepting only bounded deferrals.

    A cancellation rebase or stale restored Store value must not leave a room
    deferred beyond one complete cadence.  Deferrals within that horizon still
    provide the intended short cooldown after a manual clean or cancellation.
    """

    baseline = now if last_completed is None else last_completed + timedelta(hours=interval_hours)
    if deferred_until is None:
        return baseline
    if deferred_until > now + timedelta(hours=interval_hours):
        return baseline
    return max(baseline, deferred_until)


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


def manual_clean_robot_is_docked(state: str | None) -> bool:
    """Return whether a robot meets the sole physical gate for a manual clean.

    Dashboard-triggered room cleans are an explicit user override.  They do
    not inherit scheduler gates such as battery thresholds, room occupancy,
    cadence, or vacancy forecasting; the robot must simply be docked before
    accepting a new room-clean command.
    """

    return state == "docked"


def can_request_return_to_dock(state: str | None) -> bool:
    """Return whether Home Assistant can send a physical return command."""

    return state not in {None, "unavailable", "unknown", "docked"}


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


def is_valid_daily_time(value: object) -> bool:
    """Return whether a value is a zero-padded local ``HH:MM`` time."""

    return isinstance(value, str) and DAILY_TIME_PATTERN.fullmatch(value) is not None


def resolve_daily_window(
    configured_start: str | None,
    configured_end: str | None,
    global_start: str,
    global_end: str,
) -> ResolvedDailyWindow:
    """Resolve independently inherited room bounds against global defaults."""

    values = {
        "configured start": configured_start,
        "configured end": configured_end,
        "global start": global_start,
        "global end": global_end,
    }
    for name, value in values.items():
        if value is not None and not is_valid_daily_time(value):
            raise ValueError(f"Invalid {name}: {value!r}")
    return ResolvedDailyWindow(
        configured_start=configured_start,
        configured_end=configured_end,
        start=configured_start if configured_start is not None else global_start,
        end=configured_end if configured_end is not None else global_end,
        start_inherited=configured_start is None,
        end_inherited=configured_end is None,
    )


def in_daytime_window(now: datetime, start: str, end: str) -> bool:
    """Return whether a local time is in a configured half-open time range.

    The scheduler uses the same helper for the daytime bedroom-transit policy
    and the overnight unresolved-occupancy policy.  Supporting windows that
    cross midnight avoids treating a valid night range as empty.
    """

    if not is_valid_daily_time(start) or not is_valid_daily_time(end) or start == end:
        return False
    time_text = now.strftime("%H:%M")
    if start < end:
        return start <= time_text < end
    return time_text >= start or time_text < end


def next_window_start(now: datetime, start: str) -> datetime:
    """Return the next occurrence of a local HH:MM window start."""

    if not is_valid_daily_time(start):
        raise ValueError(f"Invalid daily window start: {start!r}")
    hour, minute = (int(part) for part in start.split(":", maxsplit=1))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate if now < candidate else candidate + timedelta(days=1)


def next_usable_window_start(now: datetime, start: str, end: str) -> datetime:
    """Return now inside a valid window, otherwise its next start boundary."""

    if not is_valid_daily_time(start) or not is_valid_daily_time(end) or start == end:
        raise ValueError(f"Invalid daily window: {start!r}-{end!r}")
    return now if in_daytime_window(now, start, end) else next_window_start(now, start)


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
    now: datetime,
) -> tuple[str, datetime]:
    """Choose the due operation for a room's configured cleaning program."""

    if can_mop and mop_due is not None and mop_due <= now:
        if vacuum_due <= now:
            return "vac_and_mop", min(vacuum_due, mop_due)
        return "mop", mop_due
    return "vacuum", vacuum_due
