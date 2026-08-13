"""Tests for the durable scheduler-state codec without Home Assistant."""

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
import types
import unittest


PACKAGE_PATH = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs"
PACKAGE_NAME = "adaptive_robovacs_state_test"
package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(PACKAGE_PATH)]
sys.modules[PACKAGE_NAME] = package
SPEC = importlib.util.spec_from_file_location(f"{PACKAGE_NAME}.state", PACKAGE_PATH / "state.py")
assert SPEC and SPEC.loader
state_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = state_module
SPEC.loader.exec_module(state_module)

SCHEMA_VERSION = state_module.SCHEMA_VERSION
ActiveJob = state_module.ActiveJob
CleaningOccurrence = state_module.CleaningOccurrence
CleaningStage = state_module.CleaningStage
RoomHistory = state_module.RoomHistory
SchedulerState = state_module.SchedulerState
SchedulerFault = state_module.SchedulerFault
StateSchemaError = state_module.StateSchemaError
migrate_runtime_robot_identity = state_module.migrate_runtime_robot_identity


ENTRY_DATA = {
    "observe_only": False,
    "forecast_confidence": 75,
    "hall_start": "08:00",
    "hall_end": "19:00",
    "unresolved_start": "00:00",
    "unresolved_end": "04:00",
}


class SchedulerStateTests(unittest.TestCase):
    def test_robot_identity_migration_preserves_settings_jobs_and_unique_alias(self) -> None:
        data = {
            "settings": {
                "robots": {
                    "vacuum.alpha": {
                        "enabled": False,
                        "cleaning_program": "vacuum_only",
                    }
                }
            },
            "rooms": {
                "study": {
                    "duration_samples": [
                        {"robot": "vacuum.alpha", "minutes": 20}
                    ]
                }
            },
            "active": {"vacuum.alpha": {"room": "study"}},
            "robot_holds": {"vacuum.alpha": {"reason": "paused"}},
            "occurrences": {
                "study": {
                    "robot_registry_id": "registry-alpha",
                    "robot_entity_id": "vacuum.alpha",
                }
            },
        }

        changed = migrate_runtime_robot_identity(
            data,
            {"registry-alpha": "vacuum.renamed"},
            {"registry-alpha": "vacuum.alpha"},
        )

        self.assertTrue(changed)
        self.assertFalse(data["settings"]["robots"]["registry-alpha"]["enabled"])
        self.assertNotIn("vacuum.alpha", data["settings"]["robots"])
        self.assertEqual(
            data["robot_entity_aliases"]["registry-alpha"], "vacuum.alpha"
        )
        self.assertIn("vacuum.renamed", data["active"])
        self.assertIn("vacuum.renamed", data["robot_holds"])
        self.assertEqual(
            data["rooms"]["study"]["duration_samples"][0]["robot"],
            "registry-alpha",
        )
        self.assertEqual(
            data["occurrences"]["study"]["robot_entity_id"],
            "vacuum.renamed",
        )
        self.assertFalse(
            migrate_runtime_robot_identity(
                data, {"registry-alpha": "vacuum.renamed"}
            )
        )

    def test_robot_identity_migration_does_not_guess_between_ambiguous_robots(self) -> None:
        data = {
            "settings": {
                "robots": {
                    "vacuum.old_one": {"enabled": False},
                    "vacuum.old_two": {"enabled": True},
                }
            },
            "rooms": {},
            "active": {},
            "robot_holds": {},
            "occurrences": {},
        }

        migrate_runtime_robot_identity(
            data,
            {
                "registry-one": "vacuum.new_one",
                "registry-two": "vacuum.new_two",
            },
        )

        self.assertIn("vacuum.old_one", data["settings"]["robots"])
        self.assertIn("vacuum.old_two", data["settings"]["robots"])
        self.assertNotIn("registry-one", data["settings"]["robots"])
        self.assertNotIn("registry-two", data["settings"]["robots"])

    def test_v1_payload_migrates_without_losing_active_hold_or_audit_data(self) -> None:
        payload = {
            "version": 1,
            "observe_only": False,
            "party_mode": True,
            "settings": {
                "rooms": {
                    "kitchen": {
                        "enabled": True,
                        "vacuum_interval": 72,
                        "mop_interval": 120,
                        "expected_minutes": 28,
                        "carpet": False,
                    }
                },
                "robots": {"vacuum.alpha": {"minimum_battery": 85, "double_pass": True}},
            },
            "rooms": {
                "kitchen": {
                    "vacuum": "2026-08-01T09:00:00+00:00",
                    "defer": {"vacuum": "2026-08-05T09:00:00+00:00"},
                    "samples": [{"start": "2026-08-03T09:00:00+00:00", "minutes": 30}],
                    "duration_samples": [
                        {
                            "minutes": 26.5,
                            "operation": "vacuum",
                            "passes": 1,
                            "robot": "vacuum.alpha",
                            "source": "state_transition",
                            "at": "2026-08-01T09:26:30+00:00",
                        }
                    ],
                }
            },
            "active": {
                "vacuum.alpha": {
                    "room": "kitchen",
                    "operation": "vacuum",
                    "phase": "paused",
                    "source": "scheduler",
                    "expected_minutes": 28,
                    "expected_end": "2026-08-05T09:28:00+00:00",
                }
            },
            "robot_holds": {
                "vacuum.alpha": {
                    "reason": "paused",
                    "phase": "held",
                    "held_at": "2026-08-05T09:02:00+00:00",
                }
            },
            "manual_events": [{"outcome": "requested"}],
            "recovery_events": [{"reason": "paused"}],
            "last_evaluation": "2026-08-05T09:00:00+00:00",
            "last_preview": {"reason": "interval"},
        }

        state, migrated = SchedulerState.from_store(payload, ENTRY_DATA)

        self.assertTrue(migrated)
        self.assertTrue(state.global_settings.party_mode)
        self.assertEqual(state.room_settings["kitchen"].vacuum_interval, 72)
        self.assertEqual(
            state.room_history["kitchen"].cleaning_completed_at,
            datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(state.robot_settings["vacuum.alpha"].minimum_battery, 85)
        self.assertEqual(state.room_history["kitchen"].duration_samples[0].minutes, 26.5)
        self.assertEqual(state.active_jobs["vacuum.alpha"].phase, "paused")
        self.assertEqual(state.robot_holds["vacuum.alpha"].reason, "paused")
        self.assertEqual(state.audit.manual_events, [{"outcome": "requested"}])

        stored = state.to_store()
        self.assertEqual(stored["schema_version"], SCHEMA_VERSION)
        self.assertNotIn("active", stored)
        self.assertEqual(stored["active_jobs"]["vacuum.alpha"]["room"], "kitchen")

    def test_v6_round_trip_preserves_occurrence_window_pass_fault_and_job_adapter(self) -> None:
        state = SchedulerState.create(ENTRY_DATA)
        study_settings, _ = state.ensure_room("study", is_bedroom=False)
        study_settings.desired_window_start = "10:15"
        study_settings.pass_count = 2
        study_settings.fan_speed = "max"
        study_settings.mop_mode = "deep"
        state.ensure_room("kitchen", is_bedroom=False)
        state.room_history["study"] = RoomHistory(
            vacuum_completed_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            deferrals={"vacuum": datetime(2026, 8, 2, tzinfo=timezone.utc)},
        )
        state.active_jobs["vacuum.beta"] = ActiveJob(
            room_id="study",
            room_ids=["study"],
            operation="vacuum",
            phase="cleaning",
            source="scheduler",
            expected_minutes=25,
            expected_end=datetime(2026, 8, 3, 9, 25, tzinfo=timezone.utc),
            adapter_id="roborock",
            adapter_schema_version=1,
            cleaning_profile={"operation": "vacuum", "fan_speed": "max"},
            requested_profile={"fan_speed": "max", "mode": "vacuum"},
            profile_sources={"fan_speed": "room", "mode": "robot"},
            manual_mode="configured",
        )
        state.robot_entity_aliases["registry-robot"] = "vacuum.beta"
        state.scheduler_fault = SchedulerFault(
            reason_code="start_outcome_uncertain",
            robot_registry_id="registry-robot",
            room_area_id="study",
            occurred_at=datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
            phase="dispatch",
            native_command_may_have_started=True,
            outcome_uncertain=True,
        )
        state.occurrences["study"] = CleaningOccurrence(
            occurrence_id="occurrence-1",
            room_id="study",
            robot_registry_id="registry-robot",
            robot_entity_id="vacuum.beta",
            program="vacuum_then_mop",
            stages=[
                CleaningStage(
                    "vacuum",
                    2,
                    "completed",
                    cleaning_profile={
                        "operation": "vacuum",
                        "fan_speed": "max",
                        "mode": "vacuum",
                    },
                    requested_profile={"fan_speed": "max", "mode": "vacuum"},
                    profile_sources={"fan_speed": "room", "mode": "robot"},
                ),
                CleaningStage("mop", 1),
            ],
            scheduled_at=datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
            created_at=datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
            adapter_id="roborock",
            adapter_schema_version=2,
            current_stage=1,
            source="manual_dashboard",
            manual_mode="configured",
            bypass_desired_window=True,
            manual_context_id="context-one",
        )

        restored, migrated = SchedulerState.from_store(state.to_store(), ENTRY_DATA)

        self.assertFalse(migrated)
        self.assertFalse(restored.global_settings.observe_only)
        self.assertEqual(restored.room_settings["study"].desired_window_start, "10:15")
        self.assertIsNone(restored.room_settings["study"].desired_window_end)
        self.assertIsNone(restored.room_settings["kitchen"].desired_window_start)
        self.assertEqual(
            restored.room_history["study"].deferrals["vacuum"],
            datetime(2026, 8, 2, tzinfo=timezone.utc),
        )
        self.assertEqual(restored.active_jobs["vacuum.beta"].expected_minutes, 25)
        self.assertEqual(restored.active_jobs["vacuum.beta"].adapter_id, "roborock")
        self.assertEqual(restored.room_settings["study"].pass_count, 2)
        self.assertEqual(restored.room_settings["study"].fan_speed, "max")
        self.assertEqual(restored.room_settings["study"].mop_mode, "deep")
        self.assertEqual(restored.scheduler_fault.robot_registry_id, "registry-robot")
        self.assertEqual(restored.occurrences["study"].current_stage, 1)
        self.assertEqual(restored.occurrences["study"].stages[0].status, "completed")
        self.assertEqual(
            restored.occurrences["study"].stages[0].cleaning_profile["fan_speed"],
            "max",
        )
        self.assertEqual(restored.occurrences["study"].source, "manual_dashboard")
        self.assertTrue(restored.occurrences["study"].bypass_desired_window)
        self.assertEqual(
            restored.active_jobs["vacuum.beta"].cleaning_profile["fan_speed"],
            "max",
        )
        self.assertEqual(
            restored.active_jobs["vacuum.beta"].profile_sources["fan_speed"],
            "room",
        )
        self.assertEqual(
            restored.robot_entity_aliases["registry-robot"], "vacuum.beta"
        )

        runtime_restored, runtime_migrated = SchedulerState.from_store(
            restored.to_runtime_data(), ENTRY_DATA
        )
        self.assertTrue(runtime_migrated)
        self.assertEqual(
            runtime_restored.scheduler_fault.reason_code,
            "start_outcome_uncertain",
        )

        stored_room = restored.to_store()["room_settings"]["study"]
        self.assertEqual(
            stored_room["daily_window"],
            {"version": 1, "start": "10:15", "end": None},
        )

    def test_v6_payload_migrates_to_nullable_room_profiles(self) -> None:
        state = SchedulerState.create(ENTRY_DATA)
        state.ensure_room("study", is_bedroom=False)
        payload = state.to_store()
        payload["schema_version"] = 6
        for key in ("fan_speed", "mode", "mop_mode", "mop_intensity"):
            payload["room_settings"]["study"].pop(key)

        restored, migrated = SchedulerState.from_store(payload, ENTRY_DATA)

        self.assertTrue(migrated)
        self.assertIsNone(restored.room_settings["study"].fan_speed)
        self.assertIsNone(restored.room_settings["study"].mode)

    def test_current_schema_rejects_malformed_profile_values(self) -> None:
        state = SchedulerState.create(ENTRY_DATA)
        state.ensure_room("study", is_bedroom=False)
        payload = state.to_store()
        payload["room_settings"]["study"]["fan_speed"] = ["max"]

        with self.assertRaises(StateSchemaError):
            SchedulerState.from_store(payload, ENTRY_DATA)

    def test_v2_rooms_migrate_to_inherited_daily_windows(self) -> None:
        state = SchedulerState.create(ENTRY_DATA)
        state.ensure_room("study", is_bedroom=False)
        payload = state.to_store()
        payload["schema_version"] = 2
        payload["room_settings"]["study"].pop("daily_window")

        restored, migrated = SchedulerState.from_store(payload, ENTRY_DATA)

        self.assertTrue(migrated)
        self.assertIsNone(restored.room_settings["study"].desired_window_start)
        self.assertIsNone(restored.room_settings["study"].desired_window_end)
        runtime = restored.to_runtime_data()["settings"]["rooms"]["study"]
        self.assertEqual(runtime["cleaning_interval"], 84)
        self.assertEqual(runtime["vacuum_interval"], 84)
        self.assertEqual(runtime["mop_interval"], 84)
        self.assertIsNone(runtime["cleaning_program"])
        self.assertIsNone(runtime["vacuum_pass_count"])
        self.assertIsNone(runtime["mop_pass_count"])

    def test_v3_payload_migrates_room_passes_to_robot_default(self) -> None:
        state = SchedulerState.create(ENTRY_DATA)
        state.ensure_room("study", is_bedroom=False)
        payload = state.to_store()
        payload["schema_version"] = 3
        payload["room_settings"]["study"].pop("pass_count")
        payload.pop("scheduler_fault")

        restored, migrated = SchedulerState.from_store(payload, ENTRY_DATA)

        self.assertTrue(migrated)
        self.assertIsNone(restored.room_settings["study"].pass_count)

    def test_v4_dual_cadence_and_mopping_enable_migrate_to_one_program(self) -> None:
        state = SchedulerState.create(ENTRY_DATA)
        state.ensure_room("study", is_bedroom=False)
        state.ensure_robot("vacuum.alpha", supports_mopping=False)
        payload = state.to_store()
        payload["schema_version"] = 4
        payload["room_settings"]["study"].pop("cleaning_interval")
        payload["room_settings"]["study"]["vacuum_interval"] = 72
        payload["room_settings"]["study"]["mop_interval"] = 144
        payload["robot_settings"]["vacuum.alpha"].pop("cleaning_program")
        payload["robot_settings"]["vacuum.alpha"]["mopping_enabled"] = True

        restored, migrated = SchedulerState.from_store(payload, ENTRY_DATA)

        self.assertTrue(migrated)
        self.assertEqual(restored.room_settings["study"].cleaning_interval, 72)
        self.assertEqual(
            restored.robot_settings["vacuum.alpha"].cleaning_program,
            "vacuum_then_mop",
        )

    def test_invalid_samples_are_dropped_without_invalidating_a_v1_migration(self) -> None:
        state, migrated = SchedulerState.from_store(
            {
                "rooms": {
                    "study": {
                        "samples": [{"start": "not-a-date", "minutes": 20}],
                        "duration_samples": [{"minutes": "bad"}],
                    }
                }
            },
            ENTRY_DATA,
        )

        self.assertTrue(migrated)
        self.assertEqual(state.room_history["study"].occupancy_samples, [])
        self.assertEqual(state.room_history["study"].duration_samples, [])

    def test_newer_schema_is_rejected_before_state_is_mutated(self) -> None:
        with self.assertRaises(StateSchemaError):
            SchedulerState.from_store({"schema_version": SCHEMA_VERSION + 1}, ENTRY_DATA)

    def test_current_schema_requires_all_structural_sections(self) -> None:
        with self.assertRaises(StateSchemaError):
            SchedulerState.from_store({"schema_version": SCHEMA_VERSION, "global": {}}, ENTRY_DATA)

        payload = SchedulerState.create(ENTRY_DATA).to_store()
        payload.pop("robot_entity_aliases")
        with self.assertRaises(StateSchemaError):
            SchedulerState.from_store(payload, ENTRY_DATA)

    def test_unknown_daily_window_version_is_rejected(self) -> None:
        state = SchedulerState.create(ENTRY_DATA)
        state.ensure_room("study", is_bedroom=False)
        payload = state.to_store()
        payload["room_settings"]["study"]["daily_window"]["version"] = 2

        with self.assertRaises(StateSchemaError):
            SchedulerState.from_store(payload, ENTRY_DATA)

    def test_invalid_persisted_daily_time_is_rejected(self) -> None:
        state = SchedulerState.create(ENTRY_DATA)
        state.ensure_room("study", is_bedroom=False)
        payload = state.to_store()
        payload["room_settings"]["study"]["daily_window"]["start"] = "9:00"

        with self.assertRaises(StateSchemaError):
            SchedulerState.from_store(payload, ENTRY_DATA)

    def test_invalid_global_daily_time_is_rejected(self) -> None:
        payload = SchedulerState.create(ENTRY_DATA).to_store()
        payload["global"]["unresolved_start"] = "9:00"

        with self.assertRaises(StateSchemaError):
            SchedulerState.from_store(payload, ENTRY_DATA)

    def test_out_of_range_persisted_numbers_are_rejected(self) -> None:
        state = SchedulerState.create(ENTRY_DATA)
        state.ensure_room("study", is_bedroom=False)
        state.ensure_robot("registry-alpha", supports_mopping=False)
        payload = state.to_store()

        invalid_values = (
            ("global", "forecast_confidence", 101),
            ("room_settings", "cleaning_interval", -1),
            ("room_settings", "expected_minutes", 181),
            ("robot_settings", "minimum_battery", 10),
        )
        for section, field, value in invalid_values:
            with self.subTest(section=section, field=field):
                candidate = SchedulerState.from_store(payload, ENTRY_DATA)[0].to_store()
                if section == "global":
                    candidate[section][field] = value
                elif section == "room_settings":
                    candidate[section]["study"][field] = value
                else:
                    candidate[section]["registry-alpha"][field] = value
                with self.assertRaises(StateSchemaError):
                    SchedulerState.from_store(candidate, ENTRY_DATA)


if __name__ == "__main__":
    unittest.main()
