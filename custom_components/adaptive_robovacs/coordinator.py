"""Durable registry-driven scheduler for Adaptive RoboVacs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .const import (
    CONF_FORECAST_CONFIDENCE,
    CONF_HALL_END,
    CONF_HALL_START,
    CONF_OBSERVE_ONLY,
    DEFAULT_BEDROOM_INTERVAL,
    DEFAULT_COMMON_INTERVAL,
    DEFAULT_EXPECTED_MINUTES,
    DEFAULT_FORECAST_CONFIDENCE,
    DEFAULT_HALL_END,
    DEFAULT_HALL_START,
    DEFAULT_MINIMUM_BATTERY,
    DEFAULT_MOP_INTERVAL,
    DOMAIN,
    EVENT_EVALUATION,
    EXTRA_CLEAR_MINUTES,
    FALLBACK_SAMPLE_COUNT,
    HISTORY_DAYS,
    SIGNAL_DISCOVERY_UPDATED,
    STORAGE_KEY,
    VERSION,
)
from .discovery import DiscoveredRobot, DiscoveredRoom, DiscoveryResult, async_discover
from .models import Forecast, due_at, forecast_vacancy, in_daytime_window, manual_deferral, resolve_occupancy

_LOGGER = logging.getLogger(__name__)

type EntityListener = Callable[[], None]


def _now() -> datetime:
    return dt_util.utcnow()


def _local(value: datetime) -> datetime:
    return dt_util.as_local(value)


def _as_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_util.UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _blank_room() -> dict[str, Any]:
    return {
        "vacuum": None,
        "mop": None,
        "defer": {},
        "occupancy": "unresolved",
        "source": "unavailable",
        "unavailable_radars": 0,
        "unoccupied_since": None,
        "samples": [],
        "source_fingerprint": None,
        "map_status": "unknown",
        "map_error": None,
    }


def _blank_data(entry: ConfigEntry) -> dict[str, Any]:
    return {
        "version": VERSION,
        "observe_only": entry.data.get(CONF_OBSERVE_ONLY, True),
        "party_mode": False,
        "forecast_confidence": entry.data.get(
            CONF_FORECAST_CONFIDENCE, DEFAULT_FORECAST_CONFIDENCE
        ),
        "hall_start": entry.data.get(CONF_HALL_START, DEFAULT_HALL_START),
        "hall_end": entry.data.get(CONF_HALL_END, DEFAULT_HALL_END),
        "settings": {"robots": {}, "rooms": {}},
        "rooms": {},
        "active": {},
        "manual_events": [],
        "recovery_events": [],
        "last_evaluation": None,
        "last_preview": {},
        "legacy_migrated": False,
    }


class AdaptiveRoboVacCoordinator:
    """Own all scheduler state and dispatch safely through Home Assistant."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.store: Store[dict[str, Any]] = Store(hass, VERSION, f"{STORAGE_KEY}.{entry.entry_id}")
        self.data: dict[str, Any] = _blank_data(entry)
        self.discovery = DiscoveryResult()
        self._lock = asyncio.Lock()
        self._unsubscribers: list[Callable[[], None]] = []
        self._listeners: set[EntityListener] = set()
        self._watch_entity_ids: set[str] = set()

    async def async_initialize(self) -> None:
        """Restore state, discover the house, and begin passive observation."""

        stored = await self.store.async_load()
        if isinstance(stored, dict):
            self.data = _blank_data(self.entry) | stored
            self.data.setdefault("settings", {"robots": {}, "rooms": {}})
            self.data["settings"].setdefault("robots", {})
            self.data["settings"].setdefault("rooms", {})
            self.data.setdefault("rooms", {})
            self.data.setdefault("active", {})
            self.data.setdefault("manual_events", [])
            self.data.setdefault("recovery_events", [])

        await self.async_refresh_discovery()
        await self._async_migrate_legacy_once()
        await self._async_recover_active_jobs()
        self._unsubscribers.extend(
            [
                async_track_time_interval(self.hass, self._async_interval, timedelta(minutes=15)),
                self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._on_state_changed),
                self.hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STARTED, self._on_home_assistant_started
                ),
            ]
        )
        await self.async_evaluate(dry_run=True, reason="startup")

    async def async_shutdown(self) -> None:
        """Persist and remove event listeners."""

        await self._async_save()
        while self._unsubscribers:
            self._unsubscribers.pop()()

    def async_add_listener(self, listener: EntityListener) -> Callable[[], None]:
        """Register a platform entity update listener."""

        self._listeners.add(listener)

        @callback
        def unsubscribe() -> None:
            self._listeners.discard(listener)

        return unsubscribe

    @callback
    def _notify_listeners(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    async def _async_save(self) -> None:
        self.data["version"] = VERSION
        await self.store.async_save(self.data)

    async def async_refresh_discovery(self) -> None:
        """Refresh registry state and reset only changed room occupancy models."""

        prior_rooms = set(self.discovery.rooms)
        prior_robots = set(self.discovery.robots)
        self.discovery = await async_discover(self.hass)

        for room in self.discovery.rooms.values():
            detail = self._room_data(room.area_id)
            fingerprint = ",".join((*room.radar_entity_ids, "|", *room.fallback_entity_ids))
            if detail.get("source_fingerprint") not in {None, fingerprint}:
                detail["samples"] = []
                detail["unoccupied_since"] = None
                detail["occupancy"] = "unresolved"
                detail["source"] = "sources_changed"
            detail["source_fingerprint"] = fingerprint
            self._room_settings(room)

        for robot in self.discovery.robots.values():
            self._robot_settings(robot)
            self.data["active"].setdefault(robot.entity_id, None)

        self._watch_entity_ids = {
            robot.entity_id for robot in self.discovery.robots.values()
        }
        for room in self.discovery.rooms.values():
            self._watch_entity_ids.update(room.radar_entity_ids)
            self._watch_entity_ids.update(room.fallback_entity_ids)
        for robot in self.discovery.robots.values():
            if robot.profile.battery_entity_id:
                self._watch_entity_ids.add(robot.profile.battery_entity_id)

        if prior_rooms != set(self.discovery.rooms) or prior_robots != set(self.discovery.robots):
            async_dispatcher_send(self.hass, SIGNAL_DISCOVERY_UPDATED, self.entry.entry_id)
        self._notify_listeners()

    def _room_data(self, area_id: str) -> dict[str, Any]:
        return self.data["rooms"].setdefault(area_id, _blank_room())

    def _room_settings(self, room: DiscoveredRoom) -> dict[str, Any]:
        settings = self.data["settings"]["rooms"].setdefault(
            room.area_id,
            {
                "enabled": not room.is_bedroom,
                "vacuum_interval": (
                    DEFAULT_BEDROOM_INTERVAL if room.is_bedroom else DEFAULT_COMMON_INTERVAL
                ),
                "mop_interval": DEFAULT_MOP_INTERVAL,
                "expected_minutes": DEFAULT_EXPECTED_MINUTES,
            },
        )
        return settings

    def _robot_settings(self, robot: DiscoveredRobot) -> dict[str, Any]:
        return self.data["settings"]["robots"].setdefault(
            robot.entity_id,
            {
                "enabled": True,
                "minimum_battery": DEFAULT_MINIMUM_BATTERY,
                "mopping_enabled": robot.profile.supports_mopping,
                "double_pass": False,
                "mode": None,
                "mop_mode": None,
                "mop_intensity": None,
            },
        )

    @property
    def observe_only(self) -> bool:
        return bool(self.data.get("observe_only", True))

    @property
    def party_mode(self) -> bool:
        return bool(self.data.get("party_mode", False))

    async def async_set_global(self, key: str, value: Any) -> None:
        """Update a global control exposed by a native entity."""

        if key not in {"observe_only", "party_mode", "forecast_confidence", "hall_start", "hall_end"}:
            raise ValueError(f"Unknown global setting: {key}")
        self.data[key] = value
        await self._async_save()
        self._notify_listeners()
        await self.async_evaluate(dry_run=True, reason=f"global:{key}")

    async def async_set_room_setting(self, area_id: str, key: str, value: Any) -> None:
        """Update a discovered room's persistent scheduling setting."""

        if area_id not in self.discovery.rooms:
            raise ValueError(f"Unknown room area: {area_id}")
        if key not in {"enabled", "vacuum_interval", "mop_interval", "expected_minutes"}:
            raise ValueError(f"Unknown room setting: {key}")
        self._room_settings(self.discovery.rooms[area_id])[key] = value
        await self._async_save()
        self._notify_listeners()
        await self.async_evaluate(dry_run=True, reason=f"room:{area_id}:{key}")

    async def async_set_robot_setting(self, entity_id: str, key: str, value: Any) -> None:
        """Update a discovered robot's scheduling or compatibility setting."""

        if entity_id not in self.discovery.robots:
            raise ValueError(f"Unknown robot: {entity_id}")
        if key not in {
            "enabled",
            "minimum_battery",
            "mopping_enabled",
            "double_pass",
            "mode",
            "mop_mode",
            "mop_intensity",
        }:
            raise ValueError(f"Unknown robot setting: {key}")
        self._robot_settings(self.discovery.robots[entity_id])[key] = value
        await self._async_save()
        self._notify_listeners()
        await self.async_evaluate(dry_run=True, reason=f"robot:{entity_id}:{key}")

    async def _async_migrate_legacy_once(self) -> None:
        """Import matched legacy state without embedding legacy room identifiers."""

        if self.data.get("legacy_migrated"):
            return
        legacy = self.hass.states.get("pyscript.robovac_scheduler_store")
        legacy_data = legacy.attributes.get("data", {}) if legacy else {}
        legacy_rooms = legacy_data.get("rooms", {}) if isinstance(legacy_data, dict) else {}
        by_legacy_key = {
            slugify(room.name): room.area_id for room in self.discovery.rooms.values()
        }
        migrated = 0
        for legacy_key, legacy_room in legacy_rooms.items():
            area_id = by_legacy_key.get(slugify(str(legacy_key)))
            if not area_id or not isinstance(legacy_room, dict):
                continue
            target = self._room_data(area_id)
            for key in ("vacuum", "mop", "defer", "samples", "unoccupied_since"):
                if key in legacy_room:
                    target[key] = deepcopy(legacy_room[key])
            settings = self._room_settings(self.discovery.rooms[area_id])
            for suffix, setting_key in (
                ("enabled", "enabled"),
                ("vacuum_interval", "vacuum_interval"),
                ("mop_interval", "mop_interval"),
                ("expected_minutes", "expected_minutes"),
            ):
                helper = self.hass.states.get(
                    f"input_{'boolean' if suffix == 'enabled' else 'number'}.robovac_{legacy_key}_{suffix}"
                )
                if helper is None:
                    continue
                if suffix == "enabled":
                    settings[setting_key] = helper.state == "on"
                else:
                    try:
                        settings[setting_key] = float(helper.state)
                    except ValueError:
                        pass
            migrated += 1
        party_mode = self.hass.states.get("input_boolean.robovac_party_mode")
        if party_mode:
            self.data["party_mode"] = party_mode.state == "on"
        self.data["legacy_migrated"] = True
        self.data["legacy_migration_count"] = migrated
        await self._async_save()

    async def _async_recover_active_jobs(self) -> None:
        """Recover a persisted command checkpoint after a Home Assistant restart."""

        now = _now()
        for entity_id, active in list(self.data["active"].items()):
            if not active:
                continue
            state = self.hass.states.get(entity_id)
            if state and state.state in {"cleaning", "returning"}:
                active["recovered_at"] = _iso(now)
                continue
            self.data["active"][entity_id] = None
            self.data["recovery_events"].append(
                {"robot": entity_id, "at": _iso(now), "reason": "cleared stale checkpoint"}
            )
        self.data["recovery_events"] = self.data["recovery_events"][-20:]
        await self._async_save()

    @callback
    def _on_home_assistant_started(self, _event: Event) -> None:
        self.hass.async_create_task(self.async_evaluate(dry_run=True, reason="ha_started"))

    @callback
    def _on_state_changed(self, event: Event) -> None:
        entity_id = event.data.get("entity_id")
        if entity_id in self._watch_entity_ids:
            self.hass.async_create_task(self.async_evaluate(dry_run=False, reason=f"state:{entity_id}"))

    async def _async_interval(self, _now_value: datetime) -> None:
        await self.async_evaluate(dry_run=False, reason="interval")

    def _room_occupancy(self, room: DiscoveredRoom) -> tuple[str, str, int]:
        radars = [
            self.hass.states.get(entity_id).state if self.hass.states.get(entity_id) else None
            for entity_id in room.radar_entity_ids
        ]
        fallbacks = [
            self.hass.states.get(entity_id).state if self.hass.states.get(entity_id) else None
            for entity_id in room.fallback_entity_ids
        ]
        resolved = resolve_occupancy(radars, fallbacks)
        return resolved.state, resolved.source, resolved.unavailable_radars

    def _observe_occupancy(self, now: datetime) -> None:
        cutoff = now - timedelta(days=HISTORY_DAYS)
        for room in self.discovery.rooms.values():
            detail = self._room_data(room.area_id)
            state, source, unavailable = self._room_occupancy(room)
            prior = detail.get("occupancy", "unresolved")
            if prior == "unoccupied" and state != "unoccupied":
                started = _as_datetime(detail.get("unoccupied_since"))
                if started:
                    duration = int((now - started).total_seconds() / 60)
                    if duration > 0:
                        detail["samples"].append({"start": _iso(started), "minutes": duration})
                detail["unoccupied_since"] = None
            elif prior != "unoccupied" and state == "unoccupied":
                detail["unoccupied_since"] = _iso(now)
            detail["occupancy"] = state
            detail["source"] = source
            detail["unavailable_radars"] = unavailable
            detail["samples"] = [
                sample
                for sample in detail.get("samples", [])
                if (_as_datetime(sample.get("start")) or now) >= cutoff
            ]

    def _robot_battery(self, robot: DiscoveredRobot) -> float | None:
        entity_id = robot.profile.battery_entity_id
        state = self.hass.states.get(entity_id) if entity_id else None
        try:
            return float(state.state) if state else None
        except (TypeError, ValueError):
            return None

    def _robot_ready(self, robot: DiscoveredRobot) -> tuple[bool, str]:
        settings = self._robot_settings(robot)
        if not settings.get("enabled", True):
            return False, "robot disabled"
        if not robot.supports_area_clean:
            return False, "does not support Home Assistant area cleaning"
        state = self.hass.states.get(robot.entity_id)
        if not state or state.state not in {"docked", "idle"}:
            return False, f"robot is {state.state if state else 'unavailable'}"
        battery = self._robot_battery(robot)
        if battery is None:
            return False, "battery unavailable"
        if battery < float(settings.get("minimum_battery", DEFAULT_MINIMUM_BATTERY)):
            return False, "battery below minimum"
        if self.data["active"].get(robot.entity_id):
            return False, "active job"
        return True, "ready"

    def _mop_ready(self, robot: DiscoveredRobot) -> bool:
        """Mop only through a discovered profile and an enabled robot setting."""

        settings = self._robot_settings(robot)
        return robot.profile.supports_mopping and bool(settings.get("mopping_enabled"))

    def _room_due(
        self, room: DiscoveredRoom, operation: str, now: datetime
    ) -> datetime:
        detail = self._room_data(room.area_id)
        settings = self._room_settings(room)
        interval = settings["mop_interval"] if operation == "mop" else settings["vacuum_interval"]
        return due_at(
            _as_datetime(detail.get(operation)),
            float(interval),
            _as_datetime(detail.get("defer", {}).get(operation)),
            now,
        )

    def _forecast(self, room: DiscoveredRoom, now: datetime) -> Forecast:
        detail = self._room_data(room.area_id)
        if detail["source"] == "no_sensor":
            return Forecast(True, 1.0, "no-sensor policy")
        samples = []
        for sample in detail.get("samples", []):
            started = _as_datetime(sample.get("start"))
            if started:
                samples.append({"start": _local(started), "minutes": sample.get("minutes", 0)})
        return forecast_vacancy(
            samples,
            _local(now),
            _local(_as_datetime(detail["unoccupied_since"]))
            if _as_datetime(detail.get("unoccupied_since"))
            else None,
            int(self._room_settings(room)["expected_minutes"]) + EXTRA_CLEAR_MINUTES,
            float(self.data.get("forecast_confidence", DEFAULT_FORECAST_CONFIDENCE)),
            FALLBACK_SAMPLE_COUNT,
        )

    def _hall_allowed(self, now: datetime) -> tuple[bool, str]:
        if not in_daytime_window(
            _local(now),
            str(self.data.get("hall_start", DEFAULT_HALL_START)),
            str(self.data.get("hall_end", DEFAULT_HALL_END)),
        ):
            return False, "outside daytime window"
        blocked = [
            room.name
            for room in self.discovery.rooms.values()
            if room.is_bedroom and self._room_data(room.area_id).get("occupancy") != "unoccupied"
        ]
        return (False, "bedrooms not clear: " + ", ".join(blocked)) if blocked else (True, "bedrooms clear")

    def _room_candidate(self, room: DiscoveredRoom, now: datetime) -> tuple[dict[str, Any] | None, str]:
        settings = self._room_settings(room)
        detail = self._room_data(room.area_id)
        if not settings.get("enabled", True):
            return None, "room disabled"
        if detail.get("map_status") == "unmapped":
            return None, "unmapped; awaiting native map repair"
        vacuum_due = self._room_due(room, "vacuum", now)
        mop_due = self._room_due(room, "mop", now)
        capable = [
            robot
            for robot in self.discovery.robots.values()
            if robot.floor_id == room.floor_id and robot.supports_area_clean
        ]
        can_mop = any(self._mop_ready(robot) for robot in capable)
        if vacuum_due > now and (mop_due > now or not can_mop):
            return None, "not due"
        if detail.get("occupancy") != "unoccupied":
            return None, f"occupancy {detail.get('occupancy')} ({detail.get('source')})"
        if room.is_bedroom_transit:
            allowed, reason = self._hall_allowed(now)
            if not allowed:
                return None, reason
        forecast = self._forecast(room, now)
        if not forecast.allowed:
            return None, forecast.reason
        operation = "vacuum"
        due = vacuum_due
        if can_mop and mop_due <= now and vacuum_due <= now:
            operation = "vac_and_mop"
            due = min(vacuum_due, mop_due)
        elif can_mop and mop_due <= now:
            operation = "mop"
            due = mop_due
        return {
            "room": room,
            "operation": operation,
            "due_at": due,
            "confidence": forecast.confidence,
            "reason": forecast.reason,
        }, "ready"

    async def _async_apply_profile(self, robot: DiscoveredRobot, operation: str) -> None:
        """Set optional native controls discovered on the robot's own device."""

        profile = robot.profile
        settings = self._robot_settings(robot)
        selections = (
            (profile.mode_select_entity_id, settings.get("mode")),
            (profile.mop_mode_select_entity_id, settings.get("mop_mode")),
            (profile.mop_intensity_select_entity_id, settings.get("mop_intensity")),
        )
        for entity_id, option in selections:
            if entity_id and option:
                await self.hass.services.async_call(
                    "select", "select_option", {"entity_id": entity_id, "option": option}, blocking=True
                )
        if profile.passes_select_entity_id and settings.get("double_pass"):
            wanted = next(
                (
                    option
                    for option in profile.passes_options
                    if slugify(option) in {"two_pass", "double_pass"}
                ),
                None,
            )
            if wanted:
                await self.hass.services.async_call(
                    "select",
                    "select_option",
                    {"entity_id": profile.passes_select_entity_id, "option": wanted},
                    blocking=True,
                )

    async def _async_dispatch(
        self, robot: DiscoveredRobot, candidate: dict[str, Any], now: datetime
    ) -> tuple[bool, str]:
        room: DiscoveredRoom = candidate["room"]
        active = {
            "room": room.area_id,
            "operation": candidate["operation"],
            "started": _iso(now),
            "seen_cleaning": False,
            "phase": "dispatching",
        }
        self.data["active"][robot.entity_id] = active
        await self._async_save()
        try:
            await self._async_apply_profile(robot, candidate["operation"])
            await self.hass.services.async_call(
                "vacuum",
                "clean_area",
                {"entity_id": robot.entity_id, "cleaning_area_id": [room.area_id]},
                blocking=True,
            )
        except Exception as err:  # ServiceValidationError varies between HA versions.
            self.data["active"][robot.entity_id] = None
            detail = self._room_data(room.area_id)
            detail["map_status"] = "unmapped"
            detail["map_error"] = str(err)
            await self._async_save()
            return False, f"dispatch failed: {err}"
        active["phase"] = "accepted"
        active["accepted_at"] = _iso(_now())
        self._room_data(room.area_id)["map_status"] = "mapped"
        self._room_data(room.area_id)["map_error"] = None
        await self._async_save()
        return True, f"dispatched {room.name}"

    async def _async_reconcile_jobs(self, now: datetime) -> None:
        """Persist completion only after an accepted command has actually cleaned."""

        changed = False
        for robot_id, active in list(self.data["active"].items()):
            if not active:
                continue
            state = self.hass.states.get(robot_id)
            state_text = state.state if state else "unavailable"
            if state_text == "cleaning":
                active["seen_cleaning"] = True
                active["phase"] = "cleaning"
                changed = True
                continue
            if active.get("seen_cleaning") and state_text in {"docked", "idle"}:
                detail = self._room_data(active["room"])
                operation = active["operation"]
                if operation in {"vacuum", "vac_and_mop"}:
                    detail["vacuum"] = _iso(now)
                if operation in {"mop", "vac_and_mop"}:
                    detail["mop"] = _iso(now)
                self.data["active"][robot_id] = None
                changed = True
                continue
            started = _as_datetime(active.get("started"))
            if (
                not active.get("seen_cleaning")
                and started
                and now - started > timedelta(minutes=10)
            ):
                detail = self._room_data(active["room"])
                detail["map_status"] = "unmapped"
                detail["map_error"] = "vacuum did not enter cleaning after command"
                self.data["active"][robot_id] = None
                changed = True
        if changed:
            await self._async_save()

    async def async_evaluate(self, dry_run: bool = False, reason: str = "manual") -> dict[str, Any]:
        """Refresh state, publish a safe preview, and optionally dispatch work."""

        async with self._lock:
            now = _now()
            await self.async_refresh_discovery()
            self._observe_occupancy(now)
            await self._async_reconcile_jobs(now)
            candidates: list[dict[str, Any]] = []
            reasons: dict[str, str] = {}
            for room in self.discovery.rooms.values():
                candidate, block_reason = self._room_candidate(room, now)
                if candidate:
                    candidates.append(candidate)
                else:
                    reasons[room.area_id] = block_reason
            candidates.sort(
                key=lambda candidate: (
                    (now - candidate["due_at"]).total_seconds(),
                    candidate["confidence"],
                ),
                reverse=True,
            )

            robot_ready = {
                robot.entity_id: self._robot_ready(robot)
                for robot in self.discovery.robots.values()
            }
            assignments: list[tuple[DiscoveredRobot, dict[str, Any]]] = []
            used_robots: set[str] = set()
            for candidate in candidates:
                room: DiscoveredRoom = candidate["room"]
                eligible = [
                    robot
                    for robot in self.discovery.robots.values()
                    if robot.floor_id == room.floor_id
                    and robot.entity_id not in used_robots
                    and robot_ready[robot.entity_id][0]
                    and (candidate["operation"] not in {"mop", "vac_and_mop"} or self._mop_ready(robot))
                ]
                if not eligible:
                    reasons.setdefault(room.area_id, "no ready compatible robot")
                    continue
                eligible.sort(
                    key=lambda robot: (
                        self._robot_battery(robot) or 0,
                        robot.entity_id,
                    ),
                    reverse=True,
                )
                robot = eligible[0]
                assignments.append((robot, candidate))
                used_robots.add(robot.entity_id)

            preview = {
                "at": _iso(now),
                "reason": reason,
                "observe_only": self.observe_only,
                "party_mode": self.party_mode,
                "candidates": [
                    {
                        "room": item["room"].area_id,
                        "room_name": item["room"].name,
                        "operation": item["operation"],
                        "due_at": _iso(item["due_at"]),
                        "confidence": item["confidence"],
                        "basis": item["reason"],
                    }
                    for item in candidates
                ],
                "assignments": [
                    {"robot": robot.entity_id, "room": item["room"].area_id}
                    for robot, item in assignments
                ],
                "blocks": reasons,
                "robots": {
                    robot_id: {"ready": ready, "reason": ready_reason}
                    for robot_id, (ready, ready_reason) in robot_ready.items()
                },
            }
            self.data["last_evaluation"] = _iso(now)
            self.data["last_preview"] = preview
            await self._async_save()

            dispatches: list[str] = []
            if not dry_run and not self.observe_only and not self.party_mode:
                for robot, candidate in assignments:
                    ok, message = await self._async_dispatch(robot, candidate, now)
                    dispatches.append(message)
                    if not ok:
                        _LOGGER.warning("Adaptive RoboVacs: %s", message)
            elif self.observe_only:
                dispatches.append("observe-only mode")
            elif self.party_mode:
                dispatches.append("party mode")
            self._notify_listeners()
            self.hass.bus.async_fire(EVENT_EVALUATION, {"entry_id": self.entry.entry_id, **preview})
            return {**preview, "dispatches": dispatches}

    async def async_record_manual_clean(
        self, robot_entity_id: str, area_ids: list[str], operations: list[str]
    ) -> dict[str, Any]:
        """Apply one-day deferrals only to known rooms due within 24 hours."""

        now = _now()
        changed: list[str] = []
        for area_id in area_ids:
            room = self.discovery.rooms.get(area_id)
            if not room:
                continue
            if robot_entity_id not in {
                robot.entity_id
                for robot in self.discovery.robots.values()
                if robot.floor_id == room.floor_id
            }:
                continue
            detail = self._room_data(area_id)
            for operation in operations:
                if operation not in {"vacuum", "mop"}:
                    continue
                next_due = self._room_due(room, operation, now)
                deferred = manual_deferral(now, next_due)
                if deferred:
                    detail.setdefault("defer", {})[operation] = _iso(deferred)
                    changed.append(f"{area_id}:{operation}")
        self.data["manual_events"].append(
            {"at": _iso(now), "robot": robot_entity_id, "rooms": area_ids, "operations": operations, "changed": changed}
        )
        self.data["manual_events"] = self.data["manual_events"][-50:]
        await self._async_save()
        self._notify_listeners()
        return {"changed": changed}

    def decommission_inventory(self) -> dict[str, Any]:
        """Report legacy-owned objects; this method never removes them."""

        legacy_entities = sorted(
            state.entity_id
            for state in self.hass.states.async_all()
            if state.entity_id.startswith("pyscript.robovac_")
            or state.entity_id.startswith("input_boolean.robovac_")
            or state.entity_id.startswith("input_number.robovac_")
            or state.entity_id.startswith("input_select.robovac_")
            or state.entity_id.startswith("input_datetime.robovac_")
        )
        references = []
        for state in self.hass.states.async_all("automation") + self.hass.states.async_all("script"):
            if "robovac_scheduler" in str(state.attributes):
                references.append(state.entity_id)
        return {
            "legacy_entities": legacy_entities,
            "external_references": sorted(references),
            "safe_to_remove": False,
            "message": "Inventory only. Legacy removal requires explicit user sign-off.",
        }

    def room_state(self, area_id: str) -> dict[str, Any]:
        """Return card-friendly state for a discovered area."""

        room = self.discovery.rooms[area_id]
        detail = self._room_data(area_id)
        settings = self._room_settings(room)
        now = _now()
        vacuum_due = self._room_due(room, "vacuum", now)
        mop_due = self._room_due(room, "mop", now)
        candidate, reason = self._room_candidate(room, now)
        return {
            "name": room.name,
            "area_id": room.area_id,
            "floor_id": room.floor_id,
            "bedroom": room.is_bedroom,
            "bedroom_transit": room.is_bedroom_transit,
            "radars": room.radar_entity_ids,
            "fallbacks": room.fallback_entity_ids,
            "enabled": settings["enabled"],
            "vacuum_interval": settings["vacuum_interval"],
            "mop_interval": settings["mop_interval"],
            "expected_minutes": settings["expected_minutes"],
            "occupancy": detail["occupancy"],
            "occupancy_source": detail["source"],
            "unavailable_radars": detail["unavailable_radars"],
            "last_cleaned": max(filter(None, [_as_datetime(detail.get("vacuum")), _as_datetime(detail.get("mop"))]), default=None),
            "last_vacuum": _as_datetime(detail.get("vacuum")),
            "last_mop": _as_datetime(detail.get("mop")),
            "vacuum_due": vacuum_due,
            "mop_due": mop_due,
            "next_candidate": candidate,
            "block_reason": reason,
            "map_status": detail.get("map_status", "unknown"),
            "map_error": detail.get("map_error"),
        }

    def robot_state(self, entity_id: str) -> dict[str, Any]:
        """Return card-friendly state for a discovered vacuum."""

        robot = self.discovery.robots[entity_id]
        state = self.hass.states.get(entity_id)
        ready, reason = self._robot_ready(robot)
        active = self.data["active"].get(entity_id)
        active_room = self.discovery.rooms.get(active["room"]) if active else None
        return {
            "name": robot.name,
            "entity_id": entity_id,
            "floor_id": robot.floor_id,
            "state": state.state if state else "unavailable",
            "battery": self._robot_battery(robot),
            "ready": ready,
            "reason": reason,
            "active": active,
            "active_room": active_room.name if active_room else None,
            "profile": robot.profile,
            "settings": self._robot_settings(robot),
        }
