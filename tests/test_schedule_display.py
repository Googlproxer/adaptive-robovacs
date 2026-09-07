"""Exercise HA state writes and Kiosk templates in a real test HA runtime."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from homeassistant.helpers.template import Template
from pytest_homeassistant_custom_component.common import async_test_home_assistant

from custom_components.adaptive_robovacs.sensor import _RoomScheduleSensor
from tests.test_entities import _Coordinator

TEMPLATE = (
    Path(__file__).parents[1] / "dashboard" / "room-schedule-template.jinja"
).read_text(encoding="utf-8")
NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)


class ScheduleDisplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.context = async_test_home_assistant(config_dir=self.temp.name)
        self.hass = await self.context.__aenter__()
        await self.hass.config.async_set_time_zone("UTC")
        self.events = []
        self.hass.bus.async_listen("state_changed", self.events.append)

    async def asyncTearDown(self) -> None:
        await self.hass.async_stop(force=True)
        await self.context.__aexit__(None, None, None)
        self.temp.cleanup()

    def set_robot(self, *, entry="entry_1", entity="vacuum.alpha", state="ready"):
        self.hass.states.async_set(
            "sensor.robot_" + entry,
            state,
            {
                "adaptive_robovacs_role": "robot_status",
                "adaptive_robovacs_entry_id": entry,
                "robot_entity_id": entity,
            },
        )

    def set_room(
        self,
        area,
        name,
        timestamp,
        *,
        entry="entry_1",
        robots=("vacuum.alpha",),
        enabled=True,
        reason=None,
        state="Scheduled",
    ):
        identity = {"adaptive_robovacs_entry_id": entry, "area_id": area}
        self.hass.states.async_set(
            f"sensor.status_{area}_{entry}",
            state,
            {
                **identity,
                "adaptive_robovacs_role": "room_status",
                "room": name,
                "enabled": enabled,
                "robot_entity_ids": list(robots),
                "robot_previews": [
                    {
                        "robot_entity_id": robot,
                        "status": "blocked" if reason else "conditional",
                        "reason": reason,
                    }
                    for robot in robots
                ],
            },
        )
        self.hass.states.async_set(
            f"sensor.schedule_{area}_{entry}",
            timestamp,
            {
                **identity,
                "adaptive_robovacs_role": "room_schedule",
            },
        )

    def render(self, now=NOW, robot="vacuum.alpha"):
        with patch("homeassistant.util.dt.now", return_value=now):
            template = Template(
                "{% set robot_entity_id = " + json.dumps(robot) + " %}" + TEMPLATE,
                self.hass,
            )
            result = template.async_render_to_info().result()
            self.assertIsInstance(result, list)
            return result

    async def test_manual_clean_discovers_renamed_control_and_excludes_other_modes(
        self,
    ) -> None:
        self.set_robot()
        self.set_room("study", "Study", NOW.isoformat(), entry="entry_2")
        self.set_room("study", "Renamed room", NOW.isoformat())
        for suffix, role in [
            ("clean", "room_manual_clean_control"),
            ("vacuum", "room_manual_vacuum_control"),
            ("mop", "room_manual_mop_control"),
        ]:
            for entry in ("entry_1", "entry_2"):
                self.hass.states.async_set(
                    f"button.renamed_{suffix}_{entry}",
                    "unknown",
                    {
                        "adaptive_robovacs_entry_id": entry,
                        "adaptive_robovacs_role": role,
                        "area_id": "study",
                    },
                )
        await self.hass.async_block_till_done()
        self.events.clear()
        card = self.render()[0]
        buttons = card["sub_button"]["main"]
        self.assertEqual([b["name"] for b in buttons], ["Clean"])
        self.assertEqual(card["sub_button"]["bottom"], [])
        self.assertEqual(buttons[0]["icon"], "mdi:robot-vacuum")
        entity_id = "button.renamed_clean_entry_1"
        self.assertEqual(buttons[0]["entity"], entity_id)
        self.assertEqual(
            buttons[0]["tap_action"],
            {
                "action": "perform-action",
                "perform_action": "button.press",
                "target": {"entity_id": entity_id},
            },
        )
        self.assertEqual(card["button_action"]["tap_action"], {"action": "more-info"})
        await self.hass.async_block_till_done()
        self.assertEqual(self.events, [])
        clean = self.hass.states.get(entity_id)
        self.hass.states.async_set(entity_id, "unavailable", clean.attributes)
        self.assertEqual(self.render()[0]["sub_button"]["main"], [])
        self.hass.states.async_remove(entity_id)
        self.assertEqual(self.render()[0]["sub_button"]["main"], [])
        self.hass.states.async_set(entity_id, "unknown", clean.attributes)
        self.hass.states.async_set("button.duplicate", "unknown", clean.attributes)
        self.assertEqual(self.render()[0]["sub_button"]["main"], [])

    async def test_timestamp_repeated_writes_emit_no_countdown_state_changes(
        self,
    ) -> None:
        coordinator = _Coordinator()
        coordinator.hass = self.hass
        entity = _RoomScheduleSensor(coordinator, "study", "Study")
        entity.hass = self.hass
        entity.entity_id = "sensor.study_next_clean"
        entity.async_write_ha_state()
        await self.hass.async_block_till_done()
        first = self.hass.states.get(entity.entity_id)
        self.events.clear()
        for minutes in (1, 30, 120):
            room = coordinator.data.rooms[0]
            room.block_reason = f"diagnostic {minutes}"
            room.desired_window_start += timedelta(minutes=minutes)
            coordinator.data.scheduler = replace(
                coordinator.data.scheduler,
                last_evaluation_at=NOW + timedelta(minutes=minutes),
            )
            entity.async_write_ha_state()
        await self.hass.async_block_till_done()
        self.assertEqual(self.events, [])
        self.assertEqual(
            self.hass.states.get(entity.entity_id).last_changed, first.last_changed
        )
        coordinator.data.rooms[0].next_clean_at = None
        entity.async_write_ha_state()
        await self.hass.async_block_till_done()
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.hass.states.get(entity.entity_id).state, "unknown")

    async def test_template_join_sort_filter_and_safe_bubble_text(self) -> None:
        self.set_robot()
        self.set_room(
            "late", "Later", (NOW + timedelta(days=1, minutes=30)).isoformat()
        )
        self.set_room("b", "Beta", NOW.isoformat())
        self.set_room("a", "Alpha", NOW.isoformat())
        self.set_room(
            "blocked",
            'Room "name"',
            NOW.isoformat(),
            reason='Robot held: "check" ${test}',
        )
        self.set_room("disabled", "Disabled", NOW.isoformat(), enabled=False)
        self.set_room("other", "Other robot", NOW.isoformat(), robots=("vacuum.beta",))
        self.set_room("cross", "Other entry", NOW.isoformat(), entry="entry_2")
        self.set_room("missing", "Missing", "unavailable")
        cards = self.render()
        self.assertEqual(
            [card["name"] for card in cards],
            ["Alpha", "Beta", "Later", "Missing", 'Room "name"'],
        )
        self.assertIn('"Now, if unoccupied"', cards[0]["styles"])
        self.assertIn('"Tomorrow 12:30"', cards[2]["styles"])
        self.assertIn('"Schedule data unavailable"', cards[3]["styles"])
        self.assertIn(json.dumps('Robot held: "check" ${test}'), cards[4]["styles"])
        self.assertEqual(cards[0]["entity"], "sensor.status_a_entry_1")
        self.assertEqual(cards[0]["tap_action"], {"action": "more-info"})
        self.assertTrue(cards[0]["show_attribute"])
        self.assertIs(cards[0]["show_state"], False)
        self.assertIs(cards[0]["show_attribute"], True)
        self.assertNotIn("attribute", cards[0])

    async def test_empty_unavailable_and_midnight_refresh_without_entity_writes(
        self,
    ) -> None:
        self.assertEqual(self.render()[0]["content"], "Schedule data unavailable.")
        self.set_robot()
        self.assertEqual(
            self.render()[0]["content"], "No enabled rooms assigned to this robot."
        )
        self.set_room("future", "Future", (NOW + timedelta(days=1)).isoformat())
        await self.hass.async_block_till_done()
        self.events.clear()
        self.assertIn("Tomorrow 12:00", self.render()[0]["styles"])
        self.assertIn(
            "Today 12:00", self.render(NOW + timedelta(hours=12))[0]["styles"]
        )
        await self.hass.async_block_till_done()
        self.assertEqual(self.events, [])
        info = Template(
            "{% set robot_entity_id = 'vacuum.alpha' %}" + TEMPLATE, self.hass
        ).async_render_to_info()
        self.assertTrue(info.has_time)
        self.set_room("future", "Future", "unknown", state="unavailable")
        self.assertIn("Schedule data unavailable", self.render()[0]["styles"])
        self.hass.states.async_remove("sensor.status_future_entry_1")
        self.assertEqual(self.render()[0]["content"], "Schedule data unavailable.")
        self.set_robot(state="unavailable")
        self.assertEqual(self.render()[0]["content"], "Schedule data unavailable.")
