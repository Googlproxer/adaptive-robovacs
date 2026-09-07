"""Behavioral tests for Home Assistant Repair flows."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState

from custom_components.adaptive_robovacs.commands import (
    AcknowledgeRetiredMapCommand,
    AcknowledgeRobotErrorCommand,
    AcknowledgeRoomRecoveryCommand,
    CommandResult,
    RecheckAndResumeCommand,
    RecheckCleaningProgramCommand,
    RecheckNotificationTargetsCommand,
    RecheckRoomFaultCommand,
    RecheckTwoPassCompatibilityCommand,
)
from custom_components.adaptive_robovacs.repairs import (
    CleaningProgramCompatibilityRepairFlow,
    ErrorRecoveryRepairFlow,
    NotificationDeliveryRepairFlow,
    RobotDispatchFaultRepairFlow,
    RoomDispatchFaultRepairFlow,
    TwoPassCompatibilityRepairFlow,
    async_create_fix_flow,
)
from custom_components.adaptive_robovacs.repairs_manager import (
    cleaning_program_issue_id,
    notification_delivery_issue_id,
    retired_map_hold_issue_id,
    robot_dispatch_fault_issue_id,
    robot_error_recovery_issue_id,
    room_dispatch_fault_issue_id,
    room_recovery_issue_id,
    two_pass_issue_id,
)
from custom_components.adaptive_robovacs.runtime_data import (
    AdaptiveRoboVacsRuntimeData,
)


class RepairFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_error_recovery_requires_confirmation_and_keeps_failed_issue_open(
        self,
    ):
        for command in (
            AcknowledgeRetiredMapCommand("registry-alpha", "timestamp"),
            AcknowledgeRoomRecoveryCommand("study", "episode-1"),
            AcknowledgeRobotErrorCommand("registry-alpha", "timestamp"),
        ):
            submit = AsyncMock(
                return_value=CommandResult.from_mapping(
                    {"cleared": False, "reason": "awaiting_safe_dock"}
                )
            )
            flow = ErrorRecoveryRepairFlow(submit, command)
            with patch(
                "custom_components.adaptive_robovacs.repairs._description_placeholders",
                return_value={},
            ):
                self.assertEqual(
                    (await flow.async_step_init({"confirm": True}))["type"].value,
                    "form",
                )
                submit.assert_not_awaited()
                self.assertEqual(
                    (await flow.async_step_confirm({}))["errors"]["base"],
                    "awaiting_safe_dock",
                )
                submit.assert_awaited_once_with(command)
                submit.return_value = None
                self.assertEqual(
                    (await flow.async_step_confirm({}))["errors"]["base"],
                    "recovery_unavailable",
                )
                submit.return_value = CommandResult.from_mapping({"cleared": True})
                self.assertEqual(
                    (await flow.async_step_confirm({}))["type"].value, "create_entry"
                )

    async def test_each_flow_submits_its_typed_recheck_only_after_confirmation(
        self,
    ) -> None:
        cases = (
            (
                RobotDispatchFaultRepairFlow,
                "registry-alpha",
                RecheckAndResumeCommand,
            ),
            (RoomDispatchFaultRepairFlow, "study", RecheckRoomFaultCommand),
            (
                TwoPassCompatibilityRepairFlow,
                "study",
                RecheckTwoPassCompatibilityCommand,
            ),
            (
                CleaningProgramCompatibilityRepairFlow,
                "study",
                RecheckCleaningProgramCommand,
            ),
        )
        for flow_type, identifier, command_type in cases:
            with self.subTest(flow=flow_type.__name__):
                submit = AsyncMock(
                    return_value=CommandResult.from_mapping({"cleared": True})
                )
                flow = flow_type(submit, identifier)
                with patch(
                    "custom_components.adaptive_robovacs.repairs."
                    "_description_placeholders",
                    return_value=None,
                ):
                    opened = await flow.async_step_init({"ignored": "opening"})
                    self.assertEqual(opened["type"].value, "form")
                    submit.assert_not_awaited()
                    completed = await flow.async_step_confirm({})
                self.assertEqual(completed["type"].value, "create_entry")
                self.assertIsInstance(submit.await_args.args[0], command_type)

    async def test_failed_rechecks_remain_open_with_safe_errors(self) -> None:
        robot_submit = AsyncMock(
            return_value=CommandResult.from_mapping(
                {"cleared": False, "reason": "robot_still_cleaning"}
            )
        )
        room_submit = AsyncMock(return_value=None)
        notification_submit = AsyncMock(
            return_value=CommandResult.from_mapping({"cleared": False})
        )
        flows = (
            (
                RobotDispatchFaultRepairFlow(robot_submit, "registry-alpha"),
                "robot_still_cleaning",
            ),
            (RoomDispatchFaultRepairFlow(room_submit, "study"), "recheck_failed"),
            (NotificationDeliveryRepairFlow(notification_submit), "recheck_failed"),
        )
        with patch(
            "custom_components.adaptive_robovacs.repairs._description_placeholders",
            return_value={"reason": "safe"},
        ):
            for flow, expected_error in flows:
                with self.subTest(flow=type(flow).__name__):
                    result = await flow.async_step_confirm({})
                    self.assertEqual(result["type"].value, "form")
                    self.assertEqual(result["errors"]["base"], expected_error)
                    self.assertEqual(
                        result["description_placeholders"], {"reason": "safe"}
                    )

    async def test_notification_flow_completes_when_a_target_exists(self) -> None:
        submit = AsyncMock(return_value=CommandResult.from_mapping({"cleared": True}))
        flow = NotificationDeliveryRepairFlow(submit)
        with patch(
            "custom_components.adaptive_robovacs.repairs._description_placeholders",
            return_value=None,
        ):
            opened = await flow.async_step_init({"ignored": "opening"})
            completed = await flow.async_step_confirm({})

        self.assertEqual(opened["type"].value, "form")
        self.assertEqual(completed["type"].value, "create_entry")
        self.assertIsInstance(
            submit.await_args.args[0], RecheckNotificationTargetsCommand
        )

    async def test_factory_resolves_only_loaded_typed_runtime_entries(self) -> None:
        application = SimpleNamespace(async_execute=AsyncMock())
        runtime = AdaptiveRoboVacsRuntimeData(
            coordinator=object(),
            application=application,
            lifecycle=object(),
        )
        entry = SimpleNamespace(
            entry_id="entry-1",
            state=ConfigEntryState.LOADED,
            runtime_data=runtime,
        )
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_entries=lambda _domain: [entry])
        )
        cases = (
            (
                retired_map_hold_issue_id("entry-1", "registry-alpha"),
                {
                    "entry_id": "entry-1",
                    "robot_registry_id": "registry-alpha",
                    "held_at": "timestamp",
                },
                ErrorRecoveryRepairFlow,
            ),
            (
                robot_dispatch_fault_issue_id("entry-1", "registry-alpha"),
                {"entry_id": "entry-1", "robot_registry_id": "registry-alpha"},
                RobotDispatchFaultRepairFlow,
            ),
            (
                room_recovery_issue_id("entry-1", "study"),
                {"entry_id": "entry-1", "area_id": "study", "recovery_id": "episode"},
                ErrorRecoveryRepairFlow,
            ),
            (
                robot_error_recovery_issue_id("entry-1", "registry-alpha"),
                {
                    "entry_id": "entry-1",
                    "robot_registry_id": "registry-alpha",
                    "held_at": "timestamp",
                },
                ErrorRecoveryRepairFlow,
            ),
            (
                notification_delivery_issue_id("entry-1"),
                {"entry_id": "entry-1"},
                NotificationDeliveryRepairFlow,
            ),
            (
                room_dispatch_fault_issue_id("entry-1", "study"),
                {"entry_id": "entry-1", "area_id": "study"},
                RoomDispatchFaultRepairFlow,
            ),
            (
                cleaning_program_issue_id("entry-1", "study"),
                {"entry_id": "entry-1", "area_id": "study"},
                CleaningProgramCompatibilityRepairFlow,
            ),
            (
                two_pass_issue_id("entry-1", "study"),
                {"entry_id": "entry-1", "area_id": "study"},
                TwoPassCompatibilityRepairFlow,
            ),
        )
        for issue_id, data, expected_type in cases:
            with self.subTest(issue=issue_id):
                flow = await async_create_fix_flow(hass, issue_id, data)
                self.assertIsInstance(flow, expected_type)

        with self.assertRaisesRegex(ValueError, "no longer available"):
            await async_create_fix_flow(hass, "unknown", {"entry_id": "entry-1"})
        entry.state = ConfigEntryState.NOT_LOADED
        with self.assertRaisesRegex(ValueError, "no longer available"):
            await async_create_fix_flow(
                hass,
                robot_dispatch_fault_issue_id("entry-1", "registry-alpha"),
                None,
            )


if __name__ == "__main__":
    unittest.main()
