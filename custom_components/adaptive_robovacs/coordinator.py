"""Durable registry-driven scheduler for Adaptive RoboVacs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_CALL_SERVICE, EVENT_HOMEASSISTANT_STARTED, EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .const import (
    CONF_FORECAST_CONFIDENCE,
    CONF_HALL_END,
    CONF_HALL_START,
    CONF_OBSERVE_ONLY,
    CONF_UNRESOLVED_END,
    CONF_UNRESOLVED_START,
    DEFAULT_BEDROOM_INTERVAL,
    DEFAULT_COMMON_INTERVAL,
    DEFAULT_EXPECTED_MINUTES,
    DEFAULT_FORECAST_CONFIDENCE,
    DEFAULT_HALL_END,
    DEFAULT_HALL_START,
    DEFAULT_MINIMUM_BATTERY,
    DEFAULT_MOP_INTERVAL,
    DEFAULT_UNRESOLVED_END,
    DEFAULT_UNRESOLVED_START,
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
from .models import (
    Forecast,
    desired_window_allows,
    due_at,
    forecast_vacancy,
    held_job_transition,
    in_daytime_window,
    learned_duration_minutes,
    manual_deferral,
    next_window_start,
    parse_manual_clean_request,
    offline_held_recovery_outcome,
    rebase_due_times,
    recovery_transition_is_observed,
    resolve_occupancy,
    select_operation,
    unresolved_occupancy_allowed,
)

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
        "duration_samples": [],
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
        "unresolved_start": entry.data.get(CONF_UNRESOLVED_START, DEFAULT_UNRESOLVED_START),
        "unresolved_end": entry.data.get(CONF_UNRESOLVED_END, DEFAULT_UNRESOLVED_END),
        "settings": {"robots": {}, "rooms": {}},
        "rooms": {},
        "active": {},
        "robot_holds": {},
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
        self._recovery_timers: dict[str, Callable[[], None]] = {}

    async def async_initialize(self) -> None:
        """Restore state, discover the house, and begin passive observation."""

        stored = await self.store.async_load()
        if isinstance(stored, dict):
            self.data = _blank_data(self.entry) | stored
            self.data.setdefault("settings", {"robots": {}, "rooms": {}})
            self.data["settings"].setdefault("robots", {})
            self.data["settings"].setdefault("rooms", {})
            self.data.setdefault("robot_holds", {})
            self.data.setdefault("rooms", {})
            self.data.setdefault("active", {})
            self.data.setdefault("manual_events", [])
            self.data.setdefault("recovery_events", [])

        await self.async_refresh_discovery()
        self._remove_deprecated_confirmation_buttons()
        await self._async_migrate_legacy_dispatch_errors()
        await self._async_migrate_legacy_once()
        await self._async_recover_active_jobs()
        self._unsubscribers.extend(
            [
                async_track_time_interval(self.hass, self._async_interval, timedelta(minutes=15)),
                self.hass.bus.async_listen(EVENT_CALL_SERVICE, self._on_call_service),
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
        while self._recovery_timers:
            self._recovery_timers.popitem()[1]()
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

    def _remove_deprecated_confirmation_buttons(self) -> None:
        """Remove v1.0.9's acknowledgement buttons from the entity registry."""

        registry = er.async_get(self.hass)
        for registry_entry in tuple(registry.entities.values()):
            if (
                registry_entry.domain == "button"
                and registry_entry.platform == DOMAIN
                and registry_entry.config_entry_id == self.entry.entry_id
                and registry_entry.unique_id.endswith("_confirm_held_clean_cancelled")
            ):
                registry.async_remove(registry_entry.entity_id)

    async def _async_migrate_legacy_dispatch_errors(self) -> None:
        """Make legacy dispatch failures safe and clear to dashboard users.

        A dispatch failure does not prove that a vacuum's native map is
        broken.  Earlier releases persisted both the raw failure and an
        ``unmapped`` status, which led the dashboard to direct users to repair
        a map without evidence.  Keep the diagnostic in the Home Assistant
        integration log and expose only a generic, actionable status instead.
        """

        changed = False
        for area_id, detail in self.data["rooms"].items():
            raw_error = detail.get("map_error")
            if raw_error == "required key not provided @ data['cleaning_area_id']":
                # This old payload defect is known to be corrected by the
                # current service call, so allow the scheduler to retry it.
                detail["map_status"] = "unknown"
                detail["map_error"] = None
                changed = True
                continue
            if detail.get("map_status") != "unmapped":
                continue
            _LOGGER.error(
                "Migrating persisted Adaptive RoboVacs dispatch failure to an "
                "unknown error: area_id=%s diagnostic=%r",
                area_id,
                raw_error,
            )
            detail["map_status"] = "error"
            detail["map_error"] = "unknown dispatch error"
            changed = True
        if changed:
            await self._async_save()

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
            if robot.profile.cleaning_time_entity_id:
                self._watch_entity_ids.add(robot.profile.cleaning_time_entity_id)

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
                "carpet": False,
                "ignore_desired_window": False,
            },
        )
        # Existing persisted settings predate newer optional room controls.
        settings.setdefault("carpet", False)
        settings.setdefault("ignore_desired_window", False)
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

        if key not in {
            "observe_only",
            "party_mode",
            "forecast_confidence",
            "hall_start",
            "hall_end",
            "unresolved_start",
            "unresolved_end",
        }:
            raise ValueError(f"Unknown global setting: {key}")
        self.data[key] = value
        await self._async_save()
        self._notify_listeners()
        await self.async_evaluate(dry_run=True, reason=f"global:{key}")

    async def async_set_room_setting(self, area_id: str, key: str, value: Any) -> None:
        """Update a discovered room's persistent scheduling setting."""

        if area_id not in self.discovery.rooms:
            raise ValueError(f"Unknown room area: {area_id}")
        if key not in {
            "enabled",
            "vacuum_interval",
            "mop_interval",
            "expected_minutes",
            "carpet",
            "ignore_desired_window",
        }:
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
        robot_ids = set(self.discovery.robots) | set(self.data["active"]) | set(self.data["robot_holds"])
        for entity_id in robot_ids:
            active = self.data["active"].get(entity_id)
            tracked_expected_minutes: float | None = None
            if active and active.get("expected_minutes") is not None:
                try:
                    tracked_expected_minutes = float(active["expected_minutes"])
                except (TypeError, ValueError):
                    pass
            if active:
                self._normalise_active_job(active, now)
            state = self.hass.states.get(entity_id)
            state_text = state.state if state else "unavailable"
            hold = self.data["robot_holds"].get(entity_id)

            # Retain v1.0.9 holds written before their richer state was added.
            if not hold and active and active.get("phase") in {"paused", "error_waiting"}:
                hold = {
                    "reason": active.get("hold_reason", "paused"),
                    "phase": "held",
                    "held_at": active.get("held_at") or _iso(now),
                    "last_observed_at": active.get("last_observed_at") or _iso(now),
                }
                self.data["robot_holds"][entity_id] = hold

            action = self._reconcile_robot_hold(entity_id, state_text, active, now)
            if action is not None:
                if action == "held":
                    if active:
                        self._hold_active_job(entity_id, active, state_text, now)
                    continue
                if action == "resumed":
                    if active:
                        self._resume_held_job(entity_id, active, state, now)
                    continue
                if action in {"cancelling", "completion_pending"}:
                    if active:
                        self._set_held_job_phase(entity_id, active, action, now)
                    continue
                if action == "cancelled":
                    cancelled_at = _as_datetime(
                        self.data["robot_holds"].get(entity_id, {}).get("returning_at")
                    ) or now
                    if active:
                        self._cancel_job(entity_id, active, cancelled_at, "physical_cancelled")
                    self._apply_robot_cancellation_deferral(entity_id, cancelled_at)
                    self.data["robot_holds"].pop(entity_id, None)
                    continue
                if action == "complete":
                    if active:
                        completion = _as_datetime(active.get("cleaning_finished")) or now
                        self._complete_job(entity_id, active, completion, "observed")
                    self.data["robot_holds"].pop(entity_id, None)
                    continue
                outcome = offline_held_recovery_outcome(
                    state_text,
                    str(hold.get("phase")),
                    _as_datetime(active.get("last_observed_at")) if active else _as_datetime(hold.get("last_observed_at")),
                    tracked_expected_minutes,
                    now,
                )
                if outcome == "complete" and active:
                    expected_end = _as_datetime(active.get("expected_end"))
                    if expected_end:
                        self._complete_job(entity_id, active, expected_end, "recovered_expected_end")
                        self.data["robot_holds"].pop(entity_id, None)
                elif outcome == "cancelled":
                    if active:
                        self._cancel_job(entity_id, active, now, "recovered_physical_cancellation")
                    self._apply_robot_cancellation_deferral(entity_id, now)
                    self.data["robot_holds"].pop(entity_id, None)
                continue

            if not active:
                continue
            if state and state.state in {"cleaning", "returning"}:
                active["recovered_at"] = _iso(now)
                if state.state == "returning":
                    # Returning is reliable evidence that an accepted room command did run,
                    # even if Home Assistant was unavailable for the cleaning transition.
                    active["seen_cleaning"] = True
                    expected_end = _as_datetime(active.get("expected_end"))
                    if expected_end and now >= expected_end:
                        active["cleaning_finished"] = _iso(expected_end)
                        active["completion_confidence"] = "recovered_expected_end"
                        active["phase"] = "returning"
                    else:
                        self._set_recovery_waiting(entity_id, active, now)
                else:
                    active["phase"] = "cleaning"
                    self._cancel_recovery_timer(entity_id)
                continue
            expected_end = _as_datetime(active.get("expected_end"))
            if active.get("seen_cleaning") and state and state.state in {"docked", "idle"}:
                if expected_end and now >= expected_end:
                    self._complete_job(entity_id, active, expected_end, "recovered_expected_end")
                else:
                    self._set_recovery_waiting(entity_id, active, now)
                continue
            if state is None or state.state in {"unavailable", "unknown"}:
                self._set_recovery_waiting(entity_id, active, now)
                continue
            self.data["active"][entity_id] = None
            self._cancel_recovery_timer(entity_id)
            self.data["recovery_events"].append({"robot": entity_id, "at": _iso(now), "reason": "unconfirmed checkpoint"})
        self.data["recovery_events"] = self.data["recovery_events"][-20:]
        await self._async_save()

    def _normalise_active_job(self, active: dict[str, Any], now: datetime) -> None:
        """Backfill lifecycle fields for checkpoints written by older releases."""

        area_ids = self._active_rooms(active)
        if area_ids:
            active["room"] = area_ids[0]
            active.setdefault("rooms", area_ids)
        rooms = [self.discovery.rooms[area_id] for area_id in area_ids if area_id in self.discovery.rooms]
        if active.get("source") == "manual_home_assistant":
            fallback = sum(float(self._room_settings(room)["expected_minutes"]) for room in rooms) or DEFAULT_EXPECTED_MINUTES
        else:
            fallback = float(self._room_settings(rooms[0])["expected_minutes"]) if rooms else DEFAULT_EXPECTED_MINUTES
        active.setdefault("source", "scheduler")
        active.setdefault("passes", 1)
        active.setdefault("expected_minutes", fallback)
        active.setdefault(
            "last_observed_at",
            active.get("observed_started") or active.get("accepted_at") or active.get("started") or _iso(now),
        )
        if not active.get("expected_end"):
            started = _as_datetime(active.get("observed_started") or active.get("accepted_at") or active.get("started")) or now
            active["expected_end"] = _iso(started + timedelta(minutes=float(active["expected_minutes"])))

    def _reconcile_robot_hold(
        self, robot_id: str, state_text: str, active: dict[str, Any] | None, now: datetime
    ) -> str | None:
        """Keep observed pauses/errors durable and classify physical follow-up only."""

        hold = self.data["robot_holds"].get(robot_id)
        if state_text in {"paused", "error"}:
            reason = (
                "robot_error"
                if state_text == "error" or (hold and hold.get("reason") == "robot_error")
                else "paused"
            )
            if not hold:
                hold = {"reason": reason, "phase": "held", "held_at": _iso(now)}
                self.data["robot_holds"][robot_id] = hold
                if reason == "robot_error":
                    _LOGGER.error(
                        "Adaptive RoboVacs scheduler held after robot error: robot=%s state=%s. "
                        "The robot must physically resume or return to its dock before scheduling can continue.",
                        robot_id,
                        state_text,
                    )
                else:
                    _LOGGER.info(
                        "Adaptive RoboVacs scheduler held while robot is paused: robot=%s", robot_id
                    )
            else:
                hold["reason"] = reason
                hold.setdefault("phase", "held")
            hold["last_observed_at"] = _iso(now)
            return "held"
        if not hold:
            return None

        hold.setdefault("phase", "held")
        if state_text not in {"unavailable", "unknown"}:
            hold["last_observed_at"] = _iso(now)
        action = held_job_transition(
            state_text,
            str(hold.get("phase", "held")),
            bool(active and active.get("completion_before_hold")),
        )
        if action == "resumed":
            self.data["robot_holds"].pop(robot_id, None)
            _LOGGER.info(
                "Adaptive RoboVacs scheduler hold released by observed physical resume: robot=%s", robot_id
            )
        elif action in {"cancelling", "completion_pending"}:
            hold["phase"] = action
            hold["returning_at"] = _iso(now)
        return action

    def _hold_active_job(
        self, robot_id: str, active: dict[str, Any], state_text: str, now: datetime
    ) -> bool:
        """Persist an interrupted job so an automatic idle state cannot complete it."""

        hold = self.data["robot_holds"].get(robot_id, {})
        is_error = hold.get("reason") == "robot_error" or state_text == "error"
        phase = "completion_held" if active.get("cleaning_finished") else (
            "error_waiting" if is_error else "paused"
        )
        changed = active.get("phase") != phase
        active["phase"] = phase
        active["hold_reason"] = "robot_error" if is_error else "paused"
        active.setdefault("held_at", _iso(now))
        active["interrupted"] = True
        if active.get("cleaning_finished"):
            active["completion_before_hold"] = True
        self._cancel_recovery_timer(robot_id)
        if changed:
            _LOGGER.info(
                "Adaptive RoboVacs active room job held: robot=%s rooms=%s reason=%s",
                robot_id,
                self._active_rooms(active),
                active["hold_reason"],
            )
        return changed

    def _set_held_job_phase(
        self, robot_id: str, active: dict[str, Any], action: str, now: datetime
    ) -> None:
        """Expose a held job's physical-return state without releasing it."""

        if action == "cancelling":
            active["phase"] = "cancelling"
            active["interrupted"] = True
            active["hold_reason"] = "physical_cancellation"
            active["cancelling_at"] = _iso(now)
        else:
            active["phase"] = "completion_held"
            active["hold_reason"] = "completion_before_fault"
        self._cancel_recovery_timer(robot_id)

    def _resume_held_job(
        self, robot_id: str, active: dict[str, Any], state: Any, now: datetime
    ) -> None:
        """Continue a held job only after the robot itself resumes cleaning."""

        active.pop("hold_reason", None)
        active.pop("held_at", None)
        active["last_observed_at"] = _iso(now)
        if not active.get("seen_cleaning"):
            observed_start = state.last_changed if state else now
            active["observed_started"] = _iso(observed_start)
            active["expected_end"] = _iso(
                observed_start + timedelta(minutes=float(active.get("expected_minutes", 0)))
            )
            timer = self._cleaning_timer_minutes(robot_id)
            if timer is not None and timer <= 1:
                active["timer_start"] = timer
        active["seen_cleaning"] = True
        active["phase"] = "cleaning"
        self._cancel_recovery_timer(robot_id)

    def _apply_robot_cancellation_deferral(self, robot_id: str, cancelled_at: datetime) -> list[str]:
        """Rebase every enabled schedule on a physically cancelled robot's floor."""

        robot = self.discovery.robots.get(robot_id)
        if not robot or not robot.floor_id:
            _LOGGER.warning(
                "Adaptive RoboVacs cannot defer a cancelled robot floor: robot=%s floor=%s",
                robot_id,
                robot.floor_id if robot else None,
            )
            return []
        floor_robots = [
            candidate
            for candidate in self.discovery.robots.values()
            if candidate.floor_id == robot.floor_id
            and candidate.supports_area_clean
            and self._robot_settings(candidate).get("enabled", True)
        ]
        mop_eligible = any(self._mop_ready(candidate) for candidate in floor_robots)
        due_times: dict[str, datetime] = {}
        for room in self.discovery.rooms.values():
            if room.floor_id != robot.floor_id or not self._room_settings(room).get("enabled", True):
                continue
            due_times[f"{room.area_id}:vacuum"] = self._room_due(room, "vacuum", cancelled_at)
            if mop_eligible and not self._room_settings(room).get("carpet", False):
                due_times[f"{room.area_id}:mop"] = self._room_due(room, "mop", cancelled_at)

        rebased = rebase_due_times(due_times, cancelled_at + timedelta(hours=24))
        for key, deferred_until in rebased.items():
            area_id, operation = key.rsplit(":", maxsplit=1)
            self._room_data(area_id).setdefault("defer", {})[operation] = _iso(deferred_until)
        if rebased:
            _LOGGER.info(
                "Adaptive RoboVacs rebased cancelled floor queue: robot=%s floor=%s entries=%s until=%s",
                robot_id,
                robot.floor_id,
                len(rebased),
                _iso(cancelled_at + timedelta(hours=24)),
            )
        return list(rebased)

    def _active_rooms(self, active: dict[str, Any]) -> list[str]:
        """Return a job's tracked rooms, retaining compatibility with v1.0.x."""

        values = active.get("rooms") or [active.get("room")]
        if not isinstance(values, list):
            values = [values]
        return list(dict.fromkeys(value for value in values if isinstance(value, str)))

    def _set_recovery_waiting(
        self, robot_id: str, active: dict[str, Any], recovered_at: datetime
    ) -> None:
        """Keep an offline completion pending until a live transition or saved end."""

        active["phase"] = "recovery_waiting"
        active["recovered_at"] = _iso(recovered_at)
        expected_end = _as_datetime(active.get("expected_end"))
        if expected_end:
            self._schedule_recovery_completion(robot_id, expected_end)

    def _cancel_recovery_timer(self, robot_id: str) -> None:
        """Remove an exact expected-end callback when live state supersedes it."""

        unsubscribe = self._recovery_timers.pop(robot_id, None)
        if unsubscribe:
            unsubscribe()

    def _schedule_recovery_completion(self, robot_id: str, expected_end: datetime) -> None:
        """Reconcile an unobserved completion exactly at its persisted end time."""

        self._cancel_recovery_timer(robot_id)
        if expected_end <= _now():
            self.hass.async_create_task(
                self.async_evaluate(dry_run=False, reason=f"recovery-end:{robot_id}")
            )
            return

        @callback
        def reconcile_at_expected_end(_when: datetime) -> None:
            self._recovery_timers.pop(robot_id, None)
            self.hass.async_create_task(
                self.async_evaluate(dry_run=False, reason=f"recovery-end:{robot_id}")
            )

        self._recovery_timers[robot_id] = async_track_point_in_utc_time(
            self.hass, reconcile_at_expected_end, expected_end
        )

    @callback
    def _on_home_assistant_started(self, _event: Event) -> None:
        self.hass.async_create_task(self.async_evaluate(dry_run=True, reason="ha_started"))

    @callback
    def _on_call_service(self, event: Event) -> None:
        """Capture explicit user room-clean service calls."""

        if event.data.get("domain") != "vacuum":
            return
        if event.data.get("service") == "clean_area":
            self.hass.async_create_task(self._async_track_manual_service_call(event))

    async def _async_track_manual_service_call(self, event: Event) -> None:
        """Persist a manual checkpoint before the vacuum service begins work."""

        request = parse_manual_clean_request(
            str(event.data.get("domain")),
            str(event.data.get("service")),
            event.context.user_id,
            event.data.get("service_data", {}),
            self.discovery.robots,
            self.discovery.rooms,
        )
        if request is None:
            return

        async with self._lock:
            robot = self.discovery.robots.get(request.robot_id)
            rooms = [self.discovery.rooms.get(area_id) for area_id in request.area_ids]
            if robot is None or any(room is None for room in rooms):
                return
            existing = self.data["active"].get(request.robot_id)
            if existing:
                reason = (
                    "scheduler job already active"
                    if existing.get("source") == "scheduler"
                    else "manual job already active"
                )
                self._record_manual_event(
                    {
                        "at": _iso(_now()),
                        "robot": request.robot_id,
                        "rooms": request.area_ids,
                        "context_id": event.context.id,
                        "outcome": "ignored",
                        "reason": reason,
                    }
                )
                await self._async_save()
                return

            expected_minutes = sum(
                self._effective_duration(room, "vacuum", 1, request.robot_id)[0]
                for room in rooms
                if room is not None
            )
            now = _now()
            self.data["active"][request.robot_id] = {
                "room": request.area_ids[0],
                "rooms": request.area_ids,
                "operation": "vacuum",
                "requested_operations": ["vacuum"],
                "started": _iso(now),
                "seen_cleaning": False,
                "phase": "manual_requested",
                "source": "manual_home_assistant",
                "manual_context_id": event.context.id,
                "expected_minutes": expected_minutes,
                "expected_end": _iso(now + timedelta(minutes=expected_minutes)),
                "last_observed_at": _iso(now),
                "passes": 1,
            }
            self._record_manual_event(
                {
                    "at": _iso(now),
                    "robot": request.robot_id,
                    "rooms": request.area_ids,
                    "operations": ["vacuum"],
                    "context_id": event.context.id,
                    "outcome": "requested",
                }
            )
            await self._async_save()
            self._notify_listeners()
            self.hass.async_create_task(
                self.async_evaluate(dry_run=True, reason=f"manual-ha:{request.robot_id}")
            )

    @callback
    def _on_state_changed(self, event: Event) -> None:
        entity_id = event.data.get("entity_id")
        if entity_id in self._watch_entity_ids:
            old_state = event.data.get("old_state")
            new_state = event.data.get("new_state")
            transition = (
                {
                    "robot": entity_id,
                    "from": old_state.state if old_state else None,
                    "to": new_state.state if new_state else None,
                    "at": _iso(new_state.last_changed) if new_state else None,
                }
                if entity_id in self.discovery.robots
                else None
            )
            self.hass.async_create_task(
                self.async_evaluate(
                    dry_run=False, reason=f"state:{entity_id}", transition=transition
                )
            )

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
        hold = self.data["robot_holds"].get(robot.entity_id)
        if hold:
            if hold.get("phase") == "cancelling":
                return False, "held clean returning to dock"
            if hold.get("phase") == "completion_pending":
                return False, "held clean awaiting physical completion"
            if hold.get("reason") == "robot_error":
                return False, "scheduler held after robot error"
            return False, "scheduler held while robot is paused"
        active = self.data["active"].get(robot.entity_id)
        if active:
            if active.get("phase") == "cancelling":
                return False, "active clean returning to dock"
            if active.get("phase") == "completion_held":
                return False, "active clean held after completion"
            if active.get("phase") == "error_waiting":
                return False, "active job held after robot error"
            if active.get("phase") == "paused":
                return False, "active job held while robot is paused"
            return False, "active job"
        state = self.hass.states.get(robot.entity_id)
        if not state or state.state not in {"docked", "idle"}:
            return False, f"robot is {state.state if state else 'unavailable'}"
        battery = self._robot_battery(robot)
        if battery is None:
            return False, "battery unavailable"
        if battery < float(settings.get("minimum_battery", DEFAULT_MINIMUM_BATTERY)):
            return False, "battery below minimum"
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

    def _effective_duration(
        self, room: DiscoveredRoom, operation: str, passes: int, robot_id: str | None = None
    ) -> tuple[float, int]:
        detail = self._room_data(room.area_id)
        samples: list[float] = []
        for sample in detail.get("duration_samples", []):
            if sample.get("operation") != operation or sample.get("source") not in {"robot_timer", "state_transition"}:
                continue
            if robot_id is not None and sample.get("robot") != robot_id:
                continue
            try:
                if int(sample.get("passes", 1)) == passes:
                    samples.append(float(sample["minutes"]))
            except (TypeError, ValueError, KeyError):
                continue
        return learned_duration_minutes(samples, float(self._room_settings(room)["expected_minutes"]))

    def _forecast(self, room: DiscoveredRoom, now: datetime, duration_minutes: float) -> Forecast:
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
            int(duration_minutes) + EXTRA_CLEAR_MINUTES,
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

    def _desired_window_allows(self, room: DiscoveredRoom, now: datetime) -> bool:
        """Apply the global desired window unless this room explicitly ignores it."""

        return desired_window_allows(
            bool(self._room_settings(room).get("ignore_desired_window", False)),
            _local(now),
            str(self.data.get("unresolved_start", DEFAULT_UNRESOLVED_START)),
            str(self.data.get("unresolved_end", DEFAULT_UNRESOLVED_END)),
        )

    def _unresolved_allowed(self, room: DiscoveredRoom, now: datetime) -> bool:
        """Permit unresolved occupancy only inside the desired cleaning window."""

        return unresolved_occupancy_allowed(
            str(self._room_data(room.area_id).get("occupancy")),
            room.is_bedroom_transit,
            _local(now),
            str(self.data.get("unresolved_start", DEFAULT_UNRESOLVED_START)),
            str(self.data.get("unresolved_end", DEFAULT_UNRESOLVED_END)),
        )

    def _room_candidate(self, room: DiscoveredRoom, now: datetime) -> tuple[dict[str, Any] | None, str]:
        settings = self._room_settings(room)
        detail = self._room_data(room.area_id)
        if not settings.get("enabled", True):
            return None, "room disabled"
        vacuum_due = self._room_due(room, "vacuum", now)
        carpet = bool(settings.get("carpet", False))
        mop_due = None if carpet else self._room_due(room, "mop", now)
        capable = [
            robot
            for robot in self.discovery.robots.values()
            if robot.floor_id == room.floor_id and robot.supports_area_clean
        ]
        can_mop = any(self._mop_ready(robot) for robot in capable)
        mop_makes_room_due = not carpet and can_mop and mop_due is not None and mop_due <= now
        if vacuum_due > now and not mop_makes_room_due:
            return None, "not due"
        if detail.get("occupancy") == "occupied":
            return None, f"occupancy {detail.get('occupancy')} ({detail.get('source')})"
        if not self._desired_window_allows(room, now):
            return None, "waiting for desired cleaning window"
        unresolved_window_allowed = self._unresolved_allowed(room, now)
        if detail.get("occupancy") != "unoccupied" and not unresolved_window_allowed:
            if detail.get("occupancy") == "unresolved":
                if room.is_bedroom_transit:
                    return None, "unresolved occupancy; bedroom-transit excluded"
                return None, "unresolved occupancy; waiting for desired cleaning window"
            return None, f"occupancy {detail.get('occupancy')} ({detail.get('source')})"
        if room.is_bedroom_transit:
            allowed, reason = self._hall_allowed(now)
            if not allowed:
                return None, reason
        operation, due = select_operation(vacuum_due, mop_due, can_mop, carpet, now)
        passes = 2 if any(bool(self._robot_settings(robot).get("double_pass")) for robot in capable) else 1
        duration_minutes, duration_sample_count = self._effective_duration(room, operation, passes)
        forecast = (
            Forecast(True, 0.0, "unresolved occupancy desired-window policy")
            if unresolved_window_allowed
            else self._forecast(room, now, duration_minutes)
        )
        if not forecast.allowed:
            return None, forecast.reason
        return {
            "room": room,
            "operation": operation,
            "due_at": due,
            "confidence": forecast.confidence,
            "reason": forecast.reason,
            "duration_minutes": duration_minutes,
            "duration_sample_count": duration_sample_count,
            "passes": passes,
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
            "source": "scheduler",
            "expected_minutes": candidate["duration_minutes"],
            "expected_end": _iso(now + timedelta(minutes=candidate["duration_minutes"])),
            "last_observed_at": _iso(now),
            "passes": candidate["passes"],
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
        except Exception:  # ServiceValidationError varies between HA versions.
            self.data["active"][robot.entity_id] = None
            detail = self._room_data(room.area_id)
            detail["map_status"] = "error"
            detail["map_error"] = "unknown dispatch error"
            _LOGGER.exception(
                "Adaptive RoboVacs room dispatch failed: robot=%s room=%s "
                "area_id=%s operation=%s",
                robot.entity_id,
                room.name,
                room.area_id,
                candidate["operation"],
            )
            await self._async_save()
            return False, "dispatch failed: unknown error"
        active["phase"] = "accepted"
        active["accepted_at"] = _iso(_now())
        self._room_data(room.area_id)["map_status"] = "mapped"
        self._room_data(room.area_id)["map_error"] = None
        await self._async_save()
        return True, f"dispatched {room.name}"

    async def _async_reconcile_jobs(
        self, now: datetime, transition: dict[str, Any] | None = None
    ) -> None:
        """Persist completion only after an accepted command has actually cleaned."""

        changed = False
        robot_ids = set(self.discovery.robots) | set(self.data["active"]) | set(self.data["robot_holds"])
        for robot_id in robot_ids:
            active = self.data["active"].get(robot_id)
            state = self.hass.states.get(robot_id)
            state_text = state.state if state else "unavailable"
            if active and state_text not in {"unavailable", "unknown"}:
                active["last_observed_at"] = _iso(now)
                changed = True

            hold_action = self._reconcile_robot_hold(robot_id, state_text, active, now)
            if hold_action == "held":
                if active:
                    changed = self._hold_active_job(robot_id, active, state_text, now) or changed
                else:
                    changed = True
                continue
            if hold_action == "resumed":
                if active:
                    self._resume_held_job(robot_id, active, state, now)
                changed = True
                continue
            if hold_action in {"cancelling", "completion_pending"}:
                if active:
                    self._set_held_job_phase(robot_id, active, hold_action, now)
                changed = True
                continue
            if hold_action == "cancelled":
                cancelled_at = _as_datetime(
                    self.data["robot_holds"].get(robot_id, {}).get("returning_at")
                ) or now
                if active:
                    self._cancel_job(robot_id, active, cancelled_at, "physical_cancelled")
                self._apply_robot_cancellation_deferral(robot_id, cancelled_at)
                self.data["robot_holds"].pop(robot_id, None)
                changed = True
                continue
            if hold_action == "complete":
                if active:
                    completion = _as_datetime(active.get("cleaning_finished")) or now
                    self._complete_job(robot_id, active, completion, "observed")
                self.data["robot_holds"].pop(robot_id, None)
                changed = True
                continue
            if not active:
                continue

            recovered_at = _as_datetime(active.get("recovered_at"))
            transition_at = _as_datetime(transition.get("at")) if transition else None
            live_recovery_transition = bool(
                active.get("phase") == "recovery_waiting"
                and transition
                and transition.get("robot") == robot_id
                and recovery_transition_is_observed(
                    transition.get("from"),
                    transition.get("to"),
                    transition_at,
                    recovered_at,
                )
            )
            if live_recovery_transition and transition_at:
                self._cancel_recovery_timer(robot_id)
                if transition["to"] == "returning":
                    self._mark_observed_completion(robot_id, active, transition_at)
                    active["phase"] = "returning"
                else:
                    self._mark_observed_completion(robot_id, active, transition_at)
                    self._complete_job(robot_id, active, transition_at, "observed")
                changed = True
                continue
            if state_text == "cleaning":
                self._resume_held_job(robot_id, active, state, now)
                changed = True
                continue
            if active.get("seen_cleaning") and state_text == "returning":
                expected_end = _as_datetime(active.get("expected_end"))
                if active.get("phase") == "recovery_waiting":
                    if expected_end and now >= expected_end:
                        active["cleaning_finished"] = _iso(expected_end)
                        active["completion_confidence"] = "recovered_expected_end"
                        active["phase"] = "returning"
                        changed = True
                    continue
                if not active.get("cleaning_finished"):
                    self._mark_observed_completion(
                        robot_id, active, state.last_changed if state else now
                    )
                active["phase"] = "returning"
                self._cancel_recovery_timer(robot_id)
                changed = True
                continue
            if active.get("seen_cleaning") and state_text in {"docked", "idle"}:
                expected_end = _as_datetime(active.get("expected_end"))
                if active.get("phase") == "recovery_waiting" and expected_end and now < expected_end:
                    continue
                completion = _as_datetime(active.get("cleaning_finished")) or (state.last_changed if state else now)
                if active.get("phase") == "recovery_waiting" and expected_end and now >= expected_end:
                    completion = expected_end
                    confidence = "recovered_expected_end"
                else:
                    confidence = str(active.get("completion_confidence", "observed"))
                self._complete_job(robot_id, active, completion, confidence)
                changed = True
                continue
            started = _as_datetime(active.get("started"))
            if (
                not active.get("seen_cleaning")
                and started
                and now - started > timedelta(minutes=10)
            ):
                if active.get("source") == "manual_home_assistant":
                    self._record_manual_event(
                        {
                            "at": _iso(now),
                            "robot": robot_id,
                            "rooms": self._active_rooms(active),
                            "context_id": active.get("manual_context_id"),
                            "outcome": "not_started_or_cancelled",
                        }
                    )
                else:
                    detail = self._room_data(active["room"])
                    detail["map_status"] = "error"
                    detail["map_error"] = "unknown dispatch error"
                    _LOGGER.error(
                        "Adaptive RoboVacs room dispatch was accepted but did not "
                        "enter cleaning within 10 minutes: robot=%s area_id=%s "
                        "operation=%s started=%s current_state=%s",
                        robot_id,
                        active["room"],
                        active.get("operation"),
                        active.get("started"),
                        state_text,
                    )
                self.data["active"][robot_id] = None
                self._cancel_recovery_timer(robot_id)
                changed = True
        if changed:
            await self._async_save()

    def _mark_observed_completion(
        self, robot_id: str, active: dict[str, Any], completion: datetime
    ) -> None:
        """Record a completion from a native state transition and learn from it."""

        active["cleaning_finished"] = _iso(completion)
        active["measured_minutes"] = self._measured_duration_minutes(robot_id, active)
        active["completion_confidence"] = "observed"

    def _cleaning_timer_minutes(self, robot_id: str) -> float | None:
        robot = self.discovery.robots.get(robot_id)
        entity_id = robot.profile.cleaning_time_entity_id if robot else None
        state = self.hass.states.get(entity_id) if entity_id else None
        try:
            value = float(state.state) if state else None
        except (TypeError, ValueError):
            return None
        unit = str(state.attributes.get("unit_of_measurement", "min")).lower() if state else "min"
        if unit in {"h", "hour", "hours"}:
            return value * 60
        if unit in {"s", "second", "seconds"}:
            return value / 60
        return value

    def _measured_duration_minutes(self, robot_id: str, active: dict[str, Any]) -> float | None:
        timer = self._cleaning_timer_minutes(robot_id)
        timer_start = active.get("timer_start")
        if timer is not None and timer_start is not None and timer >= float(timer_start):
            active["duration_source"] = "robot_timer"
            return timer - float(timer_start)
        started = _as_datetime(active.get("observed_started"))
        finished = _as_datetime(active.get("cleaning_finished"))
        if started and finished:
            active["duration_source"] = "state_transition"
            return (finished - started).total_seconds() / 60
        return None

    def _cancel_job(
        self, robot_id: str, active: dict[str, Any], cancelled_at: datetime, reason: str
    ) -> None:
        """Close a user-cancelled job without recording an incomplete room clean."""

        area_ids = self._active_rooms(active)
        if active.get("source") == "manual_home_assistant":
            self._record_manual_event(
                {
                    "at": _iso(cancelled_at),
                    "robot": robot_id,
                    "rooms": area_ids,
                    "operations": list(active.get("requested_operations", ["vacuum"])),
                    "context_id": active.get("manual_context_id"),
                    "outcome": "cancelled",
                }
            )
        self.data["recovery_events"].append(
            {
                "robot": robot_id,
                "rooms": area_ids,
                "at": _iso(cancelled_at),
                "reason": reason,
            }
        )
        self.data["recovery_events"] = self.data["recovery_events"][-20:]
        self.data["active"][robot_id] = None
        self._cancel_recovery_timer(robot_id)

    def _complete_job(self, robot_id: str, active: dict[str, Any], completion: datetime, confidence: str) -> None:
        area_ids = self._active_rooms(active)
        operation = active["operation"]
        if active.get("source") == "manual_home_assistant":
            changed = self._apply_manual_deferral(
                robot_id,
                area_ids,
                list(active.get("requested_operations", ["vacuum"])),
                completion,
            )
            self._record_manual_event(
                {
                    "at": _iso(completion),
                    "robot": robot_id,
                    "rooms": area_ids,
                    "operations": list(active.get("requested_operations", ["vacuum"])),
                    "context_id": active.get("manual_context_id"),
                    "outcome": "completed",
                    "confidence": confidence,
                    "deferred": changed,
                }
            )
        else:
            detail = self._room_data(active["room"])
            if operation in {"vacuum", "vac_and_mop"}:
                detail["vacuum"] = _iso(completion)
            if operation in {"mop", "vac_and_mop"}:
                detail["mop"] = _iso(completion)
        measured = active.get("measured_minutes")
        if (
            active.get("source") == "scheduler"
            and confidence == "observed"
            and not active.get("interrupted")
            and isinstance(measured, (float, int))
            and measured > 0
        ):
            detail = self._room_data(active["room"])
            detail.setdefault("duration_samples", []).append(
                {
                    "minutes": float(measured),
                    "operation": operation,
                    "passes": int(active.get("passes", 1)),
                    "robot": robot_id,
                    "source": active.get("duration_source", "state_transition"),
                    "at": _iso(completion),
                }
            )
            detail["duration_samples"] = detail["duration_samples"][-50:]
        self.data["recovery_events"].append(
            {
                "robot": robot_id,
                "rooms": area_ids,
                "at": _iso(completion),
                "reason": confidence,
            }
        )
        self.data["recovery_events"] = self.data["recovery_events"][-20:]
        self.data["active"][robot_id] = None
        self._cancel_recovery_timer(robot_id)

    def _apply_manual_deferral(
        self,
        robot_entity_id: str,
        area_ids: list[str],
        operations: list[str],
        completed_at: datetime,
    ) -> list[str]:
        """Apply the narrow, one-day manual-clean deferral policy."""

        changed: list[str] = []
        if robot_entity_id not in self.discovery.robots:
            return changed
        for area_id in area_ids:
            room = self.discovery.rooms.get(area_id)
            if not room:
                continue
            detail = self._room_data(area_id)
            for operation in operations:
                if operation not in {"vacuum", "mop"}:
                    continue
                if operation == "mop" and self._room_settings(room).get("carpet", False):
                    continue
                next_due = self._room_due(room, operation, completed_at)
                deferred = manual_deferral(completed_at, next_due)
                if deferred:
                    detail.setdefault("defer", {})[operation] = _iso(deferred)
                    changed.append(f"{area_id}:{operation}")
        return changed

    def _record_manual_event(self, event: dict[str, Any]) -> None:
        """Retain a bounded audit trail without changing normal cadence."""

        self.data["manual_events"].append(event)
        self.data["manual_events"] = self.data["manual_events"][-50:]

    async def async_evaluate(
        self,
        dry_run: bool = False,
        reason: str = "manual",
        transition: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Refresh state, publish a safe preview, and optionally dispatch work."""

        async with self._lock:
            now = _now()
            await self.async_refresh_discovery()
            self._observe_occupancy(now)
            await self._async_reconcile_jobs(now, transition)
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
                passes = 2 if bool(self._robot_settings(robot).get("double_pass")) else 1
                if candidate["passes"] != passes:
                    duration_minutes, duration_sample_count = self._effective_duration(
                        room, candidate["operation"], passes, robot.entity_id
                    )
                    # The candidate was conservatively checked with any configured
                    # double-pass robot.  Use the selected robot's exact profile
                    # for the durable job checkpoint and duration learning key.
                    candidate = {
                        **candidate,
                        "passes": passes,
                        "duration_minutes": duration_minutes,
                        "duration_sample_count": duration_sample_count,
                    }
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
        changed = self._apply_manual_deferral(robot_entity_id, area_ids, operations, now)
        self._record_manual_event(
            {"at": _iso(now), "robot": robot_entity_id, "rooms": area_ids, "operations": operations, "changed": changed}
        )
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
        desired_window_start = next_window_start(
            _local(now), str(self.data.get("unresolved_start", DEFAULT_UNRESOLVED_START))
        )
        vacuum_due = self._room_due(room, "vacuum", now)
        mop_due = None if settings.get("carpet", False) else self._room_due(room, "mop", now)
        capable = [
            robot
            for robot in self.discovery.robots.values()
            if robot.floor_id == room.floor_id and robot.supports_area_clean
        ]
        can_mop = any(self._mop_ready(robot) for robot in capable)
        next_due = min(vacuum_due, mop_due) if mop_due and can_mop else vacuum_due
        candidate, reason = self._room_candidate(room, now)
        active_robot_id, active = next(
            (
                (robot_id, job)
                for robot_id, job in self.data["active"].items()
                if job and area_id in self._active_rooms(job)
            ),
            (None, None),
        )
        active_robot_state = (
            self.hass.states.get(active_robot_id).state
            if active_robot_id and self.hass.states.get(active_robot_id)
            else None
        )
        duration_operation = (
            active["operation"] if active else candidate["operation"] if candidate else "vacuum"
        )
        duration_passes = int(active.get("passes", 1)) if active else candidate["passes"] if candidate else 1
        duration_minutes, duration_sample_count = self._effective_duration(
            room, duration_operation, duration_passes, active_robot_id
        )
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
            "carpet": settings["carpet"],
            "ignore_desired_window": settings["ignore_desired_window"],
            "occupancy": detail["occupancy"],
            "occupancy_source": detail["source"],
            "unavailable_radars": detail["unavailable_radars"],
            "last_cleaned": max(filter(None, [_as_datetime(detail.get("vacuum")), _as_datetime(detail.get("mop"))]), default=None),
            "last_vacuum": _as_datetime(detail.get("vacuum")),
            "last_mop": _as_datetime(detail.get("mop")),
            "vacuum_due": vacuum_due,
            "mop_due": mop_due,
            "next_due": next_due,
            "desired_window_start": desired_window_start,
            # Retained for existing dashboard templates and integrations.
            "unresolved_window_start": desired_window_start,
            "next_candidate": candidate,
            "active": active,
            "active_robot": active_robot_id,
            "active_robot_state": active_robot_state,
            "effective_duration_minutes": duration_minutes,
            "duration_sample_count": duration_sample_count,
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
        hold = self.data["robot_holds"].get(entity_id)
        active_rooms = [
            self.discovery.rooms[area_id].name
            for area_id in self._active_rooms(active)
            if area_id in self.discovery.rooms
        ] if active else []
        return {
            "name": robot.name,
            "entity_id": entity_id,
            "floor_id": robot.floor_id,
            "state": state.state if state else "unavailable",
            "battery": self._robot_battery(robot),
            "ready": ready,
            "reason": reason,
            "active": active,
            "scheduler_hold": hold,
            "active_room": ", ".join(active_rooms) if active_rooms else None,
            "active_rooms": active_rooms,
            "profile": robot.profile,
            "settings": self._robot_settings(robot),
        }
