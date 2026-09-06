"""Pure whole-house assignment planning."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from .models import (
    CleaningOperation,
    CleaningProgram,
    OccurrenceSource,
    RequestedCleaningProfile,
    ResolvedCleaningProfile,
)
from .state import CleaningOccurrence, CleaningStage


@dataclass(frozen=True, slots=True)
class VacancyDiagnostic:
    """Safe, typed vacancy evidence attached to a candidate."""

    occupancy_source: str
    unoccupied_since: datetime | None
    required_clear_minutes: int
    clear_minutes: float | None
    forecast_confidence: float
    comparable_sample_count: int
    successful_sample_count: int
    reason: str
    allowed: bool

    def as_attributes(self) -> dict[str, object]:
        """Serialize only when building entity/service attributes."""

        return {
            "occupancy_source": self.occupancy_source,
            "unoccupied_since": (
                self.unoccupied_since.isoformat() if self.unoccupied_since else None
            ),
            "required_clear_minutes": self.required_clear_minutes,
            "clear_minutes": self.clear_minutes,
            "forecast_confidence": self.forecast_confidence,
            "comparable_sample_count": self.comparable_sample_count,
            "successful_sample_count": self.successful_sample_count,
            "reason": self.reason,
            "allowed": self.allowed,
        }


@dataclass(frozen=True, slots=True)
class RobotEligibility:
    """One candidate's immutable per-robot diagnostic."""

    robot_id: str
    robot_name: str
    eligible: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ScheduleCandidate:
    """A fully typed room candidate carried through one evaluation."""

    room_id: str
    floor_id: str | None
    operation: CleaningOperation
    due_at: datetime
    confidence: float
    reason: str
    duration_minutes: float
    duration_sample_count: int
    passes: int
    occurrence: CleaningOccurrence | None
    evaluated_at: datetime
    unresolved_window_allowed: bool
    bypass_forecast: bool
    manual_override: bool
    source: OccurrenceSource
    manual_mode: str | None = None
    manual_context_id: str | None = None
    manual_user_id: str | None = None
    bypass_desired_window: bool = False
    occurrence_id: str | None = None
    stage_index: int = 0
    program: CleaningProgram | None = None
    new_stages: tuple[CleaningStage, ...] = ()
    resolved_profile: ResolvedCleaningProfile | None = None
    requested_profile: RequestedCleaningProfile | None = None
    profile_sources: tuple[tuple[str, str], ...] = ()
    vacancy_diagnostic: VacancyDiagnostic | None = None
    robot_eligibility: tuple[RobotEligibility, ...] = ()
    water_confirmed: bool = False
    ignore_water_readiness: bool = False


@dataclass(frozen=True, slots=True)
class CandidateRobotDecision:
    """A robot diagnostic plus its fully resolved candidate when eligible."""

    eligibility: RobotEligibility
    candidate: ScheduleCandidate | None = None


@dataclass(frozen=True, slots=True)
class CandidateOption:
    """One due room before a compatible robot is selected."""

    room_id: str
    due_at: datetime
    confidence: float
    ordinal: int


@dataclass(frozen=True, slots=True)
class RobotOption:
    """One robot's resolved eligibility for a candidate."""

    robot_id: str
    battery: float | None
    eligible: bool
    reason: str


@dataclass(frozen=True, slots=True)
class CandidateOptions:
    """Candidate plus every same-pass robot decision."""

    candidate: CandidateOption
    robots: tuple[RobotOption, ...]


@dataclass(frozen=True, slots=True)
class PlannedAssignment:
    """Stable room-to-robot selection."""

    room_id: str
    robot_id: str


@dataclass(frozen=True, slots=True)
class AssignmentPlan:
    """Side-effect-free result of the whole-house allocation pass."""

    ordered_room_ids: tuple[str, ...]
    assignments: tuple[PlannedAssignment, ...]
    blocks: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class PlanningInput:
    """One candidate and every robot resolution produced from an observation."""

    candidate: ScheduleCandidate
    decisions: tuple[CandidateRobotDecision, ...]
    ordinal: int
    battery_by_robot: tuple[tuple[str, float | None], ...]


@dataclass(frozen=True, slots=True)
class PlannedCandidateAssignment:
    """One selected robot paired with its immutable resolved candidate."""

    robot_id: str
    candidate: ScheduleCandidate


@dataclass(frozen=True, slots=True)
class WholeSchedulePlan:
    """Complete pure result for one set of pre-observed room candidates."""

    candidates: tuple[ScheduleCandidate, ...]
    assignments: tuple[PlannedCandidateAssignment, ...]
    blocks: tuple[tuple[str, str], ...]


def build_assignment_plan(inputs: tuple[CandidateOptions, ...]) -> AssignmentPlan:
    """Order candidates and allocate each robot at most once."""

    ordered = sorted(
        inputs,
        key=lambda item: (
            item.candidate.due_at,
            -item.candidate.confidence,
            item.candidate.ordinal,
        ),
    )
    used_robots: set[str] = set()
    assignments: list[PlannedAssignment] = []
    blocks: list[tuple[str, str]] = []
    for item in ordered:
        available = tuple(
            robot
            for robot in item.robots
            if robot.eligible and robot.robot_id not in used_robots
        )
        if not available:
            reason = next(
                (robot.reason for robot in item.robots if robot.reason),
                "no ready compatible robot",
            )
            blocks.append((item.candidate.room_id, reason))
            continue
        selected = max(
            available,
            key=lambda robot: (robot.battery or 0.0, robot.robot_id),
        )
        used_robots.add(selected.robot_id)
        assignments.append(PlannedAssignment(item.candidate.room_id, selected.robot_id))
    return AssignmentPlan(
        ordered_room_ids=tuple(item.candidate.room_id for item in ordered),
        assignments=tuple(assignments),
        blocks=tuple(blocks),
    )


def build_schedule_plan(inputs: tuple[PlanningInput, ...]) -> WholeSchedulePlan:
    """Build an ordered, resolved plan without mutating candidates or state."""

    battery_maps = {
        item.candidate.room_id: dict(item.battery_by_robot) for item in inputs
    }
    candidate_by_room = {
        item.candidate.room_id: replace(
            item.candidate,
            robot_eligibility=tuple(
                decision.eligibility for decision in item.decisions
            ),
        )
        for item in inputs
    }
    decisions_by_room = {item.candidate.room_id: item.decisions for item in inputs}
    allocation = build_assignment_plan(
        tuple(
            CandidateOptions(
                candidate=CandidateOption(
                    room_id=item.candidate.room_id,
                    due_at=item.candidate.due_at,
                    confidence=item.candidate.confidence,
                    ordinal=item.ordinal,
                ),
                robots=tuple(
                    RobotOption(
                        robot_id=decision.eligibility.robot_id,
                        battery=battery_maps[item.candidate.room_id].get(
                            decision.eligibility.robot_id
                        ),
                        eligible=(
                            decision.eligibility.eligible
                            and decision.candidate is not None
                        ),
                        reason=decision.eligibility.reason,
                    )
                    for decision in item.decisions
                ),
            )
            for item in inputs
        )
    )
    assignments: list[PlannedCandidateAssignment] = []
    for selected in allocation.assignments:
        base = candidate_by_room[selected.room_id]
        decision = next(
            decision
            for decision in decisions_by_room[selected.room_id]
            if decision.eligibility.robot_id == selected.robot_id
        )
        if decision.candidate is None:
            raise ValueError("assignment selected an unresolved candidate")
        resolved = replace(
            decision.candidate,
            robot_eligibility=base.robot_eligibility,
        )
        candidate_by_room[selected.room_id] = resolved
        assignments.append(PlannedCandidateAssignment(selected.robot_id, resolved))
    for room_id, reason in allocation.blocks:
        candidate_by_room[room_id] = replace(
            candidate_by_room[room_id],
            reason=reason,
        )
    return WholeSchedulePlan(
        candidates=tuple(
            candidate_by_room[room_id] for room_id in allocation.ordered_room_ids
        ),
        assignments=tuple(assignments),
        blocks=allocation.blocks,
    )
