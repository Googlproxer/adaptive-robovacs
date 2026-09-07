"""Application-level tests for the documented manual-clean contract."""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

from custom_components.adaptive_robovacs.application import SchedulerApplication
from custom_components.adaptive_robovacs.discovery import (
    DiscoveredRobot,
    DiscoveredRoom,
    DiscoverySnapshot,
    RobotProfile,
)
from custom_components.adaptive_robovacs.models import AdapterCapabilities
from custom_components.adaptive_robovacs.state import (
    RobotHold,
    SchedulerFault,
    SchedulerState,
)

ENTRY_DATA = {
    "observe_only": False,
    "forecast_confidence": 75,
    "unresolved_start": "00:00",
    "unresolved_end": "04:00",
}
WHEN = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class _States:
    def __init__(self) -> None:
        self.state = "docked"

    def get(self, _entity_id):
        return SimpleNamespace(state=self.state, attributes={"battery_level": 5})


def discovered_robot() -> DiscoveredRobot:
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
            supported_operations=frozenset({"vacuum"}),
            vacuum_pass_counts=frozenset({1}),
        ),
    )


def application() -> tuple[SchedulerApplication, _States]:
    """Build an application transaction with real policy methods and fake I/O."""

    app = SchedulerApplication.__new__(SchedulerApplication)
    states = _States()
    app.hass = SimpleNamespace(states=states)
    app.entry = SimpleNamespace(entry_id="entry-1", data=ENTRY_DATA)
    app.state = SchedulerState.create(ENTRY_DATA)
    room = DiscoveredRoom(
        "study",
        "Study",
        "ground",
        frozenset(),
    )
    robot = discovered_robot()
    app.discovery = DiscoverySnapshot(
        MappingProxyType({robot.entity_id: robot}),
        MappingProxyType({room.area_id: room}),
    )
    room_settings, _history = app.state.ensure_room("study", is_bedroom=False)
    room_settings.enabled = False
    robot_settings = app.state.ensure_robot("registry-alpha", supports_mopping=False)
    robot_settings.enabled = False
    robot_settings.minimum_battery = 100
    app.state.robot_holds["registry-alpha"] = RobotHold(
        reason="paused",
        phase="held",
        held_at=WHEN,
    )
    app.state.robot_faults["registry-alpha"] = SchedulerFault(
        reason_code="generic_dispatch_failed",
        robot_registry_id="registry-alpha",
        room_area_id="study",
        occurred_at=WHEN,
        phase="dispatch",
    )
    app._storage_safe_mode = False
    app._startup_state_settle_until = None
    app._closing = False
    app._lock = asyncio.Lock()
    app.async_refresh_discovery = AsyncMock()
    app._observe_occupancy = Mock()
    app._async_reconcile_jobs = AsyncMock()
    app._refresh_robot_readiness = Mock()
    app._robot_battery = Mock(return_value=5)
    app._async_prepare_occurrence = AsyncMock(
        side_effect=lambda _robot, candidate, _now: (candidate, "ready")
    )
    app._async_refresh_pending_profile_if_needed = AsyncMock(
        side_effect=lambda _robot, candidate: candidate
    )
    app._async_dispatch = AsyncMock(return_value=(True, "dispatched Study"))
    app._async_save = AsyncMock()
    app._notify_listeners = Mock()
    return app, states


class ManualApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_clean_bypasses_scheduler_policy_but_requires_dock(
        self,
    ) -> None:
        app, _states = application()

        result = await app.async_manual_clean_room(
            "study",
            "vacuum_only",
            context_id="context-1",
            user_id="user-1",
        )

        self.assertEqual(
            result,
            {
                "accepted": True,
                "status": "started",
                "reason": "dispatched Study",
                "robot_entity_id": "vacuum.alpha",
            },
        )
        app._async_dispatch.assert_awaited_once()
        dispatched = app._async_dispatch.await_args.args[1]
        self.assertTrue(dispatched.manual_override)
        self.assertTrue(dispatched.bypass_forecast)
        self.assertTrue(dispatched.bypass_desired_window)
        audit = app.state.audit.manual_events[-1]
        self.assertEqual(audit.robot_registry_id, "registry-alpha")
        self.assertEqual(audit.operations, ("vacuum",))

    async def test_every_non_bypassable_global_mode_rejects_without_dispatch(
        self,
    ) -> None:
        cases = (
            (
                "observe",
                lambda app: setattr(app.state.global_settings, "observe_only", True),
            ),
            (
                "party",
                lambda app: setattr(app.state.global_settings, "party_mode", True),
            ),
            ("storage", lambda app: setattr(app, "_storage_safe_mode", True)),
            (
                "startup",
                lambda app: setattr(
                    app,
                    "_startup_state_settle_until",
                    datetime.max.replace(tzinfo=UTC),
                ),
            ),
            ("shutdown", lambda app: setattr(app, "_closing", True)),
        )
        for name, configure in cases:
            with self.subTest(name=name):
                app, _states = application()
                configure(app)
                result = await app.async_manual_clean_room("study", "vacuum_only")
                self.assertFalse(result["accepted"])
                app._async_dispatch.assert_not_awaited()

    async def test_fresh_dock_revalidation_blocks_after_preparation(self) -> None:
        app, states = application()

        async def prepare(_robot, candidate, _now):
            states.state = "cleaning"
            return candidate, "ready"

        app._async_prepare_occurrence = AsyncMock(side_effect=prepare)

        result = await app.async_manual_clean_room("study", "vacuum_only")

        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "robot is not docked")
        app._async_dispatch.assert_not_awaited()

    async def test_unknown_mode_is_rejected_before_any_state_change(self) -> None:
        app, _states = application()

        with self.assertRaisesRegex(ValueError, "Unknown manual cleaning mode"):
            await app.async_manual_clean_room("study", "steam_clean")

        app.async_refresh_discovery.assert_not_awaited()
        self.assertEqual(app.state.audit.manual_events, [])

    async def test_missing_faulted_and_incompatible_rooms_are_rejected_safely(
        self,
    ) -> None:
        app, _states = application()
        result = await app.async_manual_clean_room("missing", "vacuum_only")
        self.assertEqual(
            result["reason"], "room is not discovered by this config entry"
        )

        app, _states = application()
        app.state.room_faults["study"] = SchedulerFault(
            "blocked", "registry-alpha", "study", WHEN, "dispatch"
        )
        result = await app.async_manual_clean_room("study", "vacuum_only")
        self.assertEqual(result["reason"], "room dispatch blocked pending Repair")

        app, states = application()
        states.state = "cleaning"
        result = await app.async_manual_clean_room("study", "vacuum_only")
        self.assertEqual(result["reason"], "Alpha: robot is not docked")

        app, _states = application()
        app._candidate_for_robot = Mock(return_value=None)
        result = await app.async_manual_clean_room("study", "vacuum_only")
        self.assertEqual(
            result["reason"], "no ready robot has a compatible cleaning profile"
        )
        app._async_dispatch.assert_not_awaited()

    async def test_highest_battery_same_floor_robot_wins_stable_assignment(
        self,
    ) -> None:
        app, _states = application()
        first = app.discovery.robots["vacuum.alpha"]
        second = replace(
            first,
            entity_id="vacuum.beta",
            registry_id="registry-beta",
            name="Beta",
            device_id="device-beta",
        )
        app.discovery = DiscoverySnapshot(
            MappingProxyType({first.entity_id: first, second.entity_id: second}),
            app.discovery.rooms,
        )
        app.state.ensure_robot("registry-beta", supports_mopping=False)
        app._robot_battery = Mock(
            side_effect=lambda robot: 30 if robot.entity_id == "vacuum.alpha" else 90
        )

        result = await app.async_manual_clean_room("study", "vacuum_only")

        self.assertEqual(result["robot_entity_id"], "vacuum.beta")
        self.assertEqual(
            app.state.audit.manual_events[-1].robot_registry_id, "registry-beta"
        )

    async def test_preparation_pending_cancelled_and_prior_state_cleanup(self) -> None:
        for with_confirmation in (False, True):
            with self.subTest(with_confirmation=with_confirmation):
                app, _states = application()
                old = SimpleNamespace(occurrence_id="old-occurrence")
                app.state.occurrences["study"] = old
                app.state.water_confirmations["old-occurrence"] = SimpleNamespace()

                async def pending(
                    _robot,
                    _candidate,
                    _now,
                    *,
                    app=app,
                    with_confirmation=with_confirmation,
                ):
                    current = SimpleNamespace(occurrence_id="new-occurrence")
                    app.state.occurrences["study"] = current
                    if with_confirmation:
                        app.state.water_confirmations["new-occurrence"] = (
                            SimpleNamespace()
                        )
                    return None, "waiting"

                app._async_prepare_occurrence = AsyncMock(side_effect=pending)
                result = await app.async_manual_clean_room("study", "vacuum_only")
                self.assertTrue(result["accepted"])
                self.assertEqual(result["status"], "pending")
                self.assertNotIn("old-occurrence", app.state.water_confirmations)
                self.assertEqual(
                    app.state.audit.manual_events[-1].outcome,
                    "awaiting_confirmation" if with_confirmation else "accepted",
                )

        app, _states = application()
        app._async_prepare_occurrence = AsyncMock(
            return_value=(None, "mopping unavailable")
        )
        result = await app.async_manual_clean_room("study", "vacuum_only")
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "mopping unavailable")

    async def test_changed_global_gate_after_preparation_cleans_occurrence(
        self,
    ) -> None:
        configurations = (
            (lambda app: setattr(app, "_closing", True), "coordinator shutting down"),
            (
                lambda app: setattr(app.state.global_settings, "observe_only", True),
                "observe-only mode",
            ),
            (
                lambda app: setattr(app.state.global_settings, "party_mode", True),
                "party mode",
            ),
        )
        for configure, reason in configurations:
            with self.subTest(reason=reason):
                app, _states = application()

                async def prepare(
                    _robot,
                    candidate,
                    _now,
                    *,
                    app=app,
                    configure=configure,
                ):
                    app.state.occurrences["study"] = SimpleNamespace(
                        occurrence_id="prepared"
                    )
                    app.state.water_confirmations["prepared"] = SimpleNamespace()
                    configure(app)
                    return candidate, "ready"

                app._async_prepare_occurrence = AsyncMock(side_effect=prepare)
                result = await app.async_manual_clean_room("study", "vacuum_only")
                self.assertFalse(result["accepted"])
                self.assertEqual(result["reason"], reason)
                self.assertNotIn("study", app.state.occurrences)
                self.assertNotIn("prepared", app.state.water_confirmations)
                app._async_dispatch.assert_not_awaited()

    async def test_dispatch_failure_is_audited_and_only_orphan_work_is_removed(
        self,
    ) -> None:
        for active_job in (False, True):
            with self.subTest(active_job=active_job):
                app, _states = application()

                async def prepare(
                    _robot,
                    candidate,
                    _now,
                    *,
                    active_job=active_job,
                    app=app,
                ):
                    app.state.occurrences["study"] = SimpleNamespace(
                        occurrence_id="prepared"
                    )
                    app.state.water_confirmations["prepared"] = SimpleNamespace()
                    if active_job:
                        app.state.active_jobs["registry-alpha"] = SimpleNamespace()
                    return candidate, "ready"

                app._async_prepare_occurrence = AsyncMock(side_effect=prepare)
                app._async_dispatch = AsyncMock(
                    return_value=(False, "safe dispatch failure")
                )
                result = await app.async_manual_clean_room("study", "vacuum_only")
                self.assertFalse(result["accepted"])
                self.assertEqual(result["status"], "failed")
                self.assertEqual(app.state.audit.manual_events[-1].outcome, "failed")
                self.assertEqual(
                    "study" in app.state.occurrences,
                    active_job,
                )


if __name__ == "__main__":
    unittest.main()
