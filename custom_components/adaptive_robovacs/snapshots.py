"""Immutable values published by the Adaptive RoboVacs coordinator."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .discovery import RobotProfile
from .map_recovery_models import MapRecoverySummary
from .models import (
    AdapterCapabilities,
    CleaningOperation,
    CleaningProgram,
    JobPhase,
    JobSource,
    OccurrenceSource,
    RequestedCleaningProfile,
    ResolvedCleaningProfile,
    StageStatus,
)
from .planner import VacancyDiagnostic
from .state import FloorPlanRectangle, FloorPlanSensorMarker

type JsonScalar = str | int | float | bool | None
type FrozenJsonValue = JsonScalar | tuple["FrozenJsonValue", ...] | "FrozenJsonObject"


@dataclass(frozen=True, slots=True)
class FrozenJsonObject(Mapping[str, FrozenJsonValue]):
    """Equality-comparable JSON retained for the legacy preview payload."""

    entries: tuple[tuple[str, FrozenJsonValue], ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, object] | None) -> FrozenJsonObject:
        source = value or {}
        return cls(
            tuple(
                (str(key), _freeze_json(item))
                for key, item in sorted(source.items(), key=lambda pair: str(pair[0]))
            )
        )

    def __getitem__(self, key: str) -> FrozenJsonValue:
        for item_key, value in self.entries:
            if item_key == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _value in self.entries)

    def __len__(self) -> int:
        return len(self.entries)


def _freeze_json(value: object) -> FrozenJsonValue:
    if isinstance(value, Mapping):
        return FrozenJsonObject.from_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, StrEnum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def thaw_json(value: FrozenJsonValue | FrozenJsonObject) -> object:
    """Create JSON containers at a Home Assistant presentation boundary."""

    if isinstance(value, FrozenJsonObject):
        return {key: thaw_json(item) for key, item in value.entries}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class FaultView:
    """Redaction-safe scheduler failure details."""

    failure_code: str
    failure_summary: str
    failure_since: datetime
    failure_phase: str
    robot_name: str | None
    room_name: str | None


@dataclass(frozen=True, slots=True)
class ActiveJobView:
    """Immutable copy of an active physical-clean checkpoint."""

    room_id: str
    room_ids: tuple[str, ...]
    operation: CleaningOperation
    phase: JobPhase
    source: JobSource
    started_at: datetime | None
    seen_cleaning: bool
    expected_minutes: float | None
    expected_end: datetime | None
    last_observed_at: datetime | None
    passes: int
    requested_operations: tuple[CleaningOperation, ...]
    manual_context_id: str | None
    accepted_at: datetime | None
    mop_washing_at: datetime | None
    observed_started_at: datetime | None
    recovered_at: datetime | None
    cleaning_finished_at: datetime | None
    completion_confidence: str | None
    timer_start: float | None
    native_timer_elapsed: float | None
    duration_source: str | None
    measured_minutes: float | None
    docked_at: datetime | None
    interruption_started_at: datetime | None
    interruption_minutes: float
    forecast_sample_eligible: bool
    recovery_crossed: bool
    interrupted: bool
    hold_reason: str | None
    held_at: datetime | None
    completion_before_hold: bool
    cancelling_at: datetime | None
    adapter_id: str
    adapter_schema_version: int
    occurrence_id: str | None
    stage_index: int | None
    cleaning_profile: ResolvedCleaningProfile | None
    requested_profile: RequestedCleaningProfile | None
    profile_sources: tuple[tuple[str, str], ...]
    manual_mode: str | None
    q10_max_plus_fallback: bool


@dataclass(frozen=True, slots=True)
class RobotHoldView:
    """Immutable copy of a scheduler or recovery hold."""

    reason: str
    phase: str
    held_at: datetime | None
    last_observed_at: datetime | None
    returning_at: datetime | None
    requested_map_id: str | None


@dataclass(frozen=True, slots=True)
class CleaningStageView:
    """One immutable stage in a room occurrence."""

    operation: CleaningOperation
    passes: int
    status: StageStatus
    reason: str | None
    started_at: datetime | None
    completed_at: datetime | None
    cleaning_profile: ResolvedCleaningProfile | None
    requested_profile: RequestedCleaningProfile | None
    profile_sources: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class CleaningOccurrenceView:
    """Immutable multi-stage occurrence state."""

    occurrence_id: str
    room_id: str
    robot_registry_id: str
    robot_entity_id: str | None
    program: CleaningProgram
    stages: tuple[CleaningStageView, ...]
    scheduled_at: datetime
    created_at: datetime
    adapter_id: str
    adapter_schema_version: int
    current_stage: int
    source: OccurrenceSource
    manual_mode: str | None
    manual_override: bool
    bypass_desired_window: bool
    manual_context_id: str | None
    manual_user_id: str | None


@dataclass(frozen=True, slots=True)
class WaterConfirmationView:
    """Safe public subset of a pending water confirmation."""

    status: str
    sent_at: datetime
    expires_at: datetime
    responded_at: datetime | None


@dataclass(frozen=True, slots=True)
class WaterNotificationEpisodeView:
    """One bounded notification episode."""

    room_id: str
    reason: str
    first_sent_at: datetime
    last_sent_at: datetime


@dataclass(frozen=True, slots=True)
class ManualAuditView:
    """One audit event with its current transient entity ID resolved."""

    at: datetime | None
    robot_entity_id: str | None
    room_ids: tuple[str, ...]
    operations: tuple[str, ...]
    context_id: str | None
    user_id: str | None
    mode: str | None
    source: str | None
    outcome: str | None
    reason: str | None
    confidence: str | None
    changed: tuple[str, ...]
    deferred: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoomDecisionView:
    """One immutable room-decision audit entry."""

    at: datetime | None
    room_area_id: str | None
    reason: str | None
    occupancy_source: str | None
    required_clear_minutes: int
    clear_minutes: float | None
    forecast_confidence: float
    comparable_sample_count: int
    forecast_reason: str | None


@dataclass(frozen=True, slots=True)
class CandidateView:
    """Entity-facing subset of a current schedule candidate."""

    room_id: str
    operation: CleaningOperation
    due_at: datetime
    confidence: float
    reason: str
    duration_minutes: float
    passes: int
    manual_override: bool
    source: OccurrenceSource


@dataclass(frozen=True, slots=True)
class RobotEligibilityView:
    """One robot's assignment eligibility for a room."""

    robot_entity_id: str
    robot_name: str
    eligible: bool
    reason: str


@dataclass(frozen=True, slots=True)
class DurationEstimateView:
    """Learned room-duration values for one compatible robot."""

    robot_entity_id: str
    robot_name: str
    typical_minutes: float
    safe_minutes: float
    sample_count: int
    learned: bool


@dataclass(frozen=True, slots=True)
class EffectiveStageProfileView:
    """One resolved operation-specific cleaning profile."""

    operation: CleaningOperation
    passes: int
    cleaning_profile: ResolvedCleaningProfile
    requested_profile: RequestedCleaningProfile
    profile_sources: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class EffectiveRobotProfileView:
    """Room program resolved against one same-floor robot."""

    robot_entity_id: str
    robot_name: str
    program: CleaningProgram | None
    compatible: bool
    stages: tuple[EffectiveStageProfileView, ...]


@dataclass(frozen=True, slots=True)
class RoomView:
    """Typed entity-facing state for one discovered room."""

    area_id: str
    name: str
    floor_id: str | None
    bedroom: bool
    bedroom_transit: bool
    radar_entity_ids: tuple[str, ...]
    fallback_entity_ids: tuple[str, ...]
    cleaning_period: str
    cleaning_profile: str
    enabled: bool
    cleaning_interval: float
    expected_minutes: float
    ignore_desired_window: bool
    desired_window_configured_start: str | None
    desired_window_configured_end: str | None
    desired_window_effective_start: str
    desired_window_effective_end: str
    desired_window_start_inherited: bool
    desired_window_end_inherited: bool
    desired_window_valid: bool
    vacuum_pass_count: int | None
    mop_pass_count: int | None
    cleaning_program: CleaningProgram | None
    fan_speed: str | None
    mode: str | None
    mop_mode: str | None
    mop_intensity: str | None
    cleaning_depth: str | None
    effective_profiles: tuple[EffectiveRobotProfileView, ...]
    latest_manual_request: ManualAuditView | None
    occupancy: str
    occupancy_source: str
    unavailable_radars: int
    last_cleaned: datetime | None
    last_cleaned_display: str
    using_initial_cadence_baseline: bool
    last_vacuum: datetime | None
    last_mop: datetime | None
    next_due: datetime
    desired_window_start: datetime
    next_candidate: CandidateView | None
    assignment_available: bool
    robot_eligibility: tuple[RobotEligibilityView, ...]
    active: ActiveJobView | None
    active_robot: str | None
    active_robot_state: str | None
    effective_duration_minutes: float
    duration_sample_count: int
    predicted_total_minutes: float
    required_vacancy_minutes: float
    duration_model_version: int
    duration_model_learned: bool
    duration_estimates_by_robot: tuple[DurationEstimateView, ...]
    block_reason: str
    vacancy_diagnostic: VacancyDiagnostic
    latest_scheduler_decision: RoomDecisionView | None
    legacy_deferral_review_needed: bool
    map_status: str
    map_error: str | None
    occurrence: CleaningOccurrenceView | None
    water_confirmation: WaterConfirmationView | None
    last_stage_outcome: str | None
    last_stage_reason: str | None
    last_stage_at: datetime | None
    last_stage_summary: str | None
    water_notification_episode: WaterNotificationEpisodeView | None
    failure: FaultView | None


@dataclass(frozen=True, slots=True)
class RobotSettingsView:
    """Immutable robot defaults exposed to controls and status entities."""

    enabled: bool
    minimum_battery: float
    cleaning_program: CleaningProgram
    double_pass: bool
    mop_double_pass: bool
    mode: str | None
    mop_mode: str | None
    mop_intensity: str | None
    fan_speed: str | None
    cleaning_depth: str | None
    cleaning_depth_configured: bool
    direct_custom_mop_migrated: bool

    @property
    def mopping_enabled(self) -> bool:
        return self.cleaning_program is not CleaningProgram.VACUUM_ONLY


@dataclass(frozen=True, slots=True)
class ObservedProfileView:
    """Current HA states for a robot's profile controls."""

    fan_speed: str | None
    mode: str | None
    mop_mode: str | None
    mop_intensity: str | None
    passes: str | None


@dataclass(frozen=True, slots=True)
class RobotView:
    """Typed entity-facing state for one registry-backed robot."""

    registry_id: str
    entity_id: str
    unique_fragment: str
    name: str
    floor_id: str | None
    state: str
    battery: float | None
    ready: bool
    reason: str
    active: ActiveJobView | None
    scheduler_hold: RobotHoldView | None
    active_room: str | None
    active_rooms: tuple[str, ...]
    profile: RobotProfile
    adapter_id: str
    adapter_schema_version: int
    adapter_capabilities: AdapterCapabilities
    adapter_diagnostic: str | None
    failure: FaultView | None
    settings: RobotSettingsView
    observed_profile: ObservedProfileView
    mop_profile_summary: str | None

    @property
    def supported_operations(self) -> tuple[str, ...]:
        return tuple(sorted(self.adapter_capabilities.supported_operations))

    @property
    def vacuum_pass_counts(self) -> tuple[int, ...]:
        return tuple(sorted(self.adapter_capabilities.vacuum_pass_counts))

    @property
    def mop_pass_counts(self) -> tuple[int, ...]:
        return tuple(sorted(self.adapter_capabilities.mop_pass_counts))

    @property
    def fan_speed_options(self) -> tuple[str, ...]:
        return self.adapter_capabilities.fan_speed_options

    @property
    def mode_options(self) -> tuple[str, ...]:
        return self.adapter_capabilities.mode_options

    @property
    def mop_mode_options(self) -> tuple[str, ...]:
        return self.adapter_capabilities.mop_mode_options

    @property
    def mop_intensity_options(self) -> tuple[str, ...]:
        return self.adapter_capabilities.mop_intensity_options

    @property
    def cleaning_depth_options(self) -> tuple[str, ...]:
        return self.adapter_capabilities.cleaning_depth_options

    @property
    def native_mop_profile(self) -> bool:
        return self.adapter_capabilities.native_mop_profile

    @property
    def mode_select_available(self) -> bool:
        return self.profile.mode_select_entity_id is not None

    @property
    def mop_mode_select_available(self) -> bool:
        return self.profile.mop_mode_select_entity_id is not None

    @property
    def mop_intensity_select_available(self) -> bool:
        return self.profile.mop_intensity_select_entity_id is not None


@dataclass(frozen=True, slots=True)
class FloorPlanSensorView:
    """One live occupancy source and its optional plan marker."""

    registry_id: str
    entity_id: str
    kind: str
    state: str
    marker: FloorPlanSensorMarker | None


@dataclass(frozen=True, slots=True)
class FloorPlanRoomView:
    """One live room and its floor-plan geometry."""

    area_id: str
    name: str
    floor_id: str
    rectangle: FloorPlanRectangle | None
    sensors: tuple[FloorPlanSensorView, ...]


@dataclass(frozen=True, slots=True)
class FloorView:
    """Stable sorted room views for one Home Assistant floor."""

    floor_id: str
    rooms: tuple[FloorPlanRoomView, ...]


@dataclass(frozen=True, slots=True)
class FloorPlanView:
    """Immutable floor-plan projection."""

    revision: int
    floors: tuple[FloorView, ...]
    edges: tuple[tuple[str, str], ...]
    orphaned_rooms: tuple[str, ...]
    orphaned_sensors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MapView:
    """Archived-map recovery presentation for one robot."""

    robot_registry_id: str
    available: bool
    summary: MapRecoverySummary
    preview_options: tuple[str, ...]
    selected_preview_option: str | None
    selected_preview: bytes | None


@dataclass(frozen=True, slots=True)
class SchedulerView:
    """Global scheduler values exposed to Home Assistant."""

    observe_only: bool
    party_mode: bool
    scheduler_halted: bool
    scheduler_limited: bool
    storage_safe_mode: bool
    forecast_confidence: float
    hall_start: str
    hall_end: str
    unresolved_start: str
    unresolved_end: str
    last_evaluation_at: datetime | None
    preview: FrozenJsonObject
    robot_faults: tuple[FaultView, ...]
    room_faults: tuple[FaultView, ...]
    floor_plan: FloorPlanView
    failure: FaultView | None

    def global_setting(self, key: str) -> object:
        """Return one supported global control value."""

        if key == "observe_only":
            return self.observe_only
        if key == "party_mode":
            return self.party_mode
        if key == "forecast_confidence":
            return self.forecast_confidence
        if key == "hall_start":
            return self.hall_start
        if key == "hall_end":
            return self.hall_end
        if key == "unresolved_start":
            return self.unresolved_start
        if key == "unresolved_end":
            return self.unresolved_end
        raise KeyError(key)


@dataclass(frozen=True, slots=True)
class IntegrationSnapshot:
    """One equality-comparable update published to every platform."""

    scheduler: SchedulerView
    rooms: tuple[RoomView, ...]
    robots: tuple[RobotView, ...]
    maps: tuple[MapView, ...]
    floor_plan: FloorPlanView

    def room(self, area_id: str) -> RoomView | None:
        return next((room for room in self.rooms if room.area_id == area_id), None)

    def robot_by_entity_id(self, entity_id: str) -> RobotView | None:
        return next(
            (robot for robot in self.robots if robot.entity_id == entity_id),
            None,
        )

    def robot_by_registry_id(self, registry_id: str) -> RobotView | None:
        return next(
            (robot for robot in self.robots if robot.registry_id == registry_id),
            None,
        )

    def map_for_robot(self, registry_id: str) -> MapView | None:
        return next(
            (item for item in self.maps if item.robot_registry_id == registry_id),
            None,
        )
