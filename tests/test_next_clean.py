"""Canonical timestamp stability, robot membership, and display-only updates."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import timedelta
from types import MappingProxyType, SimpleNamespace
from unittest.mock import Mock, patch

from custom_components.adaptive_robovacs.discovery import DiscoverySnapshot
from custom_components.adaptive_robovacs.models import JobPhase
from custom_components.adaptive_robovacs.projections import build_snapshot
from custom_components.adaptive_robovacs.room_status import (
    robot_preview_reason,
    room_robot_previews,
    room_status,
)
from custom_components.adaptive_robovacs.schedule_clock import (
    SchedulePresentationClock,
    advance_room_window,
)
from custom_components.adaptive_robovacs.sensor import (
    _RoomScheduleSensor,
    _RoomStatusSensor,
)
from custom_components.adaptive_robovacs.snapshots import RoomRobotPreviewView
from custom_components.adaptive_robovacs.state import Deferral, RobotHold
from tests.test_application_planning import occurrence, planning_application
from tests.test_application_state import NOW, active_job


class NextCleanProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = planning_application()
        self.app.hass.states.values["vacuum.alpha"].attributes = {}
        self.settings = self.app.state.room_settings["study"]
        self.history = self.app.state.room_history["study"]

    def snapshot(self, now=NOW):
        with (
            patch(
                "custom_components.adaptive_robovacs.projections._now", return_value=now
            ),
            patch(
                "custom_components.adaptive_robovacs.application.core._now",
                return_value=now,
            ),
        ):
            return build_snapshot(self.app)

    def test_future_and_occupied_rooms_retain_membership_and_timestamp(self) -> None:
        self.history.cleaning_completed_at = NOW
        self.history.occupancy = "occupied"
        first = self.snapshot()
        second = self.snapshot(NOW + timedelta(minutes=31))
        self.assertEqual(first.rooms[0].next_clean_at, NOW + timedelta(days=1))
        self.assertEqual(first.rooms[0].next_clean_at, second.rooms[0].next_clean_at)
        self.assertIsNone(first.rooms[0].next_candidate)
        self.assertEqual(
            first.rooms[0].robot_previews,
            (RoomRobotPreviewView("vacuum.alpha", "conditional", None),),
        )
        self.assertEqual(room_status(first.rooms[0]), "Scheduled")

    def test_cadence_baseline_recognised_deferrals_and_pending_stage(self) -> None:
        self.history.cleaning_completed_at = None
        self.app.state.first_scheduler_online_at = NOW
        self.assertEqual(
            self.snapshot().rooms[0].next_clean_at, NOW + timedelta(days=1)
        )
        self.app.state.first_scheduler_online_at = None
        self.assertIsNone(self.snapshot().rooms[0].next_clean_at)
        pending = occurrence()
        pending.scheduled_at = NOW - timedelta(hours=1)
        self.app.state.occurrences["study"] = pending
        self.assertEqual(self.snapshot().rooms[0].next_clean_at, pending.scheduled_at)
        for source, area, expected in (
            ("manual_clean", "study", NOW),
            ("affected_cancellation", "study", NOW),
            ("unrecognised", "study", pending.scheduled_at),
            ("manual_clean", "other", pending.scheduled_at),
        ):
            self.history.deferrals["cleaning"] = Deferral(NOW, source, NOW, area)
            self.assertEqual(self.snapshot().rooms[0].next_clean_at, expected)
        pending.source = "manual_dashboard"
        self.history.deferrals["cleaning"] = Deferral(NOW, "manual_clean", NOW, "study")
        self.assertEqual(self.snapshot().rooms[0].next_clean_at, pending.scheduled_at)

    def test_global_room_modes_and_all_active_phases_clear_timestamp(self) -> None:
        due = self.snapshot().rooms[0].next_clean_at
        for flag in ("observe_only", "party_mode"):
            setattr(self.app.state.global_settings, flag, True)
            view = self.snapshot().rooms[0]
            self.assertIsNone(view.next_clean_at)
            self.assertEqual(view.robot_previews[0].status, "blocked")
            setattr(self.app.state.global_settings, flag, False)
            self.assertEqual(self.snapshot().rooms[0].next_clean_at, due)
        self.app._storage_safe_mode = True
        self.assertIsNone(self.snapshot().rooms[0].next_clean_at)
        self.app._storage_safe_mode = False
        self.settings.enabled = False
        self.assertIsNone(self.snapshot().rooms[0].next_clean_at)
        self.settings.enabled = True
        for phase in JobPhase:
            job = active_job()
            job.phase = phase
            self.app.state.active_jobs["registry-alpha"] = job
            self.assertIsNone(self.snapshot().rooms[0].next_clean_at)
        self.app.state.active_jobs.clear()
        self.assertEqual(self.snapshot().rooms[0].next_clean_at, due)

    def test_inherited_ignored_and_occurrence_bypassed_windows(self) -> None:
        self.settings.ignore_desired_window = False
        self.app.state.global_settings.unresolved_start = "08:00"
        self.app.state.global_settings.unresolved_end = "18:00"
        with patch(
            "custom_components.adaptive_robovacs.projections.dt_util.as_local",
            side_effect=lambda value: value,
        ):
            first = self.snapshot().rooms[0]
            self.assertEqual(first.next_clean_at, NOW.replace(hour=8))
            self.assertEqual(first.next_clean_window_end_at, NOW.replace(hour=18))
            self.settings.desired_window_start = "12:00"
            self.assertEqual(
                self.snapshot().rooms[0].next_clean_at, NOW.replace(hour=12)
            )
            self.settings.ignore_desired_window = True
            self.assertLess(self.snapshot().rooms[0].next_clean_at, NOW)
            self.settings.ignore_desired_window = False
            pending = occurrence()
            pending.bypass_desired_window = True
            self.app.state.occurrences["study"] = pending
            self.assertEqual(
                self.snapshot().rooms[0].next_clean_at, pending.scheduled_at
            )

    def test_shared_floors_renames_reassignment_and_per_robot_hold(self) -> None:
        original = self.app.discovery.robots["vacuum.alpha"]
        other = replace(
            original, entity_id="vacuum.beta", registry_id="registry-beta", name="Beta"
        )
        self.app.state.ensure_robot("registry-beta", supports_mopping=True)
        self.app._robot_ready = lambda robot: (
            robot.registry_id == "registry-alpha",
            "Robot held",
        )
        self.app.discovery = DiscoverySnapshot(
            MappingProxyType({original.entity_id: original, other.entity_id: other}),
            self.app.discovery.rooms,
        )
        view = self.snapshot().rooms[0]
        self.assertEqual(view.robot_previews[0].status, "conditional")
        self.assertEqual(view.robot_previews[1].reason, "Robot held")
        renamed = replace(original, entity_id="vacuum.renamed", name="New name")
        other = replace(other, floor_id="upper")
        self.app.discovery = DiscoverySnapshot(
            MappingProxyType({renamed.entity_id: renamed, other.entity_id: other}),
            self.app.discovery.rooms,
        )
        view = self.snapshot().rooms[0]
        self.assertEqual(
            tuple(item.robot_entity_id for item in view.robot_previews),
            ("vacuum.renamed",),
        )

    def test_restrictions_do_not_rewrite_canonical_time(self) -> None:
        original = self.snapshot()
        self.app.state.robot_holds["registry-alpha"] = RobotHold(
            "test", "Robot held", held_at=NOW
        )
        held = self.snapshot()
        self.assertEqual(held.rooms[0].next_clean_at, original.rooms[0].next_clean_at)
        self.assertEqual(held.rooms[0].robot_previews[0].status, "blocked")
        room = original.rooms[0]
        for field in ("failure", "recovery"):
            blocked = replace(room, **{field: object()})
            previews = room_robot_previews(blocked, original.robots, original.scheduler)
            self.assertEqual(previews[0].status, "blocked")
            self.assertEqual(blocked.next_clean_at, room.next_clean_at)

    def test_sensor_contract_keeps_only_identity_on_existing_timestamp(self) -> None:
        snapshot = self.snapshot()
        coordinator = SimpleNamespace(
            data=snapshot, entry=SimpleNamespace(entry_id="entry-1")
        )
        timestamp = _RoomScheduleSensor(coordinator, "study", "Study")
        status = _RoomStatusSensor(coordinator, "study", "Study")
        self.assertEqual(timestamp.unique_id, "entry-1_room_study_next_clean")
        self.assertEqual(status.unique_id, "entry-1_room_study_status")
        self.assertEqual(timestamp.device_class, "timestamp")
        self.assertEqual(
            set(timestamp.extra_state_attributes),
            {
                "adaptive_robovacs_entry_id",
                "adaptive_robovacs_role",
                "area_id",
                "room",
                "floor_id",
            },
        )
        self.assertIn("vacancy_diagnostic", status.extra_state_attributes)
        self.assertEqual(
            status.extra_state_attributes["robot_entity_ids"], ["vacuum.alpha"]
        )
        before = (timestamp.native_value, timestamp.extra_state_attributes)
        coordinator.data = self.snapshot(NOW + timedelta(minutes=30))
        self.assertEqual(
            before, (timestamp.native_value, timestamp.extra_state_attributes)
        )

    def test_window_clock_publishes_only_at_boundary_and_cleans_up(self) -> None:
        snapshot = self.snapshot()
        room = replace(
            snapshot.rooms[0],
            next_clean_at=NOW.replace(hour=8),
            schedule_due_at=NOW - timedelta(days=1),
            next_clean_window_end_at=NOW.replace(hour=18),
            desired_window_effective_start="08:00",
            desired_window_effective_end="18:00",
        )
        snapshot = replace(snapshot, rooms=(room,))
        self.assertIs(advance_room_window(room, NOW), room)
        publish = Mock()
        cancel = Mock()
        with (
            patch(
                "custom_components.adaptive_robovacs.schedule_clock.async_track_point_in_utc_time",
                return_value=cancel,
            ) as track,
            patch(
                "custom_components.adaptive_robovacs.schedule_clock.dt_util.as_local",
                side_effect=lambda value: value,
            ),
        ):
            clock = SchedulePresentationClock(Mock(), publish)
            clock.update(snapshot)
            self.assertEqual(track.call_args.args[2], NOW.replace(hour=18))
            publish.assert_not_called()
            clock._advance(NOW.replace(hour=18))
            updated = publish.call_args.args[0]
            self.assertEqual(updated.rooms[0].next_clean_at, NOW.replace(day=6, hour=8))
            self.assertIs(updated.scheduler, snapshot.scheduler)
            self.assertIs(updated.rooms[0].robot_previews, room.robot_previews)
            self.app.storage.async_save.assert_not_called()
            self.app.async_evaluate.assert_not_called()
            clock.stop()
            clock._advance(NOW + timedelta(days=1))
            publish.assert_called_once()
            self.assertIsNone(clock._cancel)
            clock.update(replace(snapshot, rooms=()))
            self.assertIsNone(clock._cancel)

    def test_preview_pending_stage_compatibility_water_and_missing_time(self) -> None:
        self.app.state.occurrences["study"] = occurrence()
        snapshot = self.snapshot()
        room, robot, scheduler = (
            snapshot.rooms[0],
            snapshot.robots[0],
            snapshot.scheduler,
        )
        self.assertIsNone(robot_preview_reason(room, robot, scheduler))
        pending = room.occurrence
        other = replace(pending, robot_entity_id="vacuum.other")
        self.assertEqual(
            robot_preview_reason(replace(room, occurrence=other), robot, scheduler),
            "Occurrence assigned to another robot",
        )
        complete = replace(pending, current_stage=len(pending.stages))
        self.assertEqual(
            robot_preview_reason(replace(room, occurrence=complete), robot, scheduler),
            "Completion pending",
        )
        unsupported = replace(
            robot,
            adapter_capabilities=replace(
                robot.adapter_capabilities, supported_operations=frozenset()
            ),
        )
        self.assertEqual(
            robot_preview_reason(room, unsupported, scheduler),
            "Robot does not support the scheduled stage",
        )
        waiting = replace(room, water_confirmation=SimpleNamespace(status="pending"))
        self.assertEqual(
            robot_preview_reason(waiting, robot, scheduler),
            "Waiting for water confirmation",
        )
        missing = replace(room, next_clean_at=None)
        self.assertEqual(
            robot_preview_reason(missing, robot, scheduler), "No valid schedule time"
        )
