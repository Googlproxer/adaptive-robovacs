"""Behavioral checks for user-confirmed recovery resets."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from custom_components.adaptive_robovacs.models import (
    CleaningOperation,
    CleaningProgram,
    FaultCode,
)
from custom_components.adaptive_robovacs.state import (
    CleaningOccurrence,
    CleaningStage,
    SchedulerFault,
    SchedulerState,
)
from tests.test_application_state import ENTRY_DATA, NOW, state_application


class RecoveryResetTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def ready_dispatch(app) -> None:
        app.dispatch = SimpleNamespace(
            async_preflight=AsyncMock(return_value=SimpleNamespace(ready=True)),
            async_validate_profile=AsyncMock(return_value=SimpleNamespace(ready=True)),
        )
        app._candidate_for_robot = Mock(side_effect=lambda candidate, _robot: candidate)

    async def test_mapping_fault_cannot_be_cleared_by_settings_reset(self) -> None:
        app = state_application()
        app.state.room_faults["study"] = SchedulerFault(
            FaultCode.AREA_MAPPING_STALE,
            "registry-alpha",
            "study",
            NOW,
            "preflight",
        )
        result = await app.async_reset_incompatible_settings("study", "room")
        self.assertEqual(result["reason"], "mapping_requires_review")
        app.storage.async_save.assert_not_awaited()

    async def test_room_profile_reset_validates_before_persisting(self) -> None:
        app = state_application()
        app.state.room_settings["study"].fan_speed = "unsupported"
        app.state.room_settings["study"].profile_custom = True
        app.state.room_faults["study"] = SchedulerFault(
            FaultCode.PROFILE_OPTION_UNSUPPORTED,
            "registry-alpha",
            "study",
            NOW,
            "profile_preflight",
        )
        app.dispatch = SimpleNamespace(
            async_preflight=AsyncMock(return_value=SimpleNamespace(ready=True)),
            async_validate_profile=AsyncMock(return_value=SimpleNamespace(ready=True)),
        )
        app._candidate_for_robot = Mock(side_effect=lambda candidate, _robot: candidate)
        result = await app.async_reset_incompatible_settings("study", "room")
        self.assertTrue(result["cleared"], result)
        self.assertIsNone(app.state.room_settings["study"].fan_speed)
        self.assertFalse(app.state.room_settings["study"].profile_custom)
        self.assertNotIn("study", app.state.room_faults)
        app.storage.async_save.assert_awaited()

    async def test_room_reset_discards_pending_only_when_selected(self) -> None:
        app = state_application()
        app.state.room_settings["study"].cleaning_program = CleaningProgram.VACUUM_ONLY
        app.state.room_settings["study"].vacuum_pass_count = 2
        app.state.room_settings["study"].mop_pass_count = 2
        app.state.occurrences["study"] = CleaningOccurrence(
            "pending-1",
            "study",
            "registry-alpha",
            "vacuum.alpha",
            CleaningProgram.VACUUM_ONLY,
            [CleaningStage(CleaningOperation.VACUUM, 2)],
            NOW,
            NOW,
            "fake",
            2,
        )
        self.ready_dispatch(app)
        rejected = await app.async_reset_incompatible_settings("study", "room")
        self.assertEqual(rejected["reason"], "pending_attempt_requires_discard")
        self.assertIn("study", app.state.occurrences)
        result = await app.async_reset_incompatible_settings(
            "study", "room_and_pending"
        )
        self.assertTrue(result["cleared"], result)
        self.assertNotIn("study", app.state.occurrences)
        settings = app.state.room_settings["study"]
        self.assertIsNone(settings.cleaning_program)
        self.assertIsNone(settings.vacuum_pass_count)
        self.assertIsNone(settings.mop_pass_count)
        self.assertEqual(
            app.state.audit.recovery_events[-1].reason,
            "settings_reset_pending_discarded",
        )

    async def test_robot_reset_preserves_enablement_and_battery_gate(self) -> None:
        app = state_application()
        settings = app.state.robot_settings["registry-alpha"]
        settings.enabled = False
        settings.minimum_battery = 67
        settings.fan_speed = "unsupported"
        settings.cleaning_program = CleaningProgram.VACUUM_ONLY
        app.state.room_faults["study"] = SchedulerFault(
            FaultCode.PROFILE_OPTION_UNSUPPORTED,
            "registry-alpha",
            "study",
            NOW,
            "profile_preflight",
        )
        self.ready_dispatch(app)
        result = await app.async_reset_incompatible_settings("study", "robot")
        self.assertTrue(result["cleared"], result)
        restored = app.state.robot_settings["registry-alpha"]
        self.assertFalse(restored.enabled)
        self.assertEqual(restored.minimum_battery, 67)
        self.assertIsNone(restored.fan_speed)
        self.assertEqual(restored.cleaning_program, CleaningProgram.VACUUM_THEN_MOP)
        self.assertNotIn("study", app.state.room_faults)

    async def test_incompatible_defaults_preserve_original_settings(self) -> None:
        app = state_application()
        original = app.state.room_settings["study"]
        original.fan_speed = "unsupported"
        app.dispatch = SimpleNamespace(
            async_preflight=AsyncMock(return_value=SimpleNamespace(ready=False)),
            async_validate_profile=AsyncMock(return_value=SimpleNamespace(ready=True)),
        )
        app._candidate_for_robot = Mock(side_effect=lambda candidate, _robot: candidate)
        result = await app.async_reset_incompatible_settings("study", "room")
        self.assertEqual(result["reason"], "defaults_incompatible")
        self.assertIs(app.state.room_settings["study"], original)
        app.storage.async_save.assert_not_awaited()

    async def test_unsafe_store_reset_keeps_scheduler_in_observe_only(self) -> None:
        app = state_application()
        app._storage_safe_mode = True
        restored = SchedulerState.create(ENTRY_DATA)
        restored.global_settings.observe_only = True
        app.storage = SimpleNamespace(
            async_reset_unsafe_state=AsyncMock(return_value=(restored, "backup-key"))
        )
        result = await app.async_reset_unsafe_store()
        self.assertTrue(result["cleared"], result)
        self.assertTrue(app.observe_only)
        self.assertFalse(app._storage_safe_mode)
        app.repairs.set_storage_unsafe.assert_called_once_with(False)

    async def test_unsafe_store_backup_failure_keeps_safe_mode(self) -> None:
        app = state_application()
        app._storage_safe_mode = True
        app.storage = SimpleNamespace(
            async_reset_unsafe_state=AsyncMock(side_effect=ValueError("private"))
        )
        with self.assertLogs(
            "custom_components.adaptive_robovacs.application.settings", level="ERROR"
        ):
            result = await app.async_reset_unsafe_store()
        self.assertEqual(result["reason"], "backup_or_reset_failed")
        self.assertTrue(app._storage_safe_mode)
        app.repairs.set_storage_unsafe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
