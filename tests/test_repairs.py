"""Behavioral tests for independent, redaction-safe Repair services."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from custom_components.adaptive_robovacs.const import DOMAIN
from custom_components.adaptive_robovacs.discovery import (
    DiscoveredRobot,
    DiscoveredRoom,
    RobotProfile,
)
from custom_components.adaptive_robovacs.models import (
    AdapterCapabilities,
    CleaningOperation,
    CleaningProgram,
)
from custom_components.adaptive_robovacs.repair_service import RepairService
from custom_components.adaptive_robovacs.repairs_manager import (
    fault_summary,
    robot_dispatch_fault_issue_id,
)
from custom_components.adaptive_robovacs.state import (
    CleaningStage,
    RobotSettings,
    RoomRecovery,
    RoomSettings,
    SchedulerFault,
)

WHEN = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)


def robot() -> DiscoveredRobot:
    return DiscoveredRobot(
        entity_id="vacuum.alpha",
        name="Alpha",
        registry_id="registry-alpha",
        platform="generic",
        device_id="device-alpha",
        dock_area_id="dock",
        floor_id="ground",
        supports_area_clean=True,
        supports_send_command=False,
        profile=RobotProfile(),
        adapter_id="generic",
        adapter_schema_version=1,
        adapter_capabilities=AdapterCapabilities(
            adapter_id="generic",
            schema_version=1,
            portable_area_clean=True,
            supported_pass_counts=frozenset({1}),
        ),
    )


class RepairServiceTests(unittest.TestCase):
    def test_room_recovery_issues_recreate_and_remove_only_owned_episodes(self):
        record = RoomRecovery(
            "episode",
            "study",
            "registry-alpha",
            "occurrence",
            0,
            CleaningOperation.MOP,
            WHEN,
            "robot_trapped",
        )
        registry = SimpleNamespace(
            issues={
                (DOMAIN, "room_recovery_entry-1_stale"): object(),
                (DOMAIN, "room_recovery_other-entry_study"): object(),
                (DOMAIN, "room_recovery_entry-1_study"): object(),
            }
        )
        with (
            patch(
                "custom_components.adaptive_robovacs.repair_service.ir.async_get",
                return_value=registry,
            ),
            patch(
                "custom_components.adaptive_robovacs.repair_service.ir.async_create_issue"
            ) as create,
            patch(
                "custom_components.adaptive_robovacs.repair_service.ir.async_delete_issue"
            ) as delete,
        ):
            self.service.sync_room_recoveries(
                {"study": record},
                [robot()],
                {"study": DiscoveredRoom("study", "Study", "ground", frozenset())},
            )
            self.assertEqual(create.call_args.kwargs["data"]["recovery_id"], "episode")
            self.assertIn(
                "trapped", create.call_args.kwargs["translation_placeholders"]["reason"]
            )
            self.assertTrue(create.call_args.kwargs["is_fixable"])
            self.assertTrue(create.call_args.kwargs["is_persistent"])
            delete.assert_called_once_with(
                self.hass, DOMAIN, "room_recovery_entry-1_stale"
            )
            self.service.sync_room_recoveries({"study": record}, [], {})
            self.assertEqual(
                create.call_args.kwargs["translation_placeholders"]["room"],
                "the affected room",
            )
            self.service.set_robot_error_recovery("registry-alpha", WHEN, robot())
            self.assertEqual(
                create.call_args.kwargs["data"]["held_at"], WHEN.isoformat()
            )
            self.service.set_robot_error_recovery("registry-alpha", WHEN, None)
            self.service.delete_robot_error_recovery("registry-alpha")
            self.assertEqual(
                delete.call_args.args[-1], "robot_error_recovery_entry-1_registry-alpha"
            )

    def setUp(self) -> None:
        self.hass = object()
        self.service = RepairService(self.hass, "entry-1")

    def test_storage_repair_is_persistent_and_clears_by_stable_id(self) -> None:
        with (
            patch(
                "custom_components.adaptive_robovacs.repair_service."
                "ir.async_create_issue"
            ) as create,
            patch(
                "custom_components.adaptive_robovacs.repair_service."
                "ir.async_delete_issue"
            ) as delete,
        ):
            self.service.set_storage_unsafe(True, "unsupported schema")
            self.service.set_storage_unsafe(False)

        self.assertEqual(create.call_args.args[1:3], (DOMAIN, "storage_unsafe_entry-1"))
        self.assertTrue(create.call_args.kwargs["is_persistent"])
        self.assertEqual(
            create.call_args.kwargs["translation_placeholders"],
            {"reason": "unsupported schema"},
        )
        delete.assert_called_once_with(
            self.hass,
            DOMAIN,
            "storage_unsafe_entry-1",
        )

    def test_unresolved_identity_sync_deletes_stale_and_creates_current(self) -> None:
        prefix = "unresolved_robot_reference_entry-1_"
        registry = SimpleNamespace(
            issues={
                (DOMAIN, f"{prefix}old"): object(),
                ("different", f"{prefix}ignored"): object(),
            }
        )
        with (
            patch(
                "custom_components.adaptive_robovacs.repair_service.ir.async_get",
                return_value=registry,
            ),
            patch(
                "custom_components.adaptive_robovacs.repair_service."
                "ir.async_create_issue"
            ) as create,
            patch(
                "custom_components.adaptive_robovacs.repair_service."
                "ir.async_delete_issue"
            ) as delete,
        ):
            self.service.sync_unresolved_robot_references(("vacuum.missing",))

        delete.assert_called_once_with(self.hass, DOMAIN, f"{prefix}old")
        self.assertEqual(
            create.call_args.args[2],
            f"{prefix}vacuum.missing",
        )
        self.assertEqual(
            create.call_args.kwargs["data"]["legacy_key"],
            "vacuum.missing",
        )

    def test_dispatch_fault_uses_safe_summary_not_raw_vendor_error(self) -> None:
        fault = SchedulerFault(
            reason_code="start_outcome_uncertain",
            robot_registry_id="registry-alpha",
            room_area_id="study",
            occurred_at=WHEN,
            phase="dispatch",
            native_command_may_have_started=True,
            outcome_uncertain=True,
        )
        with (
            patch(
                "custom_components.adaptive_robovacs.repair_service."
                "ir.async_create_issue"
            ) as create,
            patch(
                "custom_components.adaptive_robovacs.repair_service."
                "ir.async_delete_issue"
            ),
        ):
            self.service.sync_dispatch_faults(
                {"registry-alpha": fault},
                {},
                (robot(),),
                {"study": DiscoveredRoom("study", "Study", "ground", frozenset())},
            )

        call = create.call_args
        self.assertEqual(
            call.args[2],
            robot_dispatch_fault_issue_id("entry-1", "registry-alpha"),
        )
        placeholders = call.kwargs["translation_placeholders"]
        self.assertEqual(placeholders["robot"], "Alpha")
        self.assertEqual(placeholders["room"], "Study")
        self.assertEqual(
            placeholders["reason"],
            fault_summary("start_outcome_uncertain"),
        )
        self.assertNotIn("vendor", str(placeholders).lower())

    def test_scoped_delete_helpers_preserve_other_repairs(self) -> None:
        with patch(
            "custom_components.adaptive_robovacs.repair_service.ir.async_delete_issue"
        ) as delete:
            self.service.delete_robot_dispatch_fault("registry-alpha")
            self.service.delete_room_dispatch_fault("study")

        self.assertEqual(delete.call_count, 2)
        self.assertEqual(
            delete.call_args_list[0].args[2],
            "robot_dispatch_fault_entry-1_registry-alpha",
        )
        self.assertEqual(
            delete.call_args_list[1].args[2],
            "room_dispatch_fault_entry-1_study",
        )

    def test_notification_delivery_repair_is_fixable_and_clearable(self) -> None:
        with (
            patch(
                "custom_components.adaptive_robovacs.repair_service."
                "ir.async_create_issue"
            ) as create,
            patch(
                "custom_components.adaptive_robovacs.repair_service."
                "ir.async_delete_issue"
            ) as delete,
        ):
            self.service.set_notification_delivery_issue(True)
            self.service.set_notification_delivery_issue(False)
        self.assertTrue(create.call_args.kwargs["is_fixable"])
        self.assertEqual(create.call_args.kwargs["translation_placeholders"], {})
        delete.assert_called_once()

    def test_robot_and_room_faults_fall_back_to_safe_generic_names(self) -> None:
        fault = SchedulerFault(
            "unknown-private-code", "registry-missing", "missing", WHEN, "dispatch"
        )
        with (
            patch(
                "custom_components.adaptive_robovacs.repair_service."
                "ir.async_create_issue"
            ) as create,
            patch(
                "custom_components.adaptive_robovacs.repair_service."
                "ir.async_delete_issue"
            ),
        ):
            self.service.sync_dispatch_faults(
                {"registry-missing": fault},
                {"missing": fault},
                (),
                {},
            )
        self.assertEqual(create.call_count, 2)
        self.assertEqual(
            create.call_args_list[0].kwargs["translation_placeholders"]["robot"],
            "the selected vacuum",
        )
        self.assertEqual(
            create.call_args_list[1].kwargs["translation_placeholders"]["room"],
            "the selected room",
        )

    def test_two_pass_repairs_follow_current_same_floor_capability(self) -> None:
        room_settings = RoomSettings.defaults(False)
        room_settings.vacuum_pass_count = 2
        discovered_room = DiscoveredRoom("study", "Study", "ground", frozenset())
        with (
            patch(
                "custom_components.adaptive_robovacs.repair_service."
                "ir.async_create_issue"
            ) as create,
            patch(
                "custom_components.adaptive_robovacs.repair_service."
                "ir.async_delete_issue"
            ) as delete,
        ):
            self.service.sync_two_pass_issues(
                {"study": room_settings}, {"study": discovered_room}, (robot(),)
            )
            create.assert_called_once()
            room_settings.vacuum_pass_count = 1
            self.service.sync_two_pass_issues(
                {"study": room_settings}, {"study": discovered_room}, (robot(),)
            )
            self.service.sync_two_pass_issues(
                {"missing": room_settings}, {}, (robot(),)
            )
            self.assertEqual(delete.call_count, 2)

            compatible = replace(
                robot(),
                adapter_capabilities=replace(
                    robot().adapter_capabilities,
                    supported_pass_counts=frozenset({1, 2}),
                ),
            )
            room_settings.vacuum_pass_count = 2
            self.service.sync_two_pass_issues(
                {"study": room_settings},
                {"study": discovered_room},
                (compatible,),
            )
            self.assertEqual(delete.call_count, 3)

    def test_cleaning_program_repairs_use_typed_profiles_and_occurrences(self) -> None:
        discovered_room = DiscoveredRoom("study", "Study", "ground", frozenset())
        room_settings = RoomSettings.defaults(False)
        robot_settings = RobotSettings.defaults(False)
        room_settings.cleaning_program = CleaningProgram.MOP_ONLY
        with (
            patch(
                "custom_components.adaptive_robovacs.repair_service."
                "ir.async_create_issue"
            ) as create,
            patch(
                "custom_components.adaptive_robovacs.repair_service."
                "ir.async_delete_issue"
            ) as delete,
        ):
            self.service.sync_cleaning_program_issues(
                {"study": room_settings},
                {"registry-alpha": robot_settings},
                {},
                {"study": discovered_room},
                (robot(),),
            )
            create.assert_called_once()

            room_settings.cleaning_program = CleaningProgram.VACUUM_ONLY
            self.service.sync_cleaning_program_issues(
                {"study": room_settings},
                {"registry-alpha": robot_settings},
                {},
                {"study": discovered_room},
                (robot(),),
            )
            delete.assert_called_once()

            occurrence = SimpleNamespace(
                robot_registry_id="registry-alpha",
                current_stage=0,
                stages=[CleaningStage(CleaningOperation.VACUUM, 1)],
            )
            self.service.sync_cleaning_program_issues(
                {"study": room_settings},
                {"registry-alpha": robot_settings},
                {"study": occurrence},
                {"study": discovered_room},
                (robot(),),
            )
            self.assertEqual(delete.call_count, 2)

            occurrence.current_stage = 1
            self.service.sync_cleaning_program_issues(
                {"study": room_settings},
                {"registry-alpha": robot_settings},
                {"study": occurrence},
                {"study": discovered_room},
                (robot(),),
            )
            self.assertEqual(create.call_count, 2)

            room_settings.enabled = False
            self.service.sync_cleaning_program_issues(
                {"study": room_settings}, {}, {}, {"study": discovered_room}, ()
            )
            self.assertEqual(delete.call_count, 3)


if __name__ == "__main__":
    unittest.main()
