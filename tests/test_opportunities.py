"""Behavioural coverage for bounded opportunity accounting and durable evidence."""

from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from custom_components.adaptive_robovacs.opportunities import (
    advance_opportunity,
    blocker_category,
    missed_cadences,
    opportunity_advice,
)
from custom_components.adaptive_robovacs.state import (
    OpportunityWindow,
    RoomOpportunity,
    SchedulerState,
    StateSchemaError,
)
from tests.test_state import populated_state

NOW = datetime(2026, 10, 5, 9, tzinfo=UTC)
DUE = NOW - timedelta(days=3)


def advance(record, now=NOW, **overrides):
    options = dict(
        now=now,
        due=DUE,
        reason="forecast",
        in_window=True,
        window_end=NOW + timedelta(hours=1),
        continuous=True,
    )
    options.update(overrides)
    return advance_opportunity(record, **options)


class OpportunityTests(unittest.TestCase):
    def test_schema_18_defaults_preserve_every_existing_durable_section(self):
        payload = populated_state().to_store()
        payload["schema_version"] = 18
        for settings in payload["room_settings"].values():
            for key in (
                "occupancy_sensitivity_minutes",
                "vacancy_fallback_enabled",
                "vacancy_fallback_minutes",
            ):
                settings.pop(key)
        for history in payload["room_history"].values():
            history.pop("opportunity")
            history.pop("occupancy_transitions")
        for occurrence in payload["occurrences"].values():
            occurrence.pop("mop_delay_expirations")
        before = copy.deepcopy(payload)
        state, migrated = SchedulerState.from_store(payload, {})
        self.assertTrue(migrated)
        self.assertEqual(payload, before)
        settings = state.room_settings["study"]
        self.assertTrue(settings.vacancy_fallback_enabled)
        self.assertEqual(settings.occupancy_sensitivity_minutes, 0)
        self.assertEqual(settings.vacancy_fallback_minutes, 0)
        current = state.to_store()
        for key, value in before.items():
            if key not in {
                "schema_version",
                "room_settings",
                "room_history",
                "occurrences",
            }:
                self.assertEqual(current[key], value, key)
        for section in ("room_settings", "room_history", "occurrences"):
            for item_id, value in before[section].items():
                for key, field_value in value.items():
                    self.assertEqual(current[section][item_id][key], field_value, key)

    def test_unchanged_blocker_closes_a_complete_window(self):
        record = advance(RoomOpportunity())
        record = advance(record, NOW + timedelta(minutes=30))
        record = advance(
            record,
            NOW + timedelta(hours=1),
            reason="outside_hours",
            in_window=False,
            window_end=None,
        )
        self.assertEqual(dict(record.totals), {"forecast": 60})
        self.assertEqual(len(record.windows), 1)
        self.assertEqual(dict(record.windows[0].minutes), {"forecast": 60})
        self.assertTrue(record.observed_window)

    def test_intervals_count_once_and_unchanged_short_calls_do_not_write(self):
        record = advance(RoomOpportunity())
        self.assertIs(advance(record, NOW + timedelta(seconds=10)), record)
        record = advance(record, NOW + timedelta(minutes=15), reason="occupancy")
        record = advance(record, NOW + timedelta(minutes=30), reason="adjacency")
        self.assertEqual(dict(record.totals), {"forecast": 15, "occupancy": 15})

    def test_restart_gap_is_unknown_and_cannot_prove_a_window(self):
        record = advance(RoomOpportunity())
        record = advance(
            record,
            NOW + timedelta(hours=2),
            continuous=False,
            in_window=False,
            window_end=None,
            reason="outside_hours",
        )
        self.assertEqual(dict(record.totals), {"unknown": 120})
        self.assertFalse(record.observed_window)
        self.assertEqual(dict(record.windows[0].minutes), {"unknown": 60})

    def test_window_history_is_bounded_without_losing_totals(self):
        record = RoomOpportunity()
        for day in range(20):
            start = NOW + timedelta(days=day)
            end = start + timedelta(hours=1)
            record = advance(record, start, window_end=end)
            record = advance(
                record, end, reason="outside_hours", in_window=False, window_end=None
            )
        self.assertEqual(len(record.windows), 14)
        self.assertEqual(dict(record.totals)["forecast"], 1200)

    def test_cadence_threshold_and_advice_are_explicit(self):
        self.assertEqual(missed_cadences(NOW - timedelta(seconds=1), NOW, 24), 0)
        self.assertEqual(missed_cadences(NOW, NOW, 24), 1)
        self.assertEqual(missed_cadences(NOW + timedelta(days=2), NOW, 24), 3)
        for invalid in (0, -1, float("nan"), float("inf")):
            self.assertEqual(missed_cadences(NOW, NOW, invalid), 0)
        record = RoomOpportunity(
            totals=(("outside_hours", 1000), ("forecast", 10), ("adjacency", 20))
        )
        self.assertEqual(opportunity_advice(record)[0], "adjacency")
        self.assertEqual(
            opportunity_advice(record, repeated_mop=True)[0], "mop_follow_up"
        )
        self.assertEqual(opportunity_advice(RoomOpportunity())[0], "other")
        for reason, expected in (
            ("adjacent forecast", "adjacency"),
            ("clear minutes", "forecast"),
            ("occupancy occupied", "occupancy"),
            ("water approval", "water"),
            ("Repair needed", "recovery"),
            ("robot not docked", "robot"),
            ("waiting for desired cleaning window", "outside_hours"),
            ("assigned", "eligible"),
            ("unknown", "other"),
        ):
            self.assertEqual(blocker_category(reason), expected)

    def test_store_round_trip_migration_and_corruption(self):
        record = advance(RoomOpportunity())
        record = advance(
            record,
            NOW + timedelta(hours=1),
            reason="outside_hours",
            in_window=False,
            window_end=None,
        )
        record = replace(record, alert_signature="forecast", alert_at=NOW)
        self.assertEqual(RoomOpportunity.from_mapping(record.to_store()), record)
        state = SchedulerState.create({})
        state.ensure_room("test-room", is_bedroom=False)
        state.room_history["test-room"].opportunity = record
        state.room_settings["test-room"].vacancy_fallback_minutes = 90
        payload = state.to_store()
        snapshot = copy.deepcopy(payload)
        restored, migrated = SchedulerState.from_store(payload, {})
        self.assertFalse(migrated)
        self.assertEqual(restored.to_store(), payload)
        self.assertEqual(payload, snapshot)
        legacy = copy.deepcopy(payload)
        legacy["schema_version"] = 18
        old, migrated = SchedulerState.from_store(legacy, {})
        self.assertTrue(migrated)
        self.assertEqual(old.room_history["test-room"].opportunity, record)
        for changes in (
            {"reason": "invented"},
            {"accounted_at": "bad"},
            {"totals": {"forecast": -1}},
            {"totals": {"secret": 1}},
            {"windows": [record.windows[0].to_store()] * 15},
            {"windows": [{"started_at": NOW.isoformat(), "ended_at": "bad"}]},
            {"in_window": "yes"},
        ):
            with self.subTest(changes=changes), self.assertRaises(StateSchemaError):
                RoomOpportunity.from_mapping({**record.to_store(), **changes})
        with self.assertRaises(StateSchemaError):
            OpportunityWindow.from_mapping(
                {"started_at": NOW.isoformat(), "ended_at": DUE.isoformat()}
            )
