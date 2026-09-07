"""Upgrade and restart regressions for retiring the map snapshot feature."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_test_home_assistant,
)

from custom_components.adaptive_robovacs.commands import AcknowledgeRetiredMapCommand
from custom_components.adaptive_robovacs.const import DOMAIN
from custom_components.adaptive_robovacs.integration_core import (
    async_setup_entry,
    async_unload_entry,
)
from custom_components.adaptive_robovacs.repair_service import RepairService
from custom_components.adaptive_robovacs.repairs_manager import (
    retired_map_hold_issue_id,
)
from custom_components.adaptive_robovacs.retired_features import (
    async_remove_retired_map_archive,
)
from custom_components.adaptive_robovacs.state import (
    RobotHold,
    SchedulerState,
    StateSchemaError,
    UnresolvedRobotReference,
)
from custom_components.adaptive_robovacs.storage import SchedulerStore
from tests.test_application_state import ENTRY_DATA, NOW, state_application
from tests.test_ha_integration import _FakeApplication


class RetiredMapTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.hass_context = async_test_home_assistant(config_dir=self.temp_dir.name)
        self.hass = await self.hass_context.__aenter__()
        self.entry = MockConfigEntry(domain=DOMAIN, entry_id="entry-retired-map")
        self.entry.add_to_hass(self.hass)

    async def asyncTearDown(self):
        await self.hass.async_stop(force=True)
        await self.hass_context.__aexit__(None, None, None)
        self.temp_dir.cleanup()

    async def test_upgrade_removes_only_owned_entities_and_archive_before_platforms(
        self,
    ):
        registry = er.async_get(self.hass)
        other = MockConfigEntry(domain=DOMAIN, entry_id="entry-other")
        other.add_to_hass(self.hass)
        removed = []
        for domain, suffix in (
            ("button", "capture_map_snapshot"),
            ("sensor", "map_recovery"),
            ("select", "map_recovery_preview"),
            ("camera", "map_recovery_preview"),
        ):
            record = registry.async_get_or_create(
                domain,
                DOMAIN,
                f"{self.entry.entry_id}_robot_vacuum.missing_{suffix}",
                config_entry=self.entry,
            )
            removed.append(
                registry.async_update_entity(
                    record.entity_id, new_entity_id=f"{domain}.user_renamed"
                ).entity_id
            )
        retained = []
        for domain, platform, unique_id, owner in (
            (
                "sensor",
                DOMAIN,
                f"{self.entry.entry_id}_robot_vacuum.missing_status",
                self.entry,
            ),
            (
                "sensor",
                DOMAIN,
                f"{self.entry.entry_id}_robot__map_recovery",
                self.entry,
            ),
            (
                "number",
                DOMAIN,
                f"{self.entry.entry_id}_robot_vacuum.missing_map_recovery",
                self.entry,
            ),
            (
                "sensor",
                "another_platform",
                f"{self.entry.entry_id}_robot_vacuum.missing_map_recovery",
                self.entry,
            ),
            (
                "button",
                DOMAIN,
                f"{self.entry.entry_id}_robot_vacuum.other_capture_map_snapshot",
                other,
            ),
        ):
            retained.append(
                registry.async_get_or_create(
                    domain, platform, unique_id, config_entry=owner
                ).entity_id
            )
        archive = Store(self.hass, 1, f"{DOMAIN}.map_recovery.{self.entry.entry_id}")
        await archive.async_save({"obsolete": "payload"})
        # Removal never decodes the archive, even if it was corrupted on disk.
        await asyncio.to_thread(
            Path(archive.path).write_text, "invalid json", encoding="utf-8"
        )
        other_archive = Store(self.hass, 1, f"{DOMAIN}.map_recovery.{other.entry_id}")
        await other_archive.async_save({"untouched": True})
        scheduler = SchedulerStore(self.hass, self.entry.entry_id)
        saved = SchedulerState.create(ENTRY_DATA)
        saved.robot_holds["missing"] = RobotHold(
            "map_recovery_pending", "manual_verification", held_at=NOW
        )
        await scheduler.async_save(saved)

        async def forward(_entry, platforms):
            self.assertNotIn("camera", platforms)
            self.assertFalse(await asyncio.to_thread(Path(archive.path).exists))
            self.assertTrue(all(registry.async_get(key) is None for key in removed))
            self.assertTrue(
                all(registry.async_get(key) is not None for key in retained)
            )

        with (
            patch(
                "custom_components.adaptive_robovacs.integration_core.SchedulerApplication",
                _FakeApplication,
            ),
            patch.object(
                self.hass.config_entries,
                "async_forward_entry_setups",
                AsyncMock(side_effect=forward),
            ),
            patch.object(
                self.hass.config_entries,
                "async_unload_platforms",
                AsyncMock(return_value=True),
            ),
        ):
            for _ in range(2):
                self.assertTrue(await async_setup_entry(self.hass, self.entry))
                self.assertTrue(await async_unload_entry(self.hass, self.entry))
        self.assertEqual(
            (await scheduler.async_load(ENTRY_DATA)).state.encode(), saved.encode()
        )
        self.assertEqual(await other_archive.async_load(), {"untouched": True})

    async def test_archive_failure_is_logged_and_retried_without_loading(self):
        store = Mock(
            async_remove=AsyncMock(
                side_effect=[PermissionError("private payload"), None]
            ),
            async_load=AsyncMock(),
        )
        with patch(
            "custom_components.adaptive_robovacs.retired_features.Store",
            return_value=store,
        ):
            with self.assertLogs(
                "custom_components.adaptive_robovacs.retired_features", level="WARNING"
            ) as logs:
                await async_remove_retired_map_archive(self.hass, self.entry.entry_id)
            await async_remove_retired_map_archive(self.hass, self.entry.entry_id)
        self.assertEqual(store.async_remove.await_count, 2)
        store.async_load.assert_not_awaited()
        self.assertNotIn("private payload", " ".join(logs.output))

    def application(self):
        app = state_application()
        app.hass = self.hass
        app.repairs = RepairService(self.hass, self.entry.entry_id)
        app.storage = SchedulerStore(self.hass, self.entry.entry_id)
        app._reset_ready_confirmation = Mock()
        app.state.robot_holds["registry-alpha"] = RobotHold(
            "map_recovery_pending", "manual_verification", held_at=NOW
        )
        registry = er.async_get(self.hass)
        registry.async_get_or_create(
            "vacuum",
            "test_vendor",
            "alpha",
            suggested_object_id="alpha",
            config_entry=self.entry,
        )
        self.set_mapping(
            {"area_mapping": {"study": ["1"]}, "last_seen_segments": [{"id": "1"}]}
        )
        self.hass.states.async_set("vacuum.alpha", "docked")
        return app

    def set_mapping(self, options):
        er.async_get(self.hass).async_update_entity_options(
            "vacuum.alpha", "vacuum", options
        )

    async def test_hold_survives_restart_and_confirmation_saves_only_its_release(self):
        app = self.application()
        app.state.robot_holds["other"] = RobotHold("robot_error", "held", held_at=NOW)
        await app.storage.async_save(app.state)
        app.state = (await app.storage.async_load(ENTRY_DATA)).state
        before = deepcopy(app.state.encode())
        self.assertEqual(
            app._reconcile_robot_hold("registry-alpha", "docked", None, NOW), "held"
        )
        app._sync_retired_map_issues()
        issue_id = retired_map_hold_issue_id(self.entry.entry_id, "registry-alpha")
        self.assertIsNotNone(ir.async_get(self.hass).async_get_issue(DOMAIN, issue_id))
        result = await app._async_execute_command(
            AcknowledgeRetiredMapCommand("registry-alpha", NOW.isoformat())
        )
        self.assertEqual(
            result.as_response(), {"cleared": True, "dispatch_started": False}
        )
        before["robot_holds"].pop("registry-alpha")
        self.assertEqual(app.state.encode(), before)
        self.assertEqual(
            (await app.storage.async_load(ENTRY_DATA)).state.encode(), before
        )
        self.assertIsNone(ir.async_get(self.hass).async_get_issue(DOMAIN, issue_id))
        app.async_evaluate.assert_not_awaited()
        app.async_refresh_discovery.assert_awaited_once_with(notify=False)

    async def test_confirmation_requires_current_hold_and_terminal_robot(self):
        app = self.application()
        original = deepcopy(app.state)
        for state_text in (
            "cleaning",
            "returning",
            "paused",
            "error",
            "unavailable",
            "unknown",
            None,
        ):
            with self.subTest(state=state_text):
                if state_text is None:
                    self.hass.states.async_remove("vacuum.alpha")
                else:
                    self.hass.states.async_set("vacuum.alpha", state_text)
                result = await app.async_acknowledge_retired_map(
                    "registry-alpha", NOW.isoformat()
                )
                self.assertFalse(result["cleared"])
                self.assertEqual(app.state, original)
        self.hass.states.async_set("vacuum.alpha", "idle")
        for hold in (
            None,
            RobotHold("robot_error", "held", held_at=NOW),
            RobotHold(
                "map_recovery_pending", "held", held_at=NOW + timedelta(seconds=1)
            ),
        ):
            with self.subTest(hold=hold):
                app.state.robot_holds = {} if hold is None else {"registry-alpha": hold}
                self.assertEqual(
                    (
                        await app.async_acknowledge_retired_map(
                            "registry-alpha", NOW.isoformat()
                        )
                    )["reason"],
                    "recovery_changed",
                )
        app.state = original
        app.state.active_jobs["registry-alpha"] = Mock()
        self.assertEqual(
            (
                await app.async_acknowledge_retired_map(
                    "registry-alpha", NOW.isoformat()
                )
            )["reason"],
            "awaiting_safe_dock",
        )
        app.state.active_jobs.clear()
        app.discovery = replace(app.discovery, robots={})
        self.assertEqual(
            (
                await app.async_acknowledge_retired_map(
                    "registry-alpha", NOW.isoformat()
                )
            )["reason"],
            "awaiting_safe_dock",
        )

    async def test_confirmation_rejects_unsafe_storage_and_invalid_mapping(self):
        app = self.application()
        for gate in ("_storage_safe_mode", "_closing"):
            setattr(app, gate, True)
            self.assertEqual(
                (
                    await app.async_acknowledge_retired_map(
                        "registry-alpha", NOW.isoformat()
                    )
                )["reason"],
                "recovery_unavailable",
            )
            setattr(app, gate, False)
        for options in (
            {},
            {"area_mapping": {}},
            {"area_mapping": {"study": ["1"]}},
            {"area_mapping": {"study": ["2"]}, "last_seen_segments": [{"id": "1"}]},
            {
                "area_mapping": {"study": ["1_1", "2_2"]},
                "last_seen_segments": [{"id": "1_1"}, {"id": "2_2"}],
            },
        ):
            with self.subTest(options=options):
                self.set_mapping(options)
                self.assertEqual(
                    (
                        await app.async_acknowledge_retired_map(
                            "registry-alpha", NOW.isoformat()
                        )
                    )["reason"],
                    "mapping_invalid",
                )
        er.async_get(self.hass).async_remove("vacuum.alpha")
        self.assertEqual(
            (
                await app.async_acknowledge_retired_map(
                    "registry-alpha", NOW.isoformat()
                )
            )["reason"],
            "mapping_invalid",
        )
        self.assertIn("registry-alpha", app.state.robot_holds)
        app.async_evaluate.assert_not_awaited()

    async def test_failed_save_retains_hold_and_repair(self):
        app = self.application()
        app._sync_retired_map_issues()
        original = deepcopy(app.state)
        app.storage = Mock(async_save=AsyncMock(side_effect=OSError("disk full")))
        with self.assertRaises(OSError):
            await app.async_acknowledge_retired_map("registry-alpha", NOW.isoformat())
        self.assertEqual(app.state, original)
        self.assertIsNotNone(
            ir.async_get(self.hass).async_get_issue(
                DOMAIN, retired_map_hold_issue_id(self.entry.entry_id, "registry-alpha")
            )
        )
        app._notify_listeners.assert_not_called()

    async def test_undated_hold_can_be_confirmed_and_other_entry_repairs_survive(self):
        app = self.application()
        app.state.robot_holds["registry-alpha"].held_at = None
        app._sync_retired_map_issues()
        other = RepairService(self.hass, "entry-other")
        other.sync_retired_map_holds(app.state.robot_holds, ())
        self.assertTrue(
            (await app.async_acknowledge_retired_map("registry-alpha", ""))["cleared"]
        )
        self.assertIsNotNone(
            ir.async_get(self.hass).async_get_issue(
                DOMAIN, retired_map_hold_issue_id("entry-other", "registry-alpha")
            )
        )


class RetiredMapCodecTests(unittest.TestCase):
    def test_old_metadata_is_stripped_once_including_quarantined_holds(self):
        state = SchedulerState.create(ENTRY_DATA)
        hold = RobotHold("map_recovery_pending", "manual_verification", held_at=NOW)
        state.robot_holds["registry-alpha"] = hold
        state.unresolved_robot_references["legacy"] = UnresolvedRobotReference(
            "legacy", "missing", NOW, hold=hold
        )
        expected = state.encode()
        for version in (16, 17):
            for location in ("active", "quarantine"):
                with self.subTest(version=version, location=location):
                    payload = deepcopy(expected)
                    payload["schema_version"] = version
                    target = (
                        payload["robot_holds"]["registry-alpha"]
                        if location == "active"
                        else payload["unresolved_robot_references"]["legacy"]["hold"]
                    )
                    target["requested_map_id"] = {"obsolete": "ignored"}
                    loaded, migrated = SchedulerState.from_store(payload, ENTRY_DATA)
                    self.assertTrue(migrated)
                    self.assertEqual(loaded.encode(), expected)
                    self.assertFalse(
                        SchedulerState.from_store(loaded.encode(), ENTRY_DATA)[1]
                    )
        payload = deepcopy(expected)
        payload["robot_holds"]["registry-alpha"]["reason"] = None
        with self.assertRaises(StateSchemaError):
            SchedulerState.from_store(payload, ENTRY_DATA)


if __name__ == "__main__":
    unittest.main()
