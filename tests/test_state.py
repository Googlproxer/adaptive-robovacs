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
RoomHistory = state_module.RoomHistory
SchedulerState = state_module.SchedulerState
SchedulerFault = state_module.SchedulerFault
StateSchemaError = state_module.StateSchemaError


ENTRY_DATA = {
    "observe_only": False,
    "forecast_confidence": 75,
    "hall_start": "08:00",
    "hall_end": "19:00",
    "unresolved_start": "00:00",
    "unresolved_end": "04:00",
}


class SchedulerStateTests(unittest.TestCase):
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
        self.assertEqual(state.robot_settings["vacuum.alpha"].minimum_battery, 85)
        self.assertEqual(state.room_history["kitchen"].duration_samples[0].minutes, 26.5)
        self.assertEqual(state.active_jobs["vacuum.alpha"].phase, "paused")
        self.assertEqual(state.robot_holds["vacuum.alpha"].reason, "paused")
        self.assertEqual(state.audit.manual_events, [{"outcome": "requested"}])

        stored = state.to_store()
        self.assertEqual(stored["schema_version"], SCHEMA_VERSION)
        self.assertNotIn("active", stored)
        self.assertEqual(stored["active_jobs"]["vacuum.alpha"]["room"], "kitchen")

    def test_v4_round_trip_preserves_window_pass_fault_and_job_adapter(self) -> None:
        state = SchedulerState.create(ENTRY_DATA)
        study_settings, _ = state.ensure_room("study", is_bedroom=False)
        study_settings.desired_window_start = "10:15"
        study_settings.pass_count = 2
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
        )
        state.scheduler_fault = SchedulerFault(
            reason_code="start_outcome_uncertain",
            robot_registry_id="registry-robot",
            room_area_id="study",
            occurred_at=datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
            phase="dispatch",
            native_command_may_have_started=True,
            outcome_uncertain=True,
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
        self.assertEqual(restored.scheduler_fault.robot_registry_id, "registry-robot")

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
        self.assertEqual(
            restored.to_runtime_data()["settings"]["rooms"]["study"],
            {
                "enabled": True,
                "vacuum_interval": 84,
                "mop_interval": 168,
                "expected_minutes": 30,
                "carpet": False,
                "ignore_desired_window": False,
                "desired_window_start": None,
                "desired_window_end": None,
                "pass_count": None,
            },
        )

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


if __name__ == "__main__":
    unittest.main()
