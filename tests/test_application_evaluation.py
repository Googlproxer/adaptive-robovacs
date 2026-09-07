"""End-to-end application transaction tests for scheduled evaluation."""

from __future__ import annotations

import unittest
from dataclasses import replace
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from custom_components.adaptive_robovacs.application import SchedulerApplication
from custom_components.adaptive_robovacs.models import OccurrenceSource
from custom_components.adaptive_robovacs.planner import (
    PlannedCandidateAssignment,
    WholeSchedulePlan,
)
from custom_components.adaptive_robovacs.state import SchedulerFault
from tests.test_application_dispatch import resolved_candidate, transaction_application
from tests.test_application_state import NOW


def evaluation_application() -> SchedulerApplication:
    app = transaction_application()
    app.hass.bus = SimpleNamespace(async_fire=Mock())
    app._expire_robot_cooldowns = Mock()
    app._observe_occupancy = Mock()
    app._async_reconcile_jobs = AsyncMock()
    app._refresh_robot_readiness = Mock()
    app._async_save = AsyncMock()
    app._async_dispatch = AsyncMock(return_value=(True, "started"))
    return app


async def evaluate(
    app: SchedulerApplication, *, dry_run: bool = False, reason: str = "test"
):
    """Call the real method despite the fixture's callback mock."""

    with patch(
        "custom_components.adaptive_robovacs.application.core._now", return_value=NOW
    ):
        return await SchedulerApplication.async_evaluate(
            app, dry_run=dry_run, reason=reason
        )


class EvaluationPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_closing_and_shutdown_never_observe_or_dispatch(self) -> None:
        app = evaluation_application()
        app._closing = True
        result = await evaluate(app)
        self.assertEqual(result["dispatches"], ["coordinator shutting down"])
        app.async_refresh_discovery.assert_not_awaited()

        app = evaluation_application()
        app._shutdown_started = Mock(return_value=True)
        result = await evaluate(app)
        self.assertEqual(result["dispatches"], ["coordinator shutting down"])
        app.async_refresh_discovery.assert_not_awaited()

    async def test_dry_run_publishes_complete_typed_plan_preview(self) -> None:
        app = evaluation_application()
        result = await evaluate(app, dry_run=True, reason="dashboard")

        self.assertEqual(result["reason"], "dashboard")
        self.assertEqual(result["assignments"][0]["robot"], "vacuum.alpha")
        self.assertEqual(result["assignments"][0]["room"], "study")
        self.assertEqual(result["candidates"][0]["operation"], "cleaning")
        self.assertTrue(result["candidates"][0]["eligible"])
        self.assertEqual(result["dispatches"], [])
        app.async_refresh_discovery.assert_awaited_once_with(notify=False)
        app._async_reconcile_jobs.assert_awaited_once_with(NOW)
        app._async_save.assert_awaited_once()
        app._notify_listeners.assert_called_once()
        app.hass.bus.async_fire.assert_called_once()

    async def test_preview_records_blocked_rooms_and_unassigned_candidates(
        self,
    ) -> None:
        app = evaluation_application()
        app.state.room_settings["study"].enabled = False
        result = await evaluate(app, dry_run=True)
        self.assertEqual(result["blocks"]["study"], "room disabled")
        self.assertEqual(result["candidates"], [])

        app = evaluation_application()
        app._candidate_robot_diagnostics = Mock(return_value=())
        app._record_room_decision = Mock()
        result = await evaluate(app, dry_run=True)
        self.assertEqual(result["assignments"], [])
        self.assertIn("study", result["blocks"])
        app._record_room_decision.assert_called()

    async def test_non_dispatch_modes_report_the_active_global_gate(self) -> None:
        cases = (
            (
                lambda item: item.state.room_faults.__setitem__(
                    "study",
                    SchedulerFault(
                        "blocked", "registry-alpha", "study", NOW, "dispatch"
                    ),
                ),
                "scheduler limited to unaffected robots and rooms",
            ),
            (
                lambda item: setattr(item.state.global_settings, "observe_only", True),
                "observe-only mode",
            ),
            (
                lambda item: setattr(item.state.global_settings, "party_mode", True),
                "party mode",
            ),
        )
        for configure, message in cases:
            with self.subTest(message=message):
                app = evaluation_application()
                configure(app)
                result = await evaluate(app, dry_run=True)
                self.assertEqual(result["dispatches"], [message])


class EvaluationDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_revalidates_twice_and_dispatches_fresh_candidate(
        self,
    ) -> None:
        app = evaluation_application()
        app._async_prepare_occurrence = AsyncMock(
            side_effect=lambda _robot, candidate, _now: (candidate, None)
        )
        app._async_refresh_pending_profile_if_needed = AsyncMock(
            side_effect=lambda _robot, candidate: candidate
        )
        app._async_dispatch = AsyncMock(return_value=(True, "started Study"))

        result = await evaluate(app)

        self.assertEqual(result["dispatches"], ["started Study"])
        self.assertEqual(app._observe_occupancy.call_count, 3)
        app._async_prepare_occurrence.assert_awaited_once()
        app._async_refresh_pending_profile_if_needed.assert_awaited_once()
        app._async_dispatch.assert_awaited_once()

    async def test_failed_dispatch_is_reported_without_retry(self) -> None:
        app = evaluation_application()
        app._async_prepare_occurrence = AsyncMock(
            side_effect=lambda _robot, candidate, _now: (candidate, None)
        )
        app._async_refresh_pending_profile_if_needed = AsyncMock(
            side_effect=lambda _robot, candidate: candidate
        )
        app._async_dispatch = AsyncMock(return_value=(False, "safe failure"))
        result = await evaluate(app)
        self.assertEqual(result["dispatches"], ["safe failure"])
        app._async_dispatch.assert_awaited_once()

    async def test_prepare_revalidation_handles_disappearance_and_all_gate_failures(
        self,
    ) -> None:
        base = evaluation_application()
        candidate = replace(
            resolved_candidate(base),
            manual_override=False,
            source=OccurrenceSource.SCHEDULER,
        )
        plan = WholeSchedulePlan(
            (candidate,),
            (PlannedCandidateAssignment("vacuum.alpha", candidate),),
            (),
        )
        cases = (
            (
                True,
                (candidate, "ready"),
                (True, "ready"),
                candidate,
                "unavailable room",
            ),
            (
                False,
                (None, "room became occupied"),
                (True, "ready"),
                candidate,
                "Study: room became occupied",
            ),
            (
                False,
                (candidate, "ready"),
                (False, "robot moved"),
                candidate,
                "Study: robot moved",
            ),
            (
                False,
                (candidate, "ready"),
                (True, "ready"),
                None,
                "Study: cleaning program or vacancy forecast is no longer compatible",
            ),
        )
        for disappear, room_result, robot_result, resolved, expected in cases:
            with self.subTest(expected=expected):
                app = evaluation_application()
                app._room_candidate = Mock(
                    side_effect=((candidate, "ready"), room_result)
                )
                app._candidate_robot_diagnostics = Mock(return_value=())
                app._robot_ready = Mock(side_effect=((True, "ready"), robot_result))
                app._candidate_for_robot = Mock(return_value=resolved)
                if disappear:

                    async def save_then_remove(*, app=app) -> None:
                        app.discovery = replace(
                            app.discovery, rooms=MappingProxyType({})
                        )

                    app._async_save = AsyncMock(side_effect=save_then_remove)
                with patch(
                    "custom_components.adaptive_robovacs.application.evaluation."
                    "build_schedule_plan",
                    return_value=plan,
                ):
                    result = await evaluate(app)
                self.assertEqual(result["dispatches"], [f"waiting for {expected}"])
                app._async_dispatch.assert_not_awaited()

    async def test_preparation_message_stops_before_second_revalidation(self) -> None:
        app = evaluation_application()
        app._async_prepare_occurrence = AsyncMock(
            return_value=(None, "waiting for water confirmation")
        )
        result = await evaluate(app)
        self.assertEqual(result["dispatches"], ["waiting for water confirmation"])
        app._async_dispatch.assert_not_awaited()

        app = evaluation_application()
        app._async_prepare_occurrence = AsyncMock(return_value=(None, None))
        result = await evaluate(app)
        self.assertEqual(result["dispatches"], [])

    async def test_final_revalidation_rejects_every_changed_physical_gate(self) -> None:
        base = evaluation_application()
        candidate = replace(
            resolved_candidate(base),
            manual_override=False,
            source=OccurrenceSource.SCHEDULER,
        )
        plan = WholeSchedulePlan(
            (candidate,),
            (PlannedCandidateAssignment("vacuum.alpha", candidate),),
            (),
        )
        cases = (
            (
                True,
                (candidate, "ready"),
                (True, "ready"),
                candidate,
                "unavailable room",
            ),
            (False, (None, "occupied"), (True, "ready"), candidate, "Study: occupied"),
            (
                False,
                (candidate, "ready"),
                (False, "battery fell"),
                candidate,
                "Study: battery fell",
            ),
            (
                False,
                (candidate, "ready"),
                (True, "ready"),
                None,
                "Study: cleaning program is no longer compatible",
            ),
        )
        for disappear, fresh_room, fresh_robot, fresh_resolved, expected in cases:
            with self.subTest(expected=expected):
                app = evaluation_application()
                app._room_candidate = Mock(
                    side_effect=(
                        (candidate, "ready"),
                        (candidate, "ready"),
                        fresh_room,
                    )
                )
                app._candidate_robot_diagnostics = Mock(return_value=())
                app._robot_ready = Mock(
                    side_effect=(
                        (True, "ready"),
                        (True, "ready"),
                        fresh_robot,
                    )
                )
                app._candidate_for_robot = Mock(side_effect=(candidate, fresh_resolved))

                async def prepare(
                    _robot,
                    prepared,
                    _now,
                    *,
                    disappear=disappear,
                    app=app,
                ):
                    if disappear:
                        app.discovery = replace(
                            app.discovery, rooms=MappingProxyType({})
                        )
                    return prepared, None

                app._async_prepare_occurrence = AsyncMock(side_effect=prepare)
                with patch(
                    "custom_components.adaptive_robovacs.application.evaluation."
                    "build_schedule_plan",
                    return_value=plan,
                ):
                    result = await evaluate(app)
                self.assertEqual(result["dispatches"], [f"waiting for {expected}"])
                app._async_dispatch.assert_not_awaited()

    async def test_shutdown_between_assignments_aborts_without_outbound_call(
        self,
    ) -> None:
        app = evaluation_application()
        app._shutdown_started = Mock(side_effect=(False, False, True))
        result = await evaluate(app)
        self.assertEqual(result["dispatches"], [])
        app._async_dispatch.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
