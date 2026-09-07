"""Public service registration and typed-command routing tests."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError

from custom_components.adaptive_robovacs import services
from custom_components.adaptive_robovacs.commands import (
    ClearLegacyDeferralsCommand,
    CommandResult,
    EvaluateCommand,
    ManualCleanRoomCommand,
    RecordManualCleanCommand,
    SaveFloorPlanCommand,
    SetRoomAdjacencyCommand,
)
from custom_components.adaptive_robovacs.const import (
    DOMAIN,
    SERVICE_CLEAR_LEGACY_DEFERRALS,
    SERVICE_EVALUATE,
    SERVICE_LIST_LEGACY_DEFERRALS,
    SERVICE_MANUAL_CLEAN_ROOM,
    SERVICE_RECORD_MANUAL_CLEAN,
    SERVICE_SAVE_FLOOR_PLAN,
    SERVICE_SET_ROOM_ADJACENCY,
)
from custom_components.adaptive_robovacs.runtime_data import (
    AdaptiveRoboVacsRuntimeData,
)


class _Services:
    def __init__(self) -> None:
        self.handlers = {}
        self.present = False

    def has_service(self, _domain, _service):
        return self.present

    def async_register(self, domain, service, handler, **kwargs):
        self.handlers[(domain, service)] = (handler, kwargs)


def harness(entry_count: int = 1):
    application = SimpleNamespace(
        async_execute=AsyncMock(return_value=CommandResult.from_mapping({"ok": True})),
        legacy_deferral_report=lambda: [{"area_id": "study"}],
    )
    entries = [
        SimpleNamespace(
            entry_id=f"entry-{index}",
            state=ConfigEntryState.LOADED,
            runtime_data=AdaptiveRoboVacsRuntimeData(
                coordinator=None,
                application=application,
                lifecycle=None,
            ),
        )
        for index in range(1, entry_count + 1)
    ]
    service_registry = _Services()
    hass = SimpleNamespace(
        services=service_registry,
        config_entries=SimpleNamespace(async_entries=lambda _domain: entries),
        auth=SimpleNamespace(
            async_get_user=AsyncMock(return_value=SimpleNamespace(is_admin=True))
        ),
    )
    return hass, application, entries


def call(data=None, *, user_id="admin", context_id="context"):
    return SimpleNamespace(
        data=data or {},
        context=SimpleNamespace(user_id=user_id, id=context_id),
    )


class ServiceLookupTests(unittest.IsolatedAsyncioTestCase):
    def test_application_lookup_requires_one_loaded_typed_runtime(self) -> None:
        hass, application, entries = harness()
        self.assertIs(services._application(hass), application)
        self.assertIs(services._application(hass, "entry-1"), application)

        entries[0].state = ConfigEntryState.NOT_LOADED
        with self.assertRaisesRegex(ServiceValidationError, "not configured"):
            services._application(hass)
        entries[0].state = ConfigEntryState.LOADED
        entries[0].runtime_data = object()
        with self.assertRaisesRegex(ServiceValidationError, "not configured"):
            services._application(hass)

        hass, _application, _entries = harness(2)
        with self.assertRaisesRegex(ServiceValidationError, "entry_id is required"):
            services._application(hass)
        with self.assertRaisesRegex(ServiceValidationError, "not loaded"):
            services._application(hass, "missing")

    async def test_admin_guard_requires_authenticated_current_admin(self) -> None:
        hass, _application, _entries = harness()
        with self.assertRaisesRegex(ServiceValidationError, "authenticated"):
            await services._require_admin(hass, call(user_id=None))
        hass.auth.async_get_user.return_value = None
        with self.assertRaisesRegex(ServiceValidationError, "administrator"):
            await services._require_admin(hass, call())
        hass.auth.async_get_user.return_value = SimpleNamespace(is_admin=False)
        with self.assertRaisesRegex(ServiceValidationError, "administrator"):
            await services._require_admin(hass, call())


class ServiceRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.hass, self.application, _entries = harness()
        await services.async_register_services(self.hass)

    async def invoke(self, name, data=None):
        handler = self.hass.services.handlers[(DOMAIN, name)][0]
        return await handler(call(data))

    async def test_every_public_service_routes_one_typed_command(self) -> None:
        cases = (
            (SERVICE_EVALUATE, {"dry_run": True}, EvaluateCommand),
            (
                SERVICE_RECORD_MANUAL_CLEAN,
                {
                    "robot_entity_id": "vacuum.alpha",
                    "area_ids": ["study"],
                },
                RecordManualCleanCommand,
            ),
            (
                SERVICE_MANUAL_CLEAN_ROOM,
                {"area_id": "study", "mode": "mop_only"},
                ManualCleanRoomCommand,
            ),
            (
                SERVICE_CLEAR_LEGACY_DEFERRALS,
                {"area_ids": ["study"], "confirm": True},
                ClearLegacyDeferralsCommand,
            ),
            (
                SERVICE_SAVE_FLOOR_PLAN,
                {
                    "floor_id": "ground",
                    "revision": 0,
                    "rooms": {},
                    "edges": [],
                    "sensors": {},
                },
                SaveFloorPlanCommand,
            ),
            (
                SERVICE_SET_ROOM_ADJACENCY,
                {"area_id": "study", "neighbor_area_ids": ["hall"]},
                SetRoomAdjacencyCommand,
            ),
        )
        for name, data, expected in cases:
            with self.subTest(service=name):
                self.application.async_execute.reset_mock()
                result = await self.invoke(name, data)
                self.assertEqual(result, {"ok": True})
                command = self.application.async_execute.await_args.args[0]
                self.assertIsInstance(command, expected)

        result = await self.invoke(SERVICE_LIST_LEGACY_DEFERRALS)
        self.assertEqual(result, {"legacy_deferrals": [{"area_id": "study"}]})

    async def test_registration_is_idempotent_and_keeps_response_contracts(
        self,
    ) -> None:
        self.assertEqual(len(self.hass.services.handlers), 7)
        for retired in (
            "capture_map_snapshot",
            "list_retained_maps",
            "activate_retained_map",
            "confirm_map_selection",
        ):
            self.assertNotIn((DOMAIN, retired), self.hass.services.handlers)
        self.hass.services.present = True
        before = dict(self.hass.services.handlers)
        await services.async_register_services(self.hass)
        self.assertEqual(self.hass.services.handlers, before)
        self.assertIsNone(await services.async_unregister_services(self.hass))


if __name__ == "__main__":
    unittest.main()
