"""Behavioral tests for the typed scheduler Store codec and migrations."""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

PACKAGE_PATH = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs"
PACKAGE_NAME = "adaptive_robovacs_state_test"
package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(PACKAGE_PATH)]
sys.modules[PACKAGE_NAME] = package
SPEC = importlib.util.spec_from_file_location(
    f"{PACKAGE_NAME}.state", PACKAGE_PATH / "state.py"
)
assert SPEC and SPEC.loader
state = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = state
SPEC.loader.exec_module(state)
models = importlib.import_module(f"{PACKAGE_NAME}.models")


ENTRY_DATA = {
    "observe_only": False,
    "forecast_confidence": 75,
    "unresolved_start": "00:00",
    "unresolved_end": "04:00",
}
WHEN = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)


def populated_state():
    """Return representative typed state spanning every durable section."""

    result = state.SchedulerState.create(ENTRY_DATA)
    room_settings, history = result.ensure_room("study", is_bedroom=False)
    room_settings.cleaning_program = models.CleaningProgram.VACUUM_THEN_MOP
    room_settings.vacuum_pass_count = 2
    room_settings.fan_speed = "max"
    history.cleaning_completed_at = WHEN - timedelta(days=2)
    history.deferrals["cleaning"] = state.Deferral(
        until=WHEN + timedelta(hours=1),
        source="manual_clean",
        created_at=WHEN,
        room_area_id="study",
    )
    history.duration_samples.append(
        state.DurationSample(
            minutes=21.5,
            operation=models.CleaningOperation.VACUUM,
            passes=2,
            robot_registry_id="registry-alpha",
            source="state_transition",
            recorded_at=WHEN,
        )
    )
    robot_settings = result.ensure_robot("registry-alpha", supports_mopping=True)
    robot_settings.minimum_battery = 80
    result.robot_entity_aliases["registry-alpha"] = "vacuum.alpha"
    result.active_jobs["registry-alpha"] = state.ActiveJob(
        room_id="study",
        room_ids=["study"],
        operation=models.CleaningOperation.VACUUM,
        phase=models.JobPhase.CLEANING,
        source=models.JobSource.SCHEDULER,
        started_at=WHEN,
        expected_minutes=21.5,
        expected_end=WHEN + timedelta(minutes=22),
        cleaning_profile=models.ResolvedCleaningProfile(
            operation=models.CleaningOperation.VACUUM,
            fan_speed="max",
        ),
        requested_profile=models.RequestedCleaningProfile(fan_speed="max"),
        profile_sources=(("fan_speed", "room"),),
    )
    result.robot_holds["registry-alpha"] = state.RobotHold(
        reason="paused", phase="held", held_at=WHEN
    )
    result.robot_cooldowns["registry-alpha"] = state.RobotCooldown(
        until=WHEN + timedelta(minutes=15), cancelled_at=WHEN
    )
    result.occurrences["study"] = state.CleaningOccurrence(
        occurrence_id="occurrence-1",
        room_id="study",
        robot_registry_id="registry-alpha",
        robot_entity_id="vacuum.alpha",
        program=models.CleaningProgram.VACUUM_THEN_MOP,
        stages=[
            state.CleaningStage(
                models.CleaningOperation.VACUUM,
                2,
                models.StageStatus.RUNNING,
            ),
            state.CleaningStage(models.CleaningOperation.MOP, 1),
        ],
        scheduled_at=WHEN,
        created_at=WHEN,
        adapter_id="roborock",
        adapter_schema_version=2,
    )
    result.water_confirmations["occurrence-1"] = state.WaterConfirmation(
        request_id="request-1",
        occurrence_id="occurrence-1",
        room_id="study",
        robot_registry_id="registry-alpha",
        stage_index=1,
        confirm_hash="a" * 64,
        cancel_hash="b" * 64,
        tag="water-confirmation",
        sent_at=WHEN,
        expires_at=WHEN + timedelta(minutes=10),
    )
    result.water_notification_episodes["study"] = state.WaterNotificationEpisode(
        room_id="study",
        reason="water_confirmation_required",
        first_sent_at=WHEN,
        last_sent_at=WHEN,
    )
    result.robot_faults["registry-alpha"] = state.SchedulerFault(
        reason_code="start_outcome_uncertain",
        robot_registry_id="registry-alpha",
        room_area_id="study",
        occurred_at=WHEN,
        phase="dispatch",
        native_command_may_have_started=True,
        outcome_uncertain=True,
    )
    result.audit.manual_events.append(
        state.ManualAuditRecord(
            at=WHEN,
            robot_registry_id="registry-alpha",
            room_ids=("study",),
            operations=("vacuum",),
            outcome="requested",
        )
    )
    result.audit.recovery_events.append(
        state.RecoveryAuditRecord(
            robot_registry_id="registry-alpha",
            room_ids=("study",),
            at=WHEN,
            reason="observed",
        )
    )
    result.audit.room_decisions.append(
        state.RoomDecisionRecord(
            at=WHEN,
            room_area_id="study",
            reason="waiting for 30 clear minutes",
        )
    )
    result.floor_plan = state.FloorPlanState(
        revision=4,
        rooms={
            "study": state.FloorPlanRectangle("ground", 1, 2, 8, 6),
        },
        sensors={"registry-radar": state.FloorPlanSensorMarker("study", 500, 250)},
    )
    result.first_scheduler_online_at = WHEN - timedelta(minutes=1)
    return result


class SchedulerStateTests(unittest.TestCase):
    def test_retired_settings_are_dropped_without_changing_durable_state(self) -> None:
        expected = populated_state().encode()
        for retired in (
            {"hall_start": "09:00"},
            {"hall_end": "20:00"},
            {"hall_start": "invalid", "hall_end": {"unused": True}},
        ):
            with self.subTest(retired=retired):
                payload = deepcopy(expected)
                payload["global"].update(retired)
                original_payload = deepcopy(payload)
                entry_data = {**ENTRY_DATA, **retired}

                restored, migrated = state.SchedulerState.from_store(
                    payload, entry_data
                )
                self.assertTrue(migrated)
                self.assertEqual(payload, original_payload)
                self.assertEqual(restored.encode(), expected)
                again, migrated_again = state.SchedulerState.from_store(
                    restored.encode(), entry_data
                )
                self.assertFalse(migrated_again)
                self.assertEqual(again.encode(), expected)

    def test_schema_16_round_trip_is_lossless_and_idempotent(self) -> None:
        original = populated_state()

        payload = original.encode()
        restored, migrated = state.SchedulerState.from_store(payload, ENTRY_DATA)
        again, migrated_again = state.SchedulerState.from_store(
            restored.encode(), ENTRY_DATA
        )

        self.assertFalse(migrated)
        self.assertFalse(migrated_again)
        self.assertEqual(again.encode(), payload)
        self.assertEqual(
            restored.audit.manual_events[0].robot_registry_id,
            "registry-alpha",
        )
        self.assertNotIn("robot", payload["audit"]["manual_events"][0])

    def test_schema_16_rejects_an_unknown_persisted_fault_code(self) -> None:
        payload = populated_state().encode()
        payload["robot_faults"]["registry-alpha"]["reason_code"] = "future_fault"

        with self.assertRaises(state.StateSchemaError):
            state.SchedulerState.from_store(payload, ENTRY_DATA)

    def test_each_versioned_schema_migrates_once_to_schema_17(self) -> None:
        for version in range(2, 17):
            with self.subTest(version=version):
                payload = populated_state().to_store()
                payload["schema_version"] = version
                if version < 15:
                    payload.pop("floor_plan")
                if version < 10:
                    payload["scheduler_fault"] = next(
                        iter(payload["robot_faults"].values())
                    )

                migrated, changed = state.SchedulerState.from_store(payload, ENTRY_DATA)
                stable, changed_again = state.SchedulerState.from_store(
                    migrated.encode(), ENTRY_DATA
                )

                self.assertTrue(changed)
                self.assertFalse(changed_again)
                self.assertEqual(stable.encode()["schema_version"], 17)
                self.assertEqual(stable.room_settings["study"].fan_speed, "max")
                self.assertEqual(stable.active_jobs["registry-alpha"].room_id, "study")
                self.assertEqual(stable.audit.manual_events[0].outcome, "requested")

    def test_v1_payload_preserves_settings_history_jobs_holds_and_audit(self) -> None:
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
                        "carpet": True,
                    }
                },
                "robots": {
                    "vacuum.alpha": {
                        "minimum_battery": 85,
                        "double_pass": True,
                    }
                },
            },
            "rooms": {
                "kitchen": {
                    "vacuum": "2026-08-01T09:00:00+00:00",
                    "defer": {"vacuum": "2026-08-05T09:00:00+00:00"},
                    "duration_samples": [
                        {
                            "minutes": 26.5,
                            "operation": "vacuum",
                            "passes": 1,
                            "robot": "vacuum.alpha",
                            "source": "state_transition",
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
                }
            },
            "robot_holds": {"vacuum.alpha": {"reason": "paused", "phase": "held"}},
            "manual_events": [{"outcome": "requested"}],
            "recovery_events": [{"reason": "paused"}],
            "last_preview": {},
        }

        restored, migrated = state.SchedulerState.from_store(payload, ENTRY_DATA)

        self.assertTrue(migrated)
        self.assertTrue(restored.global_settings.party_mode)
        self.assertEqual(restored.room_settings["kitchen"].cleaning_interval, 72)
        self.assertEqual(restored.robot_settings["vacuum.alpha"].minimum_battery, 85)
        self.assertEqual(
            restored.room_history["kitchen"].duration_samples[0].minutes, 26.5
        )
        self.assertEqual(restored.active_jobs["vacuum.alpha"].phase, "paused")
        self.assertEqual(restored.robot_holds["vacuum.alpha"].reason, "paused")
        self.assertEqual(restored.audit.manual_events[0].outcome, "requested")
        encoded = restored.encode()
        self.assertEqual(encoded["schema_version"], 17)
        self.assertNotIn("carpet", encoded["room_settings"]["kitchen"])

    def test_registry_identity_migration_survives_entity_rename(self) -> None:
        scheduler_state = populated_state()
        scheduler_state.robot_settings["vacuum.alpha"] = (
            scheduler_state.robot_settings.pop("registry-alpha")
        )
        scheduler_state.active_jobs["vacuum.alpha"] = scheduler_state.active_jobs.pop(
            "registry-alpha"
        )
        scheduler_state.robot_holds["vacuum.alpha"] = scheduler_state.robot_holds.pop(
            "registry-alpha"
        )
        scheduler_state.robot_cooldowns["vacuum.alpha"] = (
            scheduler_state.robot_cooldowns.pop("registry-alpha")
        )

        changed = state.migrate_robot_identity(
            scheduler_state,
            {"registry-alpha": "vacuum.renamed"},
            {"registry-alpha": "vacuum.alpha"},
        )

        self.assertTrue(changed)
        self.assertIn("registry-alpha", scheduler_state.robot_settings)
        self.assertNotIn("vacuum.alpha", scheduler_state.active_jobs)
        self.assertEqual(
            scheduler_state.occurrences["study"].robot_entity_id,
            "vacuum.renamed",
        )
        self.assertEqual(
            scheduler_state.audit.manual_events[0].robot_registry_id,
            "registry-alpha",
        )
        self.assertFalse(
            state.migrate_robot_identity(
                scheduler_state, {"registry-alpha": "vacuum.renamed"}
            )
        )

    def test_ambiguous_legacy_identity_is_not_guessed(self) -> None:
        scheduler_state = state.SchedulerState.create(ENTRY_DATA)
        scheduler_state.robot_settings["vacuum.old_one"] = state.RobotSettings(
            enabled=False
        )
        scheduler_state.robot_settings["vacuum.old_two"] = state.RobotSettings()

        state.migrate_robot_identity(
            scheduler_state,
            {
                "registry-one": "vacuum.new_one",
                "registry-two": "vacuum.new_two",
            },
        )

        self.assertIn("vacuum.old_one", scheduler_state.robot_settings)
        self.assertIn("vacuum.old_two", scheduler_state.robot_settings)

    def test_unresolved_reference_round_trips_without_becoming_dispatchable(
        self,
    ) -> None:
        scheduler_state = state.SchedulerState.create(ENTRY_DATA)
        scheduler_state.unresolved_robot_references["vacuum.missing"] = (
            state.UnresolvedRobotReference(
                legacy_key="vacuum.missing",
                reason="no_registry_match",
                first_seen_at=WHEN,
                settings=state.RobotSettings(enabled=False),
                occurrence_room_ids=("study",),
            )
        )

        restored, migrated = state.SchedulerState.from_store(
            scheduler_state.encode(), ENTRY_DATA
        )

        self.assertFalse(migrated)
        reference = restored.unresolved_robot_references["vacuum.missing"]
        self.assertEqual(reference.reason, "no_registry_match")
        self.assertNotIn("vacuum.missing", restored.robot_settings)
        self.assertNotIn("vacuum.missing", restored.active_jobs)

    def test_current_schema_rejects_malformed_or_lossy_records(self) -> None:
        mutations = (
            lambda payload: payload.pop("robot_entity_aliases"),
            lambda payload: payload["global"].__setitem__("unresolved_start", "9:00"),
            lambda payload: payload["room_settings"]["study"].__setitem__(
                "fan_speed", ["max"]
            ),
            lambda payload: payload["active_jobs"]["registry-alpha"].__setitem__(
                "phase", "invented"
            ),
            lambda payload: payload["occurrences"]["study"].__setitem__(
                "program", "invented"
            ),
            lambda payload: payload["audit"]["manual_events"][0].__setitem__(
                "robot", "vacuum.alpha"
            ),
            lambda payload: payload["floor_plan"].__setitem__(
                "edges", [["study", "hall"]]
            ),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                payload = populated_state().to_store()
                mutate(payload)
                with self.assertRaises(state.StateSchemaError):
                    state.SchedulerState.from_store(payload, ENTRY_DATA)

    def test_newer_schema_is_rejected_without_a_fallback_write(self) -> None:
        with self.assertRaises(state.StateSchemaError):
            state.SchedulerState.from_store(
                {"schema_version": state.SCHEMA_VERSION + 1}, ENTRY_DATA
            )


class StateCodecBoundaryTests(unittest.TestCase):
    """Exercise corruption handling at the durable JSON boundary."""

    def test_scalar_decoders_are_strict_only_where_schema_requires_it(self) -> None:
        self.assertTrue(state._boolean(None, True, "flag"))
        with self.assertRaisesRegex(state.StateSchemaError, "must be a boolean"):
            state._boolean(1, False, "flag")

        self.assertEqual(state._number(object(), 4.5), 4.5)
        self.assertEqual(state._number("not-a-number", 4.5), 4.5)
        self.assertEqual(state._integer(object(), 4), 4)
        self.assertEqual(state._integer("not-an-integer", 4), 4)
        self.assertEqual(state._bounded_number(None, 5, "value", 0, 10), 5)
        for invalid in (object(), "not-a-number"):
            with (
                self.subTest(invalid=type(invalid).__name__),
                self.assertRaisesRegex(state.StateSchemaError, "must be a number"),
            ):
                state._bounded_number(invalid, 5, "value", 0, 10)
        with self.assertRaisesRegex(state.StateSchemaError, "between 0 and 10"):
            state._bounded_number(11, 5, "value", 0, 10)

        with self.assertRaisesRegex(state.StateSchemaError, "zero-padded"):
            state._daily_time("9:00", "08:00", "window")
        with self.assertRaisesRegex(state.StateSchemaError, "or null"):
            state._optional_daily_time("9:00", "window")
        self.assertIsNone(state._optional_daily_time(None, "window"))
        self.assertEqual(state._optional_daily_time("09:00", "window"), "09:00")

        for invalid in (0, 3, "bad"):
            with (
                self.subTest(pass_count=invalid),
                self.assertRaisesRegex(state.StateSchemaError, "pass_count"),
            ):
                state._optional_pass_count(invalid)
        self.assertIsNone(state._optional_pass_count(None))

    def test_timestamp_collection_and_enum_decoders_fail_closed(self) -> None:
        naive = datetime(2026, 1, 2, 3, 4)
        self.assertEqual(state._timestamp(naive).tzinfo, UTC)
        self.assertEqual(
            state._timestamp("2026-01-02T03:04:00Z"),
            datetime(2026, 1, 2, 3, 4, tzinfo=UTC),
        )
        self.assertIsNone(state._timestamp("not-a-date"))
        self.assertEqual(state._string_list(["one", 2, "three"]), ["one", "three"])
        self.assertEqual(state._string_list("one"), [])
        self.assertEqual(state._sequence((1, 2)), (1, 2))
        self.assertEqual(state._sequence({1, 2}), ())
        self.assertIsNone(state._cleaning_operation("invalid"))
        self.assertEqual(
            state._cleaning_operations(["vacuum", "invalid", "mop"]),
            (models.CleaningOperation.VACUUM, models.CleaningOperation.MOP),
        )
        self.assertIsNone(state._cleaning_program("invalid"))
        self.assertIsNone(state._job_phase("invalid"))
        self.assertIsNone(state._job_source("invalid"))

    def test_profile_decoders_reject_unbounded_or_ill_typed_values(self) -> None:
        self.assertEqual(state._profile_mapping(None), {})
        self.assertEqual(state._profile_sources_mapping(None), {})
        for value, message in (
            ([], "must be an object"),
            ({"unknown": "value"}, "unsupported fields"),
            ({"fan_speed": 1}, "strings or null"),
        ):
            with (
                self.subTest(profile=value),
                self.assertRaisesRegex(state.StateSchemaError, message),
            ):
                state._profile_mapping(value)
        for value in ([], {"unknown": "room"}, {"fan_speed": "other"}):
            with (
                self.subTest(sources=value),
                self.assertRaisesRegex(state.StateSchemaError, "sources"),
            ):
                state._profile_sources_mapping(value)

    def test_legacy_history_records_discard_only_invalid_samples(self) -> None:
        self.assertIsNone(state.OccupancySample.from_mapping({"minutes": 2}))
        self.assertEqual(
            state.OccupancySample.from_mapping(
                {"start": WHEN.isoformat(), "minutes": -2}
            ).minutes,
            0,
        )
        self.assertIsNone(state.DurationSample.from_mapping({}))
        self.assertIsNone(
            state.DurationSample.from_mapping(
                {
                    "operation": "vacuum",
                    "robot": "vacuum.alpha",
                    "source": "observed",
                    "minutes": 0,
                }
            )
        )
        self.assertIsNone(state.Deferral.from_mapping({"source": "legacy"}))

        history = state.RoomHistory.from_mapping(
            {
                "vacuum": "2026-01-01T00:00:00Z",
                "mop": "2026-01-02T00:00:00Z",
                "defer": {
                    "vacuum": "2026-01-03T00:00:00Z",
                    "mop": None,
                    "bad": "not-a-date",
                },
                "deferral_meta": {
                    "mop": {
                        "source": "legacy_metadata",
                        "until": "2026-01-04T00:00:00Z",
                    }
                },
                "samples": [
                    {"start": "2026-01-01T00:00:00Z", "minutes": 5},
                    {"minutes": 9},
                    "invalid",
                ],
                "duration_samples": [
                    {
                        "minutes": 12,
                        "operation": "vacuum",
                        "passes": 1,
                        "robot": "vacuum.alpha",
                        "source": "observed",
                    },
                    {},
                    "invalid",
                ],
            }
        )

        self.assertEqual(
            history.cleaning_completed_at,
            datetime(2026, 1, 2, tzinfo=UTC),
        )
        self.assertEqual(history.deferrals["cleaning"].until.day, 4)
        self.assertEqual(history.deferrals["mop"].source, "legacy_metadata")
        self.assertEqual(len(history.occupancy_samples), 1)
        self.assertEqual(len(history.duration_samples), 1)

    def test_nested_record_parsers_cover_invalid_and_legacy_shapes(self) -> None:
        self.assertIsNone(state.ActiveJob.from_mapping({"operation": "vacuum"}))
        active = state.ActiveJob.from_mapping(
            {
                "rooms": ["study"],
                "operation": "vacuum",
                "phase": "cleaning",
                "source": "scheduler",
            }
        )
        self.assertEqual(active.room_id, "study")
        self.assertIsNone(state.CleaningStage.from_mapping({"operation": "bad"}))
        stage = state.CleaningStage.from_mapping(
            {"operation": "vacuum", "status": "bad"}
        )
        self.assertEqual(stage.status, models.StageStatus.PENDING)
        with self.assertRaisesRegex(state.StateSchemaError, "does not match"):
            state.CleaningStage.from_mapping(
                {
                    "operation": "vacuum",
                    "cleaning_profile": {"operation": "mop"},
                }
            )

        self.assertIsNone(state.CleaningOccurrence.from_mapping({}))
        occurrence = populated_state().occurrences["study"].to_store()
        occurrence["source"] = "invalid"
        with self.assertRaisesRegex(state.StateSchemaError, "source is invalid"):
            state.CleaningOccurrence.from_mapping(occurrence)
        occurrence = populated_state().occurrences["study"].to_store()
        occurrence["manual_mode"] = "automatic"
        with self.assertRaisesRegex(state.StateSchemaError, "manual_mode"):
            state.CleaningOccurrence.from_mapping(occurrence)

        self.assertIsNone(state.WaterConfirmation.from_mapping({}))
        confirmation = populated_state().water_confirmations["occurrence-1"].to_store()
        confirmation["status"] = "bad"
        self.assertEqual(
            state.WaterConfirmation.from_mapping(confirmation).status,
            "pending",
        )
        self.assertIsNone(state.WaterNotificationEpisode.from_mapping({}))
        self.assertIsNone(state.SchedulerFault.from_mapping({}))
        self.assertIsNone(state.RobotHold.from_mapping({}))
        self.assertIsNone(state.RobotCooldown.from_mapping({}))
        self.assertIsNone(state.UnresolvedRobotReference.from_mapping({}))

    def test_floor_plan_codec_rejects_noncanonical_and_invalid_geometry(self) -> None:
        with self.assertRaisesRegex(state.StateSchemaError, "floor_id"):
            state.FloorPlanRectangle.from_mapping(
                {"floor_id": "", "x": 0, "y": 0, "width": 2, "height": 2}
            )
        with self.assertRaises(state.StateSchemaError):
            state.FloorPlanRectangle.from_mapping(
                {"floor_id": "ground", "x": -1, "y": 0, "width": 2, "height": 2}
            )
        with self.assertRaisesRegex(state.StateSchemaError, "area_id"):
            state.FloorPlanSensorMarker.from_mapping({"area_id": "", "x": 0, "y": 0})
        with self.assertRaises(state.StateSchemaError):
            state.FloorPlanSensorMarker.from_mapping(
                {"area_id": "study", "x": 1001, "y": 0}
            )

        valid = populated_state().floor_plan.to_store()
        mutations = (
            lambda payload: payload.__setitem__("revision", True),
            lambda payload: payload.__setitem__("edges", {}),
            lambda payload: payload["rooms"].__setitem__("", {}),
            lambda payload: payload["sensors"].__setitem__("", {}),
            lambda payload: payload["edges"].append(["study"]),
            lambda payload: payload["edges"].append(["z", "a"]),
            lambda payload: payload["edges"].extend(
                [["hall", "study"], ["hall", "study"]]
            ),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                payload = deepcopy(valid)
                mutate(payload)
                with self.assertRaises(state.StateSchemaError):
                    state.FloorPlanState.from_mapping(payload)

    def test_schema_16_validation_rejects_every_typed_section_boundary(self) -> None:
        def replace_section(name, value):
            return lambda payload: payload.__setitem__(name, value)

        mutations = (
            replace_section("room_settings", []),
            lambda payload: payload["robot_settings"].__setitem__("", {}),
            lambda payload: payload["active_jobs"].__setitem__("registry-alpha", []),
            lambda payload: payload["active_jobs"].__setitem__(
                "registry-alpha", {"operation": "vacuum"}
            ),
            lambda payload: payload["robot_holds"].__setitem__(
                "registry-alpha", {"reason": None}
            ),
            lambda payload: payload["room_settings"]["study"].__setitem__(
                "cleaning_program", 1
            ),
            lambda payload: payload["robot_settings"]["registry-alpha"].__setitem__(
                "cleaning_program", "invented"
            ),
            lambda payload: payload["active_jobs"]["registry-alpha"].__setitem__(
                "operation", 1
            ),
            lambda payload: payload["active_jobs"]["registry-alpha"].__setitem__(
                "source", "invented"
            ),
            lambda payload: payload["active_jobs"]["registry-alpha"].__setitem__(
                "requested_operations", "vacuum"
            ),
            lambda payload: payload["active_jobs"]["registry-alpha"].__setitem__(
                "requested_operations", ["invented"]
            ),
            lambda payload: payload["occurrences"]["study"].__setitem__(
                "source", "invented"
            ),
            lambda payload: payload["occurrences"]["study"].__setitem__("stages", []),
            lambda payload: payload["occurrences"]["study"]["stages"].__setitem__(
                0, "invalid"
            ),
            lambda payload: payload["occurrences"]["study"]["stages"][0].__setitem__(
                "operation", "invented"
            ),
            lambda payload: payload["occurrences"]["study"]["stages"][0].__setitem__(
                "status", "invented"
            ),
            lambda payload: payload["robot_entity_aliases"].__setitem__(
                "registry-alpha", 4
            ),
            lambda payload: payload["audit"].__setitem__("manual_events", {}),
            lambda payload: payload["audit"]["manual_events"][0].__setitem__(
                "robot_registry_id", ""
            ),
            lambda payload: payload["audit"]["manual_events"][0].__setitem__(
                "operations", "vacuum"
            ),
            lambda payload: payload["audit"]["manual_events"][0].__setitem__(
                "operations", ["invented"]
            ),
            lambda payload: payload["audit"]["recovery_events"][0].__setitem__(
                "robot", "vacuum.alpha"
            ),
            lambda payload: payload["audit"]["recovery_events"][0].__setitem__(
                "robot_registry_id", 1
            ),
            lambda payload: payload["water_confirmations"]["occurrence-1"].__setitem__(
                "status", "invalid"
            ),
            lambda payload: payload["evaluation"].__setitem__("last_preview", []),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                payload = populated_state().to_store()
                mutate(payload)
                with self.assertRaises(state.StateSchemaError):
                    state.SchedulerState.from_store(payload, ENTRY_DATA)

    def test_identity_migration_handles_collisions_samples_and_audits(self) -> None:
        scheduler_state = populated_state()
        scheduler_state.robot_settings["vacuum.alpha"] = state.RobotSettings(
            enabled=False
        )
        scheduler_state.active_jobs["vacuum.alpha"] = None
        scheduler_state.room_history["study"].duration_samples[
            0
        ].robot_registry_id = "vacuum.alpha"
        scheduler_state.occurrences["study"].robot_registry_id = "vacuum.alpha"
        scheduler_state.audit.manual_events[0] = state.ManualAuditRecord(
            at=WHEN,
            robot_registry_id="vacuum.alpha",
            room_ids=("study",),
            operations=("vacuum",),
            outcome="requested",
        )
        scheduler_state.audit.recovery_events[0] = state.RecoveryAuditRecord(
            robot_registry_id="vacuum.alpha",
            room_ids=("study",),
            at=WHEN,
            reason="observed",
        )

        changed = state.migrate_robot_identity(
            scheduler_state,
            {"registry-alpha": "vacuum.renamed"},
            {"registry-alpha": "vacuum.alpha"},
        )

        self.assertTrue(changed)
        self.assertEqual(
            scheduler_state.room_history["study"].duration_samples[0].robot_registry_id,
            "registry-alpha",
        )
        self.assertEqual(
            scheduler_state.audit.manual_events[0].robot_registry_id,
            "registry-alpha",
        )
        self.assertEqual(
            scheduler_state.audit.recovery_events[0].robot_registry_id,
            "registry-alpha",
        )
        self.assertIsNotNone(scheduler_state.active_jobs["registry-alpha"])

    def test_optional_number_and_frozen_json_boundaries_are_lossless(self) -> None:
        self.assertIsNone(state._optional_number(None))
        self.assertIsNone(state._optional_number(object()))
        self.assertIsNone(state._optional_number("bad"))
        self.assertEqual(state._optional_number("2.5"), 2.5)
        frozen = state.FrozenJsonObject.from_mapping(
            {"z": [1, {"nested": True}], "a": {"value": None}}
        )
        self.assertEqual(
            frozen.to_mapping(),
            {"a": {"value": None}, "z": [1, {"nested": True}]},
        )


if __name__ == "__main__":
    unittest.main()
