"""Tests for the Home Assistant vacuum I/O gateway."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from custom_components.adaptive_robovacs.adapters.base import AdapterEntityEvidence
from custom_components.adaptive_robovacs.discovery import DiscoveredRobot, RobotProfile
from custom_components.adaptive_robovacs.gateway import HomeAssistantVacuumGateway
from custom_components.adaptive_robovacs.models import (
    AdapterCapabilities,
    AdapterCleaningProfile,
    AdapterDispatchRequest,
    AdapterDispatchResult,
    CleaningOperation,
    DispatchOutcome,
)


def make_robot(*, profile: RobotProfile | None = None) -> DiscoveredRobot:
    return DiscoveredRobot(
        entity_id="vacuum.alpha",
        name="Alpha",
        registry_id="registry-alpha",
        platform="test_vendor",
        device_id="device-alpha",
        dock_area_id="dock",
        floor_id="ground",
        supports_area_clean=True,
        supports_send_command=True,
        profile=profile or RobotProfile(),
        adapter_id="fake",
        adapter_schema_version=1,
        adapter_capabilities=AdapterCapabilities(
            adapter_id="fake",
            schema_version=1,
            portable_area_clean=True,
            fan_speed_options=("quiet", "max"),
            supported_pass_counts=frozenset({1}),
        ),
        adapter_entities=(
            AdapterEntityEvidence(
                entity_id="select.alpha_mode",
                domain="select",
                platform="test_vendor",
                device_class=None,
                translation_key=None,
                options=("vacuum", "mop"),
                state="stale",
            ),
        ),
    )


def request() -> AdapterDispatchRequest:
    return AdapterDispatchRequest(
        robot_entity_id="vacuum.alpha",
        area_ids=("study",),
        operation=CleaningOperation.VACUUM,
        passes=1,
        cleaning_profile=AdapterCleaningProfile(fan_speed="quiet"),
    )


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_dispatch_phase_uses_fresh_adapter_state(self) -> None:
        state = SimpleNamespace(state="vacuum")
        states = SimpleNamespace(get=Mock(return_value=state))
        hass = SimpleNamespace(states=states)
        adapter = SimpleNamespace(
            async_preflight=AsyncMock(
                return_value=AdapterDispatchResult(
                    DispatchOutcome.READY, "ready", "preflight"
                )
            ),
            async_validate_profile=AsyncMock(
                return_value=AdapterDispatchResult(
                    DispatchOutcome.READY, "ready", "validate"
                )
            ),
            async_apply_profile=AsyncMock(
                return_value=AdapterDispatchResult(
                    DispatchOutcome.READY, "ready", "profile"
                )
            ),
            async_dispatch=AsyncMock(
                return_value=AdapterDispatchResult(
                    DispatchOutcome.ACCEPTED, "accepted", "dispatch"
                )
            ),
        )
        gateway = HomeAssistantVacuumGateway(hass, lambda: True)
        robot = make_robot()
        dispatch_request = request()

        with patch(
            "custom_components.adaptive_robovacs.gateway.adapter_for_id",
            return_value=adapter,
        ) as resolve:
            results = (
                await gateway.async_preflight(robot, dispatch_request),
                await gateway.async_validate_profile(robot, dispatch_request),
                await gateway.async_apply_profile(robot, dispatch_request),
                await gateway.async_dispatch(robot, dispatch_request),
            )

        self.assertEqual(
            [item.summary for item in results],
            ["preflight", "validate", "profile", "dispatch"],
        )
        self.assertEqual(resolve.call_count, 4)
        for method in (
            adapter.async_preflight,
            adapter.async_validate_profile,
            adapter.async_apply_profile,
            adapter.async_dispatch,
        ):
            context = method.await_args.args[1]
            self.assertEqual(context.entities[0].state, "vacuum")
            self.assertTrue(context.can_mutate())

    def test_profile_readiness_checks_options_passes_and_fan_speed(self) -> None:
        profile = RobotProfile(
            mode_select_entity_id="select.mode",
            mop_mode_select_entity_id="select.route",
            mop_intensity_select_entity_id="select.water",
            passes_select_entity_id="select.passes",
        )
        states_by_id = {
            "select.mode": SimpleNamespace(
                state="vacuum", attributes={"options": ["vacuum"]}
            ),
            "select.route": SimpleNamespace(
                state="standard", attributes={"options": ["standard"]}
            ),
            "select.water": SimpleNamespace(
                state="medium", attributes={"options": ["medium"]}
            ),
            "select.passes": SimpleNamespace(
                state="single", attributes={"options": ["Single pass", "Double-pass"]}
            ),
        }
        hass = SimpleNamespace(
            states=SimpleNamespace(get=lambda entity_id: states_by_id.get(entity_id))
        )
        gateway = HomeAssistantVacuumGateway(hass, lambda: True)
        robot = make_robot(profile=profile)
        ready_profile = AdapterCleaningProfile(
            mode="vacuum",
            mop_mode="standard",
            mop_intensity="medium",
            fan_speed="quiet",
        )

        self.assertTrue(gateway.profile_is_ready(robot, "vacuum", 2, ready_profile))
        self.assertFalse(
            gateway.profile_is_ready(
                robot,
                "vacuum",
                2,
                AdapterCleaningProfile(
                    mode="missing",
                    mop_mode="standard",
                    mop_intensity="medium",
                    fan_speed="quiet",
                ),
            )
        )
        states_by_id["select.mode"] = SimpleNamespace(
            state="unavailable", attributes={"options": ["vacuum"]}
        )
        self.assertFalse(gateway.profile_is_ready(robot, "vacuum", 2, ready_profile))
        states_by_id["select.mode"] = SimpleNamespace(
            state="vacuum", attributes={"options": ["vacuum"]}
        )
        states_by_id["select.passes"] = SimpleNamespace(
            state="single", attributes={"options": ["Single pass"]}
        )
        self.assertFalse(gateway.profile_is_ready(robot, "vacuum", 2, ready_profile))
        self.assertFalse(
            gateway.profile_is_ready(
                robot,
                "vacuum",
                1,
                AdapterCleaningProfile(
                    mode="vacuum",
                    mop_mode="standard",
                    mop_intensity="medium",
                    fan_speed="impossible",
                ),
            )
        )

    async def test_return_to_dock_obeys_the_shutdown_gate(self) -> None:
        call = AsyncMock()
        allowed = False
        gateway = HomeAssistantVacuumGateway(
            SimpleNamespace(services=SimpleNamespace(async_call=call)),
            lambda: allowed,
        )

        await gateway.async_return_to_dock("vacuum.alpha", None)
        call.assert_not_awaited()

        allowed = True
        await gateway.async_return_to_dock("vacuum.alpha", None)
        call.assert_awaited_once_with(
            "vacuum",
            "return_to_base",
            {"entity_id": "vacuum.alpha"},
            blocking=True,
            context=None,
        )


if __name__ == "__main__":
    unittest.main()
