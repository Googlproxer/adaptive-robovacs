"""Tests for the application command transaction router."""

from __future__ import annotations

import unittest
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

from custom_components.adaptive_robovacs.application import SchedulerApplication
from custom_components.adaptive_robovacs.commands import (
    ClearLegacyDeferralsCommand,
    CommandResult,
    EvaluateCommand,
    ExpireWaterConfirmationCommand,
    ManualCleanRoomCommand,
    ObservedManualCleanCommand,
    RecheckAndResumeCommand,
    RecheckCleaningProgramCommand,
    RecheckNotificationTargetsCommand,
    RecheckRoomFaultCommand,
    RecheckTwoPassCompatibilityCommand,
    RecordManualCleanCommand,
    RefreshDiscoveryCommand,
    SaveFloorPlanCommand,
    SetGlobalCommand,
    SetRobotSettingCommand,
    SetRoomAdjacencyCommand,
    SetRoomCleaningPeriodCommand,
    SetRoomCleaningProfileCommand,
    SetRoomSettingCommand,
    StateChangedCommand,
    StopAndReturnCommand,
    WaterConfirmationResponseCommand,
)
from custom_components.adaptive_robovacs.discovery import DiscoverySnapshot
from custom_components.adaptive_robovacs.floor_plans import FloorPlanWrite
from custom_components.adaptive_robovacs.models import (
    EvaluationCause,
    EvaluationMode,
    ManualCleanRequest,
    SchedulerHaltRecheckResult,
)
from tests.test_application_state import robot


def routed_application() -> SchedulerApplication:
    app = SchedulerApplication.__new__(SchedulerApplication)
    app.discovery = DiscoverySnapshot(
        MappingProxyType({"vacuum.alpha": robot()}),
        MappingProxyType({}),
    )
    app.async_evaluate = AsyncMock(return_value={"preview": True})
    app._async_refresh_discovery_after_device_label_change = AsyncMock()
    app.async_set_global = AsyncMock()
    app.async_set_room_setting = AsyncMock()
    app.async_set_robot_setting = AsyncMock()
    app.async_set_room_cleaning_period = AsyncMock()
    app.async_set_room_cleaning_profile = AsyncMock()
    app.async_manual_clean_room = AsyncMock(return_value={"manual": True})
    app.async_record_manual_clean = AsyncMock(return_value={"recorded": True})
    app._async_record_observed_manual_clean = AsyncMock()
    app._async_handle_water_confirmation = AsyncMock()
    app._async_expire_water_confirmation = AsyncMock()
    app.async_stop_and_return_to_dock = AsyncMock(return_value={"stopped": True})
    app.async_recheck_and_resume = AsyncMock(
        return_value=SchedulerHaltRecheckResult(True, "ready", "docked")
    )
    app.async_recheck_room_fault = AsyncMock(return_value=True)
    app.async_recheck_room_compatibility = AsyncMock(return_value=True)
    app.async_recheck_cleaning_program_compatibility = AsyncMock(return_value=True)
    app.async_clear_legacy_deferrals = AsyncMock(return_value={"cleared": 1})
    app.async_set_room_adjacency = AsyncMock(return_value={"revision": 2})
    app.async_save_floor_plan = AsyncMock(return_value={"revision": 3})

    app._notify_listeners = Mock()
    app._reset_room_recovery_dock = Mock()
    app.has_notification_targets = Mock(return_value=True)
    app.repairs = SimpleNamespace(set_notification_delivery_issue=Mock())
    return app


def payload(result: CommandResult | None):
    """Expose an immutable command result at this test boundary."""

    return result.as_response() if result else None


class ApplicationCommandRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_result_freezes_nested_boundary_payloads(self) -> None:
        source = {"nested": {"values": [1, 2]}}
        result = CommandResult.from_mapping(source)

        source["nested"]["values"].append(3)
        first = result.as_response()
        first["nested"]["values"].append(4)

        self.assertEqual(result.as_response(), {"nested": {"values": [1, 2]}})

    async def test_settings_floor_plan_and_fault_commands_route_once(self) -> None:
        app = routed_application()
        floor_write = FloorPlanWrite("ground", 1, (), (), ())
        cases = (
            (SetGlobalCommand("party_mode", True), app.async_set_global, None),
            (
                SetRoomSettingCommand("study", "enabled", True),
                app.async_set_room_setting,
                None,
            ),
            (
                SetRobotSettingCommand("vacuum.alpha", "enabled", True),
                app.async_set_robot_setting,
                None,
            ),
            (
                SetRoomCleaningPeriodCommand("study", "Weekly"),
                app.async_set_room_cleaning_period,
                None,
            ),
            (
                SetRoomCleaningProfileCommand("study", "Custom"),
                app.async_set_room_cleaning_profile,
                None,
            ),
            (
                RecheckRoomFaultCommand("study"),
                app.async_recheck_room_fault,
                {"cleared": True},
            ),
            (
                RecheckTwoPassCompatibilityCommand("study"),
                app.async_recheck_room_compatibility,
                {"cleared": True},
            ),
            (
                RecheckCleaningProgramCommand("study"),
                app.async_recheck_cleaning_program_compatibility,
                {"cleared": True},
            ),
            (
                ClearLegacyDeferralsCommand(("study",)),
                app.async_clear_legacy_deferrals,
                {"cleared": 1},
            ),
            (
                SetRoomAdjacencyCommand("study", ("hall",)),
                app.async_set_room_adjacency,
                {"revision": 2},
            ),
            (
                SaveFloorPlanCommand(floor_write),
                app.async_save_floor_plan,
                {"revision": 3},
            ),
        )
        for command, method, expected in cases:
            with self.subTest(command=type(command).__name__):
                before = method.await_count
                result = await app._async_execute_command(command)
                self.assertEqual(payload(result), expected)
                self.assertEqual(method.await_count, before + 1)

        result = await app._async_execute_command(
            RecheckAndResumeCommand("registry-alpha")
        )
        self.assertEqual(
            payload(result),
            {"cleared": True, "reason": "ready", "robot_state": "docked"},
        )
        self.assertEqual(
            payload(
                await app._async_execute_command(RecheckNotificationTargetsCommand())
            ),
            {"cleared": True},
        )
        app.repairs.set_notification_delivery_issue.assert_called_once_with(False)

    async def test_evaluation_state_and_manual_commands_preserve_payloads(self) -> None:
        app = routed_application()
        preview = await app._async_execute_command(
            EvaluateCommand(
                EvaluationMode.PREVIEW,
                EvaluationCause.SERVICE,
                detail="dashboard",
            )
        )
        self.assertEqual(payload(preview), {"preview": True})
        app.async_evaluate.assert_awaited_with(dry_run=True, reason="dashboard")

        await app._async_execute_command(
            StateChangedCommand("vacuum.alpha", "docked", "cleaning", None)
        )
        app.async_evaluate.assert_awaited_with(
            dry_run=False, reason="state:vacuum.alpha"
        )

        manual = await app._async_execute_command(
            ManualCleanRoomCommand("study", "vacuum_only", "context", "user")
        )
        self.assertEqual(payload(manual), {"manual": True})
        app.async_manual_clean_room.assert_awaited_once_with(
            "study", "vacuum_only", context_id="context", user_id="user"
        )
        recorded = await app._async_execute_command(
            RecordManualCleanCommand("vacuum.alpha", ("study",), ("vacuum",))
        )
        self.assertEqual(payload(recorded), {"recorded": True})
        app.async_record_manual_clean.assert_awaited_once_with(
            "vacuum.alpha", ["study"], ["vacuum"]
        )
        request = ManualCleanRequest("vacuum.alpha", ("study",))
        await app._async_execute_command(ObservedManualCleanCommand(request, "ctx"))
        app._async_record_observed_manual_clean.assert_awaited_once_with(request, "ctx")
        await app._async_execute_command(
            StopAndReturnCommand("vacuum.alpha", context=None)
        )
        app.async_stop_and_return_to_dock.assert_awaited_once_with(
            "vacuum.alpha", context=None
        )

    async def test_timer_discovery_and_water_commands_route(self) -> None:
        app = routed_application()
        await app._async_execute_command(RefreshDiscoveryCommand("labels"))
        app._async_refresh_discovery_after_device_label_change.assert_awaited_once()

        await app._async_execute_command(
            WaterConfirmationResponseCommand(
                action="confirm", request_id="request", tag="tag", dismissed=True
            )
        )
        app._async_handle_water_confirmation.assert_awaited_once_with(
            action="confirm", request_id="request", tag="tag", dismissed=True
        )
        await app._async_execute_command(ExpireWaterConfirmationCommand("request"))
        app._async_expire_water_confirmation.assert_awaited_once_with("request")


if __name__ == "__main__":
    unittest.main()
