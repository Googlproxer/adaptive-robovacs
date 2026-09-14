"""Table-driven tests for pure whole-schedule planning."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from custom_components.adaptive_robovacs.models import (
    CleaningOperation,
    OccurrenceSource,
)
from custom_components.adaptive_robovacs.planner import (
    CandidateOption,
    CandidateOptions,
    CandidateRobotDecision,
    PlannedAssignment,
    PlanningInput,
    RobotEligibility,
    RobotOption,
    ScheduleCandidate,
    build_assignment_plan,
    build_schedule_plan,
)

NOW = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


def candidate(
    room_id: str,
    *,
    due_at: datetime = NOW,
    confidence: float = 0.8,
    reason: str = "due",
) -> ScheduleCandidate:
    """Build the smallest fully typed planner candidate."""

    return ScheduleCandidate(
        room_id=room_id,
        floor_id="ground",
        operation=CleaningOperation.VACUUM,
        due_at=due_at,
        confidence=confidence,
        reason=reason,
        duration_minutes=20,
        duration_sample_count=3,
        passes=1,
        occurrence=None,
        evaluated_at=NOW,
        unresolved_window_allowed=False,
        bypass_forecast=False,
        manual_override=False,
        source=OccurrenceSource.SCHEDULER,
    )


class AssignmentPlannerTests(unittest.TestCase):
    def test_equal_last_clean_uses_due_time_confidence_then_ordinal(self) -> None:
        last_cleaned_at = NOW - timedelta(days=7)
        inputs = (
            CandidateOptions(
                CandidateOption(
                    "ordinal", NOW, 0.8, 2, last_cleaned_at=last_cleaned_at
                ),
                (RobotOption("r3", 40, True, "ready"),),
            ),
            CandidateOptions(
                CandidateOption(
                    "later",
                    NOW + timedelta(minutes=1),
                    1.0,
                    0,
                    last_cleaned_at=last_cleaned_at,
                ),
                (RobotOption("r4", 100, True, "ready"),),
            ),
            CandidateOptions(
                CandidateOption(
                    "confidence", NOW, 0.9, 5, last_cleaned_at=last_cleaned_at
                ),
                (RobotOption("r2", 50, True, "ready"),),
            ),
            CandidateOptions(
                CandidateOption("first", NOW, 0.8, 1, last_cleaned_at=last_cleaned_at),
                (RobotOption("r1", 60, True, "ready"),),
            ),
        )

        plan = build_assignment_plan(inputs)

        self.assertEqual(
            plan.ordered_room_ids,
            ("confidence", "first", "ordinal", "later"),
        )

    def test_oldest_clean_wins_even_when_another_room_was_due_first(self) -> None:
        shared = (RobotOption("rob", 80, True, "ready"),)
        plan = build_assignment_plan(
            (
                CandidateOptions(
                    CandidateOption(
                        "kitchen",
                        NOW - timedelta(hours=8),
                        0.8,
                        0,
                        last_cleaned_at=NOW - timedelta(hours=46),
                    ),
                    shared,
                ),
                CandidateOptions(
                    CandidateOption(
                        "rumpus",
                        NOW - timedelta(hours=3),
                        0.8,
                        1,
                        last_cleaned_at=NOW - timedelta(days=12),
                    ),
                    shared,
                ),
            )
        )

        self.assertEqual(plan.ordered_room_ids, ("rumpus", "kitchen"))
        self.assertEqual(
            plan.assignments,
            (PlannedAssignment("rumpus", "rob"),),
        )
        self.assertEqual(plan.blocks, (("kitchen", "ready"),))

    def test_never_cleaned_room_precedes_known_completion(self) -> None:
        shared = (RobotOption("rob", 80, True, "ready"),)
        plan = build_assignment_plan(
            (
                CandidateOptions(
                    CandidateOption(
                        "known",
                        NOW - timedelta(hours=8),
                        0.8,
                        0,
                        last_cleaned_at=NOW - timedelta(days=30),
                    ),
                    shared,
                ),
                CandidateOptions(
                    CandidateOption("never", NOW - timedelta(hours=1), 0.8, 1),
                    shared,
                ),
            )
        )

        self.assertEqual(plan.ordered_room_ids, ("never", "known"))
        self.assertEqual(plan.assignments, (PlannedAssignment("never", "rob"),))

    def test_ineligible_oldest_room_does_not_reserve_robot(self) -> None:
        plan = build_assignment_plan(
            (
                CandidateOptions(
                    CandidateOption(
                        "oldest",
                        NOW - timedelta(days=2),
                        0.8,
                        0,
                        last_cleaned_at=NOW - timedelta(days=30),
                    ),
                    (RobotOption("rob", 80, False, "room fault"),),
                ),
                CandidateOptions(
                    CandidateOption(
                        "newer",
                        NOW - timedelta(days=1),
                        0.8,
                        1,
                        last_cleaned_at=NOW - timedelta(days=2),
                    ),
                    (RobotOption("rob", 80, True, "ready"),),
                ),
            )
        )

        self.assertEqual(plan.ordered_room_ids, ("oldest", "newer"))
        self.assertEqual(plan.blocks, (("oldest", "room fault"),))
        self.assertEqual(plan.assignments, (PlannedAssignment("newer", "rob"),))

    def test_assignment_prefers_battery_then_stable_robot_id(self) -> None:
        plan = build_assignment_plan(
            (
                CandidateOptions(
                    CandidateOption("study", NOW, 1.0, 0),
                    (
                        RobotOption("registry-a", None, True, "ready"),
                        RobotOption("registry-b", 90, True, "ready"),
                        RobotOption("registry-c", 90, True, "ready"),
                    ),
                ),
            )
        )

        self.assertEqual(plan.assignments[0].robot_id, "registry-c")

    def test_each_robot_is_used_once_and_remaining_room_is_blocked(self) -> None:
        shared = (RobotOption("registry-a", 80, True, "ready"),)
        plan = build_assignment_plan(
            (
                CandidateOptions(CandidateOption("study", NOW, 1.0, 0), shared),
                CandidateOptions(CandidateOption("hall", NOW, 0.9, 1), shared),
            )
        )

        self.assertEqual(
            tuple((item.room_id, item.robot_id) for item in plan.assignments),
            (("study", "registry-a"),),
        )
        self.assertEqual(plan.blocks, (("hall", "ready"),))

    def test_ineligible_reason_is_preserved_for_dashboard_diagnostics(self) -> None:
        plan = build_assignment_plan(
            (
                CandidateOptions(
                    CandidateOption("bedroom", NOW, 1.0, 0),
                    (
                        RobotOption(
                            "registry-a",
                            100,
                            False,
                            "occupancy occupied",
                        ),
                    ),
                ),
            )
        )

        self.assertEqual(plan.assignments, ())
        self.assertEqual(
            plan.blocks,
            (("bedroom", "occupancy occupied"),),
        )


class WholeSchedulePlannerTests(unittest.TestCase):
    def test_resolved_candidate_and_all_robot_diagnostics_are_retained(self) -> None:
        base = candidate("study")
        resolved = replace(base, duration_minutes=24, reason="resolved for Alpha")
        decisions = (
            CandidateRobotDecision(
                RobotEligibility("registry-a", "Alpha", True, "ready"),
                resolved,
            ),
            CandidateRobotDecision(
                RobotEligibility("registry-b", "Beta", False, "battery below 80%"),
            ),
        )

        plan = build_schedule_plan(
            (
                PlanningInput(
                    candidate=base,
                    decisions=decisions,
                    ordinal=0,
                    battery_by_robot=(("registry-a", 90), ("registry-b", 70)),
                ),
            )
        )

        self.assertEqual(plan.assignments[0].robot_id, "registry-a")
        self.assertEqual(plan.assignments[0].candidate.duration_minutes, 24)
        self.assertEqual(
            plan.assignments[0].candidate.robot_eligibility,
            tuple(item.eligibility for item in decisions),
        )
        self.assertEqual(plan.candidates, (plan.assignments[0].candidate,))

    def test_block_reason_replaces_only_the_public_candidate_reason(self) -> None:
        base = candidate("study", reason="original")
        plan = build_schedule_plan(
            (
                PlanningInput(
                    candidate=base,
                    decisions=(
                        CandidateRobotDecision(
                            RobotEligibility(
                                "registry-a",
                                "Alpha",
                                False,
                                "not docked",
                            )
                        ),
                    ),
                    ordinal=0,
                    battery_by_robot=(("registry-a", 100),),
                ),
            )
        )

        self.assertEqual(plan.assignments, ())
        self.assertEqual(plan.blocks, (("study", "not docked"),))
        self.assertEqual(plan.candidates[0].reason, "not docked")
        self.assertEqual(base.reason, "original")


if __name__ == "__main__":
    unittest.main()
