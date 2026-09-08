"""Fake-gateway tests for checkpointed and fail-closed dispatch."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime

from custom_components.adaptive_robovacs.discovery import (
    DiscoveredRobot,
    DiscoveredRoom,
    RobotProfile,
)
from custom_components.adaptive_robovacs.dispatch import (
    DispatchDependencies,
    DispatchPipeline,
)
from custom_components.adaptive_robovacs.models import (
    AdapterCapabilities,
    AdapterCleaningProfile,
    AdapterDispatchResult,
    CleaningOperation,
    DispatchOutcome,
    WaterReadiness,
)
from custom_components.adaptive_robovacs.planner import ScheduleCandidate

WHEN = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


def robot() -> DiscoveredRobot:
    capabilities = AdapterCapabilities(
        adapter_id="fake",
        schema_version=1,
        portable_area_clean=True,
        supported_pass_counts=frozenset({1}),
        supported_operations=frozenset({"vacuum", "mop"}),
        water_readiness=WaterReadiness("ready", "ready", ready=True),
        vacuum_pass_counts=frozenset({1}),
        mop_pass_counts=frozenset({1}),
    )
    return DiscoveredRobot(
        entity_id="vacuum.alpha",
        name="Alpha",
        registry_id="registry-alpha",
        platform="fake",
        device_id="device-alpha",
        dock_area_id="dock",
        floor_id="ground",
        supports_area_clean=True,
        supports_send_command=False,
        profile=RobotProfile(),
        adapter_id="fake",
        adapter_schema_version=1,
        adapter_capabilities=capabilities,
    )


def candidate(operation: str = "vacuum") -> ScheduleCandidate:
    return ScheduleCandidate(
        room_id="study",
        floor_id="ground",
        operation=operation,
        due_at=WHEN,
        confidence=1.0,
        reason="due",
        duration_minutes=20,
        duration_sample_count=3,
        passes=1,
        occurrence=None,
        evaluated_at=WHEN,
        unresolved_window_allowed=False,
        bypass_forecast=False,
        manual_override=False,
        source="scheduler",
    )


class _Gateway:
    def __init__(self) -> None:
        self.events = []
        self.preflight = AdapterDispatchResult(DispatchOutcome.READY, "ready", "Ready")
        self.profile = AdapterDispatchResult(DispatchOutcome.READY, "ready", "Ready")
        self.applied = AdapterDispatchResult(DispatchOutcome.READY, "ready", "Ready")
        self.dispatched = AdapterDispatchResult(
            DispatchOutcome.ACCEPTED, "accepted", "Accepted"
        )
        self.dispatch_error = None
        self.preflight_error = None
        self.profile_error = None
        self.apply_error = None
        self.requests = []

    async def async_preflight(self, _robot, _request):
        self.events.append("observe-preflight")
        self.requests.append(_request)
        if self.preflight_error:
            raise self.preflight_error
        return self.preflight

    async def async_validate_profile(self, _robot, _request):
        self.events.append("validate-profile")
        self.requests.append(_request)
        if self.profile_error:
            raise self.profile_error
        return self.profile

    async def async_apply_profile(self, _robot, _request):
        self.events.append("apply-profile")
        self.requests.append(_request)
        if self.apply_error:
            raise self.apply_error
        return self.applied

    async def async_dispatch(self, _robot, _request):
        self.events.append("dispatch")
        if self.dispatch_error:
            raise self.dispatch_error
        return self.dispatched

    def profile_is_ready(self, *_args):
        return True

    async def async_return_to_dock(self, *_args):
        self.events.append("dock")


class DispatchPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.gateway = _Gateway()
        self.closing = False
        self.faults = []
        self.events = self.gateway.events

        async def latch(*values):
            self.faults.append(values)

        async def checkpoint(_robot, _active):
            self.events.append("checkpoint")

        async def abandon(_robot):
            self.events.append("abandon")

        async def accept(*_values):
            self.events.append("accepted")

        async def mop_preflight(*_values):
            self.events.append("mop-blocked")

        async def mop_mode(*_values):
            self.events.append("mop-profile-blocked")

        async def downgrade(*_values):
            self.events.append("downgraded")

        room = DiscoveredRoom("study", "Study", "ground", frozenset())
        self.pipeline = DispatchPipeline(
            self.gateway,
            DispatchDependencies(
                room_for_id=lambda area_id: room if area_id == "study" else None,
                is_closing=lambda: self.closing,
                async_latch_fault=latch,
                async_handle_mop_preflight=mop_preflight,
                async_handle_mop_mode=mop_mode,
                async_downgrade_max_plus=downgrade,
                async_checkpoint=checkpoint,
                async_abandon_checkpoint=abandon,
                async_accept=accept,
            ),
        )

    async def test_fresh_adjacency_block_abandons_unstarted_checkpoint_without_fault(
        self,
    ):
        def start_block(_candidate, _now):
            self.assertEqual(self.events[-1], "checkpoint")
            return "Adjacent room protection: Bedroom (occupied)"

        self.pipeline._dependencies = replace(
            self.pipeline._dependencies, start_block_reason=start_block
        )
        accepted, reason = await self.pipeline.async_dispatch(
            robot(), candidate(), WHEN
        )
        self.assertFalse(accepted)
        self.assertIn("Bedroom (occupied)", reason)
        self.assertEqual(self.events[-1], "abandon")
        self.assertNotIn("dispatch", self.events)
        self.assertFalse(self.faults)

    async def test_clear_final_gate_retains_checkpoint_before_dispatch(self):
        self.pipeline._dependencies = replace(
            self.pipeline._dependencies, start_block_reason=lambda _c, _n: None
        )
        accepted, _ = await self.pipeline.async_dispatch(robot(), candidate(), WHEN)
        self.assertTrue(accepted)
        self.assertLess(self.events.index("checkpoint"), self.events.index("dispatch"))

    async def test_checkpoints_before_the_only_outbound_start(self) -> None:
        accepted, message = await self.pipeline.async_dispatch(
            robot(), candidate(), WHEN
        )

        self.assertTrue(accepted)
        self.assertEqual(message, "dispatched Study")
        self.assertEqual(
            self.events,
            [
                "observe-preflight",
                "validate-profile",
                "apply-profile",
                "checkpoint",
                "dispatch",
                "accepted",
            ],
        )

    async def test_uncertain_start_is_not_abandoned_or_retried(self) -> None:
        self.gateway.dispatch_error = RuntimeError("private vendor detail")

        accepted, message = await self.pipeline.async_dispatch(
            robot(), candidate(), WHEN
        )

        self.assertFalse(accepted)
        self.assertEqual(
            message,
            "Home Assistant could not start the room clean.",
        )
        self.assertEqual(self.events.count("dispatch"), 1)
        self.assertIn("checkpoint", self.events)
        self.assertNotIn("abandon", self.events)
        self.assertEqual(self.faults[0][-2:], (False, True))

    async def test_shutdown_causes_zero_gateway_calls(self) -> None:
        self.closing = True

        accepted, message = await self.pipeline.async_dispatch(
            robot(), candidate(), WHEN
        )

        self.assertFalse(accepted)
        self.assertEqual(message, "coordinator shutting down")
        self.assertEqual(self.events, [])

    async def test_water_block_skips_mop_without_latching_a_fault(self) -> None:
        self.gateway.preflight = AdapterDispatchResult(
            DispatchOutcome.BLOCKED,
            "water_not_ready",
            "private vendor detail",
        )

        accepted, message = await self.pipeline.async_dispatch(
            robot(), candidate(CleaningOperation.MOP), WHEN
        )

        self.assertTrue(accepted)
        self.assertEqual(message, "skipped mopping Study: water unavailable")
        self.assertEqual(self.faults, [])
        self.assertEqual(self.events, ["observe-preflight", "mop-blocked"])

    async def test_profile_helpers_build_typed_requests_and_strip_mop_controls(
        self,
    ) -> None:
        item = robot()
        profile = AdapterCleaningProfile(
            fan_speed="max", mop_mode="deep", mop_intensity="high"
        )
        await self.pipeline.async_apply_profile(item, "vacuum", 1, profile)
        request = self.gateway.requests[-1]
        self.assertEqual(request.operation, CleaningOperation.VACUUM)
        self.assertEqual(request.cleaning_profile.fan_speed, "max")
        self.assertIsNone(request.cleaning_profile.mop_mode)
        self.assertIsNone(request.cleaning_profile.mop_intensity)

        await self.pipeline.async_apply_profile(item, "mop", 1, profile)
        request = self.gateway.requests[-1]
        self.assertIsNone(request.cleaning_profile.mop_mode)
        self.assertIsNone(request.cleaning_profile.mop_intensity)

        result = await self.pipeline.async_preflight(item, candidate())
        self.assertTrue(result.ready)
        result = await self.pipeline.async_validate_profile(item, candidate())
        self.assertTrue(result.ready)
        self.assertTrue(self.pipeline.profile_is_ready(item, "vacuum", 1))

    async def test_missing_room_and_shutdown_at_each_boundary_are_fail_closed(
        self,
    ) -> None:
        accepted, message = await self.pipeline.async_dispatch(
            robot(), replace(candidate(), room_id="missing"), WHEN
        )
        self.assertFalse(accepted)
        self.assertEqual(message, "room is no longer discovered")

        for stop_on_call, expected_events in (
            (2, []),
            (3, ["observe-preflight", "validate-profile"]),
            (
                4,
                ["observe-preflight", "validate-profile", "apply-profile"],
            ),
            (
                5,
                [
                    "observe-preflight",
                    "validate-profile",
                    "apply-profile",
                    "checkpoint",
                    "abandon",
                ],
            ),
        ):
            with self.subTest(stop_on_call=stop_on_call):
                self.gateway.events.clear()
                calls = 0

                def is_closing(*, stop_on_call=stop_on_call) -> bool:
                    nonlocal calls
                    calls += 1
                    return calls == stop_on_call

                self.pipeline._dependencies = replace(
                    self.pipeline._dependencies, is_closing=is_closing
                )
                accepted, message = await self.pipeline.async_dispatch(
                    robot(), candidate(), WHEN
                )
                self.assertFalse(accepted)
                self.assertEqual(message, "coordinator shutting down")
                self.assertEqual(self.gateway.events, expected_events)

    async def test_each_adapter_phase_exception_uses_stable_safe_fault(self) -> None:
        for attribute, expected_code, phase in (
            ("preflight_error", "adapter_preflight_failed", "adapter_preflight"),
            ("profile_error", "profile_validation_failed", "profile_preflight"),
            ("apply_error", "profile_apply_failed", "profile_apply"),
        ):
            with self.subTest(attribute=attribute):
                self.setUp()
                setattr(self.gateway, attribute, RuntimeError("private detail"))
                accepted, _message = await self.pipeline.async_dispatch(
                    robot(), candidate(), WHEN
                )
                self.assertFalse(accepted)
                self.assertEqual(self.faults[0][2:4], (expected_code, phase))

    async def test_non_ready_preflight_profiles_and_apply_results_are_latched(
        self,
    ) -> None:
        for phase, result, operation, expected_event in (
            (
                "preflight",
                AdapterDispatchResult(
                    DispatchOutcome.FAILED, "mapping_missing", "private"
                ),
                "vacuum",
                "adapter_preflight",
            ),
            (
                "profile",
                AdapterDispatchResult(
                    DispatchOutcome.BLOCKED, "fan_speed_invalid", "private"
                ),
                "vacuum",
                "profile_preflight",
            ),
            (
                "applied",
                AdapterDispatchResult(
                    DispatchOutcome.FAILED, "profile_write_failed", "private"
                ),
                "vacuum",
                "profile_apply",
            ),
        ):
            with self.subTest(phase=phase):
                self.setUp()
                setattr(self.gateway, phase, result)
                accepted, _message = await self.pipeline.async_dispatch(
                    robot(), candidate(operation), WHEN
                )
                self.assertFalse(accepted)
                self.assertEqual(self.faults[0][3], expected_event)

        for phase in ("profile", "applied"):
            with self.subTest(safe_mop_phase=phase):
                self.setUp()
                setattr(
                    self.gateway,
                    phase,
                    AdapterDispatchResult(
                        DispatchOutcome.BLOCKED,
                        "mop_only_mode_unconfirmed",
                        "private",
                    ),
                )
                accepted, message = await self.pipeline.async_dispatch(
                    robot(), candidate("mop"), WHEN
                )
                self.assertTrue(accepted)
                self.assertIn("mop profile unavailable", message)
                self.assertIn("mop-profile-blocked", self.events)
                self.assertEqual(self.faults, [])

    async def test_rejected_dispatch_downgrades_only_q10_max_plus_failures(
        self,
    ) -> None:
        for code, downgraded in (
            ("q10_max_plus_profile_write_failed", True),
            ("q10_max_plus_start_failed", True),
            ("generic_dispatch_rejected", False),
        ):
            with self.subTest(code=code):
                self.setUp()
                self.gateway.dispatched = AdapterDispatchResult(
                    DispatchOutcome.FAILED,
                    code,
                    "private",
                    native_attempted=True,
                    outcome_uncertain=False,
                )
                accepted, _message = await self.pipeline.async_dispatch(
                    robot(), candidate(), WHEN
                )
                self.assertFalse(accepted)
                self.assertEqual("downgraded" in self.events, downgraded)
                self.assertEqual(self.faults[0][2], code)


if __name__ == "__main__":
    unittest.main()
