"""Durable registry-driven scheduler for Adaptive RoboVacs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import logging
import secrets
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_CALL_SERVICE, EVENT_HOMEASSISTANT_STARTED, EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

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
    DEFAULT_UNRESOLVED_END,
    DEFAULT_UNRESOLVED_START,
    DOMAIN,
    EVENT_EVALUATION,
    EXTRA_CLEAR_MINUTES,
    FALLBACK_SAMPLE_COUNT,
    HISTORY_DAYS,
    SIGNAL_DISCOVERY_UPDATED,
    START_CONFIRMATION_TIMEOUT,
    STORAGE_KEY,
    STORE_VERSION,
)
from .discovery import DiscoveredRobot, DiscoveredRoom, DiscoveryResult, async_discover
from .jobs import JobLifecycle
from .models import (
    Forecast,
    ResolvedDailyWindow,
    can_refresh_pending_occurrence_profile,
    can_start_scheduled_clean,
    cleaning_profile_is_supported,
    cleaning_profile_sources,
    desired_window_allows,
    due_at,
    forecast_vacancy,
    held_job_transition,
    in_daytime_window,
    learned_duration_minutes,
    manual_clean_robot_is_docked,
    manual_deferral,
    pending_completion_is_docked,
    resolve_daily_window,
    resolve_cleaning_profile,
    requested_cleaning_profile,
    parse_manual_clean_request,
    offline_held_recovery_outcome,
    rebase_due_times,
    recovery_transition_is_observed,
    resolve_occupancy,
    effective_cleaning_program,
    expand_cleaning_program,
    stage_pass_count,
    unresolved_occupancy_allowed,
)
from .projections import robot_state, room_state
from .state import SchedulerState, StateSchemaError, migrate_runtime_robot_identity
from .runtime import HomeAssistantRuntime
from .repairs_manager import (
    async_create_scheduler_halted_issue,
    async_delete_scheduler_halted_issue,
    async_sync_two_pass_issues,
    async_sync_cleaning_program_issues,
    async_set_notification_delivery_issue,
    fault_summary,
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
        "cleaning": None,
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
        "last_stage_outcome": None,
        "last_stage_reason": None,
        "last_stage_at": None,
    }


def _blank_data(entry: ConfigEntry) -> dict[str, Any]:
    return {
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
        "scheduler_fault": None,
        "occurrences": {},
        "water_confirmations": {},
        "water_notification_episodes": {},
    }


class AdaptiveRoboVacCoordinator:
    """Own all scheduler state and dispatch safely through Home Assistant."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}"
        )
        self.data: dict[str, Any] = _blank_data(entry)
        self.state = SchedulerState.create(entry.data)
        self._storage_safe_mode = False
        self.discovery = DiscoveryResult()
        self._lock = asyncio.Lock()
        self._unsubscribers: list[Callable[[], None]] = []
        self._listeners: set[EntityListener] = set()
        self._watch_entity_ids: set[str] = set()
        self._recovery_timers: dict[str, Callable[[], None]] = {}
        self._start_confirmation_timers: dict[str, Callable[[], None]] = {}
        self._water_confirmation_timers: dict[str, Callable[[], None]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closing = False
        self._identity_migrated = False
        self.jobs = JobLifecycle(self)
        self.runtime = HomeAssistantRuntime(self)

    async def async_initialize(self) -> None:
        """Restore state, discover the house, and begin passive observation."""

        stored = await self.store.async_load()
        try:
            self.state, migrated = SchedulerState.from_store(stored, self.entry.data)
        except StateSchemaError:
            # Do not overwrite a Store written by a newer version or a malformed
            # payload.  A fresh observe-only view keeps the robot authoritative.
            self._storage_safe_mode = True
            self.state = SchedulerState.create(self.entry.data)
            self.state.global_settings.observe_only = True
            self.data = self.state.to_runtime_data()
            _LOGGER.exception(
                "Adaptive RoboVacs could not safely load persisted scheduler state; "
                "dispatch is disabled until the Store is repaired"
            )
        else:
            self.data = self.state.to_runtime_data()

        await self.async_refresh_discovery()
        if migrated or self._identity_migrated:
            await self._async_save()
        if self.data.get("scheduler_fault"):
            async_create_scheduler_halted_issue(self)
        await self._async_recover_active_jobs()
        self._unsubscribers.extend(
            [
                async_track_time_interval(self.hass, self._async_interval, timedelta(minutes=15)),
                self.hass.bus.async_listen(EVENT_CALL_SERVICE, self._on_call_service),
                self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._on_state_changed),
                self.hass.bus.async_listen(
                    "mobile_app_notification_action", self._on_mobile_notification_action
                ),
                self.hass.bus.async_listen(
                    "mobile_app_notification_cleared", self._on_mobile_notification_cleared
                ),
                self.hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STARTED, self._on_home_assistant_started
                ),
            ]
        )
        await self._async_restore_water_confirmations()
        await self.async_evaluate(dry_run=True, reason="startup")

        @callback
        def refresh_late_vendor_entities(_timestamp: datetime) -> None:
            self._async_create_task(
                self.async_evaluate(
                    dry_run=True, reason="post-start-capability-refresh"
                )
            )

        self._unsubscribers.append(
            async_track_point_in_utc_time(
                self.hass,
                refresh_late_vendor_entities,
                _now() + timedelta(seconds=30),
            )
        )

    async def async_shutdown(self) -> None:
        """Stop callbacks, drain coordinator work, and persist once."""

        self._closing = True
        while self._recovery_timers:
            self._recovery_timers.popitem()[1]()
        while self._start_confirmation_timers:
            self._start_confirmation_timers.popitem()[1]()
        while self._water_confirmation_timers:
            self._water_confirmation_timers.popitem()[1]()
        while self._unsubscribers:
            self._unsubscribers.pop()()
        current = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current and not task.done()]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=10)
            for task in pending:
                task.cancel("Adaptive RoboVacs config entry unloading")
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        async with self._lock:
            await self._async_save()

    def begin_shutdown(self) -> None:
        """Gate callbacks before Home Assistant starts unloading platforms."""

        self._closing = True

    def cancel_shutdown(self) -> None:
        """Resume normal work when platform unload is rejected."""

        self._closing = False

    def _async_create_task(
        self, coro: Coroutine[Any, Any, Any], *, name: str | None = None
    ) -> asyncio.Task[Any] | None:
        """Create one config-entry-owned task unless shutdown has begun."""

        if self._closing:
            coro.close()
            return None
        task = self.entry.async_create_task(
            self.hass,
            coro,
            name=name or f"{DOMAIN}:{self.entry.entry_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

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

    def _notification_services(self) -> tuple[str, ...]:
        """Resolve current Companion notification targets without persisting them."""

        try:
            services = self.hass.services.async_services().get("notify", {})
        except (AttributeError, TypeError):
            return ()
        return tuple(
            sorted(
                name
                for name in services
                if isinstance(name, str) and name.startswith("mobile_app_")
            )
        )

    def has_notification_targets(self) -> bool:
        """Return whether at least one Companion target currently resolves."""

        return bool(self._notification_services())

    async def _async_send_mobile_notification(
        self, payload: dict[str, Any]
    ) -> tuple[int, int]:
        """Deliver to every current Companion target with aggregate diagnostics."""

        targets = self._notification_services()
        delivered = 0
        failed = 0
        for service in targets:
            try:
                await self.hass.services.async_call(
                    "notify", service, payload, blocking=True
                )
            except Exception:
                failed += 1
            else:
                delivered += 1
        if failed:
            _LOGGER.warning(
                "Adaptive RoboVacs mobile notification delivery failed for %s of %s targets",
                failed,
                len(targets),
            )
        async_set_notification_delivery_issue(self, delivered == 0)
        return delivered, len(targets)

    async def _async_clear_mobile_notification(self, tag: str) -> None:
        await self._async_send_mobile_notification(
            {"message": "clear_notification", "data": {"tag": tag}}
        )

    @staticmethod
    def _action_hash(action: str) -> str:
        return hashlib.sha256(action.encode("utf-8")).hexdigest()

    def _schedule_water_confirmation(self, request: dict[str, Any]) -> None:
        request_id = str(request["request_id"])
        unsubscribe = self._water_confirmation_timers.pop(request_id, None)
        if unsubscribe:
            unsubscribe()
        expires = _as_datetime(request.get("expires_at"))
        if expires is None:
            return

        @callback
        def expire(_timestamp: datetime) -> None:
            self._water_confirmation_timers.pop(request_id, None)
            self._async_create_task(self._async_expire_water_confirmation(request_id))

        self._water_confirmation_timers[request_id] = async_track_point_in_utc_time(
            self.hass, expire, expires
        )

    async def _async_restore_water_confirmations(self) -> None:
        now = _now()
        for request in tuple(self.data.get("water_confirmations", {}).values()):
            if request.get("status") not in {"pending", "confirmed"}:
                continue
            if (_as_datetime(request.get("expires_at")) or now) <= now:
                await self._async_expire_water_confirmation(str(request["request_id"]))
            else:
                self._schedule_water_confirmation(request)

    @callback
    def _on_mobile_notification_action(self, event: Event) -> None:
        action = event.data.get("action")
        if isinstance(action, str):
            self._async_create_task(
                self._async_handle_water_confirmation(action=action)
            )

    @callback
    def _on_mobile_notification_cleared(self, event: Event) -> None:
        request_id = event.data.get("adaptive_robovacs_request_id")
        tag = event.data.get("tag")
        self._async_create_task(
            self._async_handle_water_confirmation(
                request_id=str(request_id) if request_id else None,
                tag=str(tag) if tag else None,
                dismissed=True,
            )
        )

    async def _async_expire_water_confirmation(self, request_id: str) -> None:
        clear_tag: str | None = None
        async with self._lock:
            request = next(
                (
                    item
                    for item in self.data.get("water_confirmations", {}).values()
                    if item.get("request_id") == request_id
                ),
                None,
            )
            if not request or request.get("status") not in {"pending", "confirmed"}:
                return
            if request.get("status") == "confirmed":
                active = next(
                    (
                        job
                        for job in self.data.get("active", {}).values()
                        if job
                        and job.get("occurrence_id") == request.get("occurrence_id")
                        and job.get("stage_index") == request.get("stage_index")
                        and job.get("phase") in {"accepted", "cleaning", "returning", "completion_pending"}
                    ),
                    None,
                )
                if active:
                    return
            request["status"] = "expired"
            request["responded_at"] = _iso(_now())
            clear_tag = str(request.get("tag"))
            self._skip_occurrence_stage(
                str(request["room_id"]),
                int(request["stage_index"]),
                "skipped_unconfirmed_water",
                "water_confirmation_expired",
                _now(),
            )
            await self._async_save()
        if clear_tag:
            await self._async_clear_mobile_notification(clear_tag)
        self._async_create_task(
            self.async_evaluate(reason="water-confirmation-expired")
        )

    async def _async_handle_water_confirmation(
        self,
        *,
        action: str | None = None,
        request_id: str | None = None,
        tag: str | None = None,
        dismissed: bool = False,
    ) -> None:
        clear_tag: str | None = None
        result: str | None = None
        async with self._lock:
            now = _now()
            action_hash = self._action_hash(action) if action else None
            request = next(
                (
                    item
                    for item in self.data.get("water_confirmations", {}).values()
                    if item.get("status") == "pending"
                    and (
                        (action_hash and action_hash in {item.get("confirm_hash"), item.get("cancel_hash")})
                        or (request_id and item.get("request_id") == request_id)
                        or (tag and item.get("tag") == tag)
                    )
                ),
                None,
            )
            if not request:
                return
            expires = _as_datetime(request.get("expires_at")) or now
            confirm = bool(
                action_hash
                and action_hash == request.get("confirm_hash")
                and now < expires
                and not dismissed
            )
            request["status"] = "confirmed" if confirm else (
                "expired" if now >= expires else "cancelled"
            )
            request["responded_at"] = _iso(now)
            clear_tag = str(request.get("tag"))
            result = str(request["status"])
            if not confirm:
                timer = self._water_confirmation_timers.pop(str(request["request_id"]), None)
                if timer:
                    timer()
            if not confirm:
                self._skip_occurrence_stage(
                    str(request["room_id"]),
                    int(request["stage_index"]),
                    "skipped_unconfirmed_water",
                    "water_confirmation_cancelled",
                    now,
                )
            await self._async_save()
        if clear_tag:
            await self._async_clear_mobile_notification(clear_tag)
        self._async_create_task(
            self.async_evaluate(reason=f"water-confirmation-{result}")
        )

    async def _async_save(self) -> None:
        if self._storage_safe_mode:
            return
        durable = deepcopy(self.data)
        entity_to_registry = {
            robot.entity_id: robot.registry_id
            for robot in self.discovery.robots.values()
        }
        for section in ("active", "robot_holds"):
            runtime_values = durable.get(section, {})
            stable_values: dict[str, Any] = {}
            for key, value in runtime_values.items():
                stable_key = entity_to_registry.get(key, key)
                if stable_key not in stable_values or stable_values[stable_key] is None:
                    stable_values[stable_key] = value
            durable[section] = stable_values
        self.state, _ = SchedulerState.from_store(durable, self.entry.data)
        await self.store.async_save(self.state.to_store())

    async def async_refresh_discovery(self) -> None:
        """Refresh registry state and reset only changed room occupancy models."""

        prior_discovery = self.discovery
        self.discovery = await async_discover(self.hass)
        self._identity_migrated = (
            self._migrate_runtime_robot_identity(prior_discovery)
            or self._identity_migrated
        )

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
            self._watch_entity_ids.update(
                entity_id
                for entity_id in (
                    robot.profile.mode_select_entity_id,
                    robot.profile.mop_mode_select_entity_id,
                    robot.profile.mop_intensity_select_entity_id,
                    robot.profile.passes_select_entity_id,
                )
                if entity_id
            )
            # A vendor select may not expose its options until after this
            # integration's first discovery pass. Watch every same-device
            # select so an initially unclassified operation control is found.
            self._watch_entity_ids.update(
                evidence.entity_id
                for evidence in robot.adapter_entities
                if evidence.domain == "select"
            )
            self._watch_entity_ids.update(robot.adapter_capabilities.watched_entity_ids)

        for area_id in tuple(self.data.get("water_notification_episodes", {})):
            room = self.discovery.rooms.get(area_id)
            room_settings = self._room_settings(room) if room else None
            has_mop_program = False
            water_ready = False
            if room and room_settings and room_settings.get("enabled", True) and not room_settings.get("carpet"):
                for robot in self.discovery.robots.values():
                    if robot.floor_id != room.floor_id:
                        continue
                    program = effective_cleaning_program(
                        room_settings.get("cleaning_program"),
                        str(self._robot_settings(robot).get("cleaning_program", "vacuum_only")),
                    )
                    has_mop_program = has_mop_program or "mop" in expand_cleaning_program(program or "")
                    water_ready = water_ready or bool(
                        robot.adapter_capabilities.water_readiness.ready
                    )
            if not has_mop_program or water_ready:
                self.data["water_notification_episodes"].pop(area_id, None)

        if prior_discovery != self.discovery:
            async_dispatcher_send(self.hass, SIGNAL_DISCOVERY_UPDATED, self.entry.entry_id)
        async_sync_two_pass_issues(self)
        async_sync_cleaning_program_issues(self)
        self._notify_listeners()

    def _migrate_runtime_robot_identity(
        self, prior_discovery: DiscoveryResult
    ) -> bool:
        """Bind stable robot keys to current entity IDs without losing aliases."""

        return migrate_runtime_robot_identity(
            self.data,
            {
                robot.registry_id: robot.entity_id
                for robot in self.discovery.robots.values()
            },
            {
                robot.registry_id: robot.entity_id
                for robot in prior_discovery.robots.values()
            },
        )

    def _room_data(self, area_id: str) -> dict[str, Any]:
        return self.data["rooms"].setdefault(area_id, _blank_room())

    def _room_settings(self, room: DiscoveredRoom) -> dict[str, Any]:
        settings = self.data["settings"]["rooms"].setdefault(
            room.area_id,
            {
                "enabled": not room.is_bedroom,
                "cleaning_interval": (
                    DEFAULT_BEDROOM_INTERVAL if room.is_bedroom else DEFAULT_COMMON_INTERVAL
                ),
                "expected_minutes": DEFAULT_EXPECTED_MINUTES,
                "carpet": False,
                "ignore_desired_window": False,
                "desired_window_start": None,
                "desired_window_end": None,
                "cleaning_program": None,
                "vacuum_pass_count": None,
                "mop_pass_count": None,
                "fan_speed": None,
                "mode": None,
                "mop_mode": None,
                "mop_intensity": None,
                "cleaning_depth": None,
            },
        )
        # Existing persisted settings predate newer optional room controls.
        settings.setdefault("carpet", False)
        settings.setdefault("ignore_desired_window", False)
        settings.setdefault("desired_window_start", None)
        settings.setdefault("desired_window_end", None)
        settings.setdefault(
            "cleaning_interval",
            settings.get("vacuum_interval", DEFAULT_BEDROOM_INTERVAL if room.is_bedroom else DEFAULT_COMMON_INTERVAL),
        )
        settings["vacuum_interval"] = settings["cleaning_interval"]
        settings["mop_interval"] = settings["cleaning_interval"]
        settings.setdefault("cleaning_program", None)
        settings.setdefault("vacuum_pass_count", settings.get("pass_count"))
        settings.setdefault("mop_pass_count", None)
        settings.setdefault("fan_speed", None)
        settings.setdefault("mode", None)
        settings.setdefault("mop_mode", None)
        settings.setdefault("mop_intensity", None)
        settings.setdefault("cleaning_depth", None)
        settings["pass_count"] = settings["vacuum_pass_count"]
        return settings

    def _desired_window(self, room: DiscoveredRoom) -> ResolvedDailyWindow:
        """Resolve one room's independently inherited daily window."""

        settings = self._room_settings(room)
        return resolve_daily_window(
            settings.get("desired_window_start"),
            settings.get("desired_window_end"),
            str(self.data.get("unresolved_start", DEFAULT_UNRESOLVED_START)),
            str(self.data.get("unresolved_end", DEFAULT_UNRESOLVED_END)),
        )

    def _robot_settings(self, robot: DiscoveredRobot) -> dict[str, Any]:
        settings = self.data["settings"]["robots"].setdefault(
            robot.registry_id,
            {
                "enabled": True,
                "minimum_battery": DEFAULT_MINIMUM_BATTERY,
                "cleaning_program": (
                    "vacuum_then_mop"
                    if "mop" in robot.adapter_capabilities.supported_operations
                    else "vacuum_only"
                ),
                "double_pass": False,
                "mop_double_pass": False,
                "mode": None,
                "mop_mode": None,
                "mop_intensity": None,
                "fan_speed": None,
                "cleaning_depth": (
                    "daily"
                    if robot.adapter_capabilities.cleaning_depth_options
                    else None
                ),
                "cleaning_depth_configured": bool(
                    robot.adapter_capabilities.cleaning_depth_options
                ),
            },
        )
        if "cleaning_program" not in settings:
            settings["cleaning_program"] = (
                "vacuum_then_mop"
                if settings.get("mopping_enabled")
                and "mop" in robot.adapter_capabilities.supported_operations
                else "vacuum_only"
            )
        settings.setdefault("mop_double_pass", False)
        settings.setdefault("cleaning_depth", None)
        if (
            not settings.get("cleaning_depth_configured")
            and settings.get("cleaning_depth") is not None
        ):
            settings["cleaning_depth_configured"] = True
        elif (
            not settings.get("cleaning_depth_configured")
            and robot.adapter_capabilities.cleaning_depth_options
        ):
            settings["cleaning_depth"] = (
                "daily"
            )
            settings["cleaning_depth_configured"] = True
        else:
            settings.setdefault("cleaning_depth_configured", False)
        settings["mopping_enabled"] = settings["cleaning_program"] != "vacuum_only"
        return settings

    def robot_unique_fragment(self, entity_id: str) -> str:
        """Return the original entity-ID fragment retained for stable unique IDs."""

        robot = self.discovery.robots.get(entity_id)
        if robot is None:
            return entity_id
        return str(
            self.data.get("robot_entity_aliases", {}).get(
                robot.registry_id, robot.entity_id
            )
        )

    def robot_registry_id(self, entity_id: str) -> str:
        """Resolve a current runtime entity ID to durable registry identity."""

        robot = self.discovery.robots.get(entity_id)
        return robot.registry_id if robot else entity_id

    @property
    def observe_only(self) -> bool:
        return self._storage_safe_mode or bool(self.data.get("observe_only", True))

    @property
    def party_mode(self) -> bool:
        return bool(self.data.get("party_mode", False))

    @property
    def scheduler_halted(self) -> bool:
        return bool(self.data.get("scheduler_fault"))

    def get_global_setting(self, key: str) -> Any:
        """Return a global control value without exposing mutable Store data."""

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
        return self.observe_only if key == "observe_only" else self.data[key]

    def get_room_setting(self, area_id: str, key: str) -> Any:
        """Return one discovered room setting without exposing mutable state."""

        room = self.discovery.rooms.get(area_id)
        if room is None:
            raise ValueError(f"Unknown room area: {area_id}")
        if key not in {
            "enabled",
            "cleaning_interval",
            "vacuum_interval",
            "expected_minutes",
            "carpet",
            "ignore_desired_window",
            "desired_window_start",
            "desired_window_end",
            "cleaning_program",
            "vacuum_pass_count",
            "mop_pass_count",
            "pass_count",
            "fan_speed",
            "mode",
            "mop_mode",
            "mop_intensity",
            "cleaning_depth",
        }:
            raise ValueError(f"Unknown room setting: {key}")
        aliases = {"vacuum_interval": "cleaning_interval", "pass_count": "vacuum_pass_count"}
        return self._room_settings(room)[aliases.get(key, key)]

    def scheduler_summary(self) -> dict[str, Any]:
        """Return the scheduler metadata used by the status sensor."""

        return {
            "last_evaluation": self.data.get("last_evaluation"),
            "preview": self.data.get("last_preview", {}),
            "scheduler_fault": self.scheduler_fault_view(),
        }

    def robot_for_registry_id(self, registry_id: str) -> DiscoveredRobot | None:
        """Resolve a stable entity-registry identity to the current vacuum."""

        return next(
            (
                robot
                for robot in self.discovery.robots.values()
                if robot.registry_id == registry_id
            ),
            None,
        )

    def scheduler_fault_view(self) -> dict[str, Any] | None:
        """Project the durable dispatch fault without sensitive adapter data."""

        fault = self.data.get("scheduler_fault")
        if not fault:
            return None
        robot = self.robot_for_registry_id(str(fault.get("robot_registry_id", "")))
        room = self.discovery.rooms.get(str(fault.get("room_area_id", "")))
        reason_code = str(fault.get("reason_code", "start_outcome_uncertain"))
        return {
            "failure_code": reason_code,
            "failure_summary": fault_summary(reason_code),
            "failure_since": fault.get("occurred_at"),
            "failure_phase": fault.get("phase"),
            "repair_active": True,
            "robot": robot.name if robot else None,
            "room": room.name if room else None,
        }

    def fault_affects_robot(self, robot: DiscoveredRobot) -> bool:
        fault = self.data.get("scheduler_fault")
        return bool(fault and fault.get("robot_registry_id") == robot.registry_id)

    def fault_affects_room(self, room: DiscoveredRoom) -> bool:
        fault = self.data.get("scheduler_fault")
        return bool(fault and fault.get("room_area_id") == room.area_id)

    async def _async_latch_scheduler_fault(
        self,
        robot: DiscoveredRobot,
        room: DiscoveredRoom,
        reason_code: str,
        phase: str,
        *,
        native_command_may_have_started: bool,
        outcome_uncertain: bool,
    ) -> None:
        """Persist the first start failure before any later dispatch can occur."""

        if self.data.get("scheduler_fault"):
            return
        occurred_at = _now()
        self.data["scheduler_fault"] = {
            "reason_code": reason_code,
            "robot_registry_id": robot.registry_id,
            "room_area_id": room.area_id,
            "occurred_at": _iso(occurred_at),
            "phase": phase,
            "native_command_may_have_started": native_command_may_have_started,
            "outcome_uncertain": outcome_uncertain,
        }
        active = self.data["active"].get(robot.entity_id)
        if outcome_uncertain and active:
            active["phase"] = "start_outcome_uncertain"
            active["last_observed_at"] = _iso(occurred_at)
        else:
            if active and active.get("occurrence_id"):
                occurrence = self.data.get("occurrences", {}).get(room.area_id)
                stage_index = active.get("stage_index")
                if (
                    occurrence
                    and isinstance(stage_index, int)
                    and stage_index < len(occurrence.get("stages", []))
                ):
                    occurrence["stages"][stage_index]["status"] = "pending"
                    occurrence["stages"][stage_index]["started_at"] = None
            self.data["active"][robot.entity_id] = None
        detail = self._room_data(room.area_id)
        detail["map_status"] = "error"
        detail["map_error"] = fault_summary(reason_code)
        self._cancel_start_confirmation(robot.entity_id)
        await self._async_save()
        async_create_scheduler_halted_issue(self)
        self._notify_listeners()

    async def async_recheck_and_resume(self) -> bool:
        """Recheck without dispatching and explicitly clear a verified halt."""

        async with self._lock:
            fault = self.data.get("scheduler_fault")
            if not fault:
                return False
            await self.async_refresh_discovery()
            robot = self.robot_for_registry_id(str(fault.get("robot_registry_id", "")))
            room = self.discovery.rooms.get(str(fault.get("room_area_id", "")))
            if robot is None or room is None:
                return False
            state = self.hass.states.get(robot.entity_id)
            if not state or state.state != "docked":
                return False
            settings = self._robot_settings(robot)
            battery = self._robot_battery(robot)
            if (
                not settings.get("enabled", True)
                or battery is None
                or battery < float(settings.get("minimum_battery", DEFAULT_MINIMUM_BATTERY))
            ):
                return False
            active = self.data["active"].get(robot.entity_id)
            occurrence = self.data.get("occurrences", {}).get(room.area_id)
            stage = None
            if occurrence:
                index = int(occurrence.get("current_stage", 0))
                if index < len(occurrence.get("stages", [])):
                    stage = occurrence["stages"][index]
            operation = str(
                active.get("operation") if active else stage.get("operation") if stage else "vacuum"
            )
            passes = int(active.get("passes", 1) if active else stage.get("passes", 1) if stage else 1)
            if not robot.adapter_capabilities.supports(operation, passes):
                return False
            candidate = {
                "room": room,
                "operation": operation,
                "passes": passes,
                "water_confirmed": bool(
                    occurrence
                    and (
                        request := self.data.get("water_confirmations", {}).get(
                            str(occurrence.get("occurrence_id"))
                        )
                    )
                    and request.get("status") == "confirmed"
                    and (_as_datetime(request.get("expires_at")) or _now()) > _now()
                ),
                "ignore_water_readiness": operation == "mop",
            }
            cleaning_profile = (
                active.get("cleaning_profile")
                if active
                else stage.get("cleaning_profile") if stage else None
            )
            if not isinstance(cleaning_profile, dict) or not cleaning_profile:
                resolved_profile = resolve_cleaning_profile(
                    operation,
                    self._room_settings(room),
                    settings,
                    robot.adapter_capabilities,
                )
                if resolved_profile is None:
                    return False
                cleaning_profile = resolved_profile.to_mapping()
            candidate["resolved_profile"] = cleaning_profile
            if not self.runtime.profile_is_ready(
                robot,
                str(candidate["operation"]),
                passes,
                cleaning_profile,
            ):
                return False
            try:
                preflight = await self.runtime.async_preflight(robot, candidate)
            except Exception:
                _LOGGER.exception(
                    "Adaptive RoboVacs non-dispatching repair recheck failed: robot=%s room=%s adapter=%s",
                    robot.entity_id,
                    room.name,
                    robot.adapter_id,
                )
                return False
            if not preflight.ready:
                return False
            if active and not active.get("seen_cleaning"):
                occurrence = self.data.get("occurrences", {}).get(room.area_id)
                stage_index = active.get("stage_index")
                if occurrence and isinstance(stage_index, int) and stage_index < len(occurrence.get("stages", [])):
                    occurrence["stages"][stage_index]["status"] = "pending"
                    occurrence["stages"][stage_index]["started_at"] = None
                self.data["active"][robot.entity_id] = None
            self.data["scheduler_fault"] = None
            detail = self._room_data(room.area_id)
            detail["map_status"] = "mapped"
            detail["map_error"] = None
            await self._async_save()
            async_delete_scheduler_halted_issue(self)
            self._notify_listeners()
            return True

    async def async_recheck_room_compatibility(self, area_id: str) -> bool:
        """Recheck a saved two-pass room without sending a clean."""

        async with self._lock:
            await self.async_refresh_discovery()
            room = self.discovery.rooms.get(area_id)
            if room is None:
                return False
            if self._room_settings(room).get("pass_count") != 2:
                return True
            compatible = any(
                robot.floor_id == room.floor_id
                and robot.supports_area_clean
                and 2 in robot.adapter_capabilities.supported_pass_counts
                for robot in self.discovery.robots.values()
            )
            if compatible:
                async_sync_two_pass_issues(self)
            return compatible

    async def async_recheck_cleaning_program_compatibility(self, area_id: str) -> bool:
        """Refresh and verify that one floor robot can execute the room program."""

        async with self._lock:
            await self.async_refresh_discovery()
            room = self.discovery.rooms.get(area_id)
            if room is None:
                return False
            base = {
                "room": room,
                "occurrence": self.data.get("occurrences", {}).get(area_id),
                "due_at": _now(),
                "confidence": 1.0,
                "reason": "repair recheck",
            }
            compatible = any(
                robot.floor_id == room.floor_id
                and robot.supports_area_clean
                and self._candidate_for_robot(base, robot) is not None
                for robot in self.discovery.robots.values()
            )
            if compatible:
                async_sync_cleaning_program_issues(self)
            return compatible

    def _cancel_start_confirmation(self, robot_id: str) -> None:
        unsubscribe = self._start_confirmation_timers.pop(robot_id, None)
        if unsubscribe:
            unsubscribe()

    def _schedule_start_confirmation(self, robot_id: str) -> None:
        """Schedule a bounded accepted-command confirmation check."""

        self._cancel_start_confirmation(robot_id)
        deadline = _now() + START_CONFIRMATION_TIMEOUT

        @callback
        def check_start(_timestamp: datetime) -> None:
            self._start_confirmation_timers.pop(robot_id, None)
            self._async_create_task(
                self.async_evaluate(
                    dry_run=True, reason=f"start-confirmation:{robot_id}"
                )
            )

        self._start_confirmation_timers[robot_id] = async_track_point_in_utc_time(
            self.hass, check_start, deadline
        )

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
        if key in {"unresolved_start", "unresolved_end"}:
            global_start = str(value) if key == "unresolved_start" else str(
                self.data.get("unresolved_start", DEFAULT_UNRESOLVED_START)
            )
            global_end = str(value) if key == "unresolved_end" else str(
                self.data.get("unresolved_end", DEFAULT_UNRESOLVED_END)
            )
            resolve_daily_window(None, None, global_start, global_end)
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
            "cleaning_interval",
            "vacuum_interval",
            "expected_minutes",
            "carpet",
            "ignore_desired_window",
            "desired_window_start",
            "desired_window_end",
            "cleaning_program",
            "vacuum_pass_count",
            "mop_pass_count",
            "pass_count",
            "fan_speed",
            "mode",
            "mop_mode",
            "mop_intensity",
            "cleaning_depth",
        }:
            raise ValueError(f"Unknown room setting: {key}")
        room = self.discovery.rooms[area_id]
        settings = self._room_settings(room)
        key = {"vacuum_interval": "cleaning_interval", "pass_count": "vacuum_pass_count"}.get(key, key)
        if key in {"vacuum_pass_count", "mop_pass_count"} and value not in {None, 1, 2}:
            raise ValueError("Room pass count must be Robot default, 1, or 2")
        if key == "cleaning_program" and value not in {
            None, "vacuum_only", "mop_only", "vacuum_then_mop", "mop_then_vacuum"
        }:
            raise ValueError("Unknown room cleaning program")
        if key in {
            "fan_speed",
            "mode",
            "mop_mode",
            "mop_intensity",
            "cleaning_depth",
        } and not (
            value is None or isinstance(value, str)
        ):
            raise ValueError("Room cleaning profile options must be strings or Robot default")
        if key in {"desired_window_start", "desired_window_end"}:
            configured_start = value if key == "desired_window_start" else settings.get(
                "desired_window_start"
            )
            configured_end = value if key == "desired_window_end" else settings.get(
                "desired_window_end"
            )
            resolve_daily_window(
                configured_start,
                configured_end,
                str(self.data.get("unresolved_start", DEFAULT_UNRESOLVED_START)),
                str(self.data.get("unresolved_end", DEFAULT_UNRESOLVED_END)),
            )
        settings[key] = value
        if key == "cleaning_interval":
            settings["vacuum_interval"] = value
            settings["mop_interval"] = value
        if key == "vacuum_pass_count":
            settings["pass_count"] = value
        await self._async_save()
        async_sync_cleaning_program_issues(self)
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
            "cleaning_program",
            "double_pass",
            "mop_double_pass",
            "mode",
            "mop_mode",
            "mop_intensity",
            "fan_speed",
            "cleaning_depth",
        }:
            raise ValueError(f"Unknown robot setting: {key}")
        settings = self._robot_settings(self.discovery.robots[entity_id])
        if key == "cleaning_program" and value not in {
            "vacuum_only", "mop_only", "vacuum_then_mop", "mop_then_vacuum"
        }:
            raise ValueError("Unknown robot cleaning program")
        if key in {
            "fan_speed",
            "mode",
            "mop_mode",
            "mop_intensity",
            "cleaning_depth",
        } and not (value is None or isinstance(value, str)):
            raise ValueError("Robot cleaning profile options must be strings or Not configured")
        if key == "mopping_enabled":
            settings["cleaning_program"] = "vacuum_then_mop" if value else "vacuum_only"
        else:
            settings[key] = value
        if key == "cleaning_depth":
            settings["cleaning_depth_configured"] = True
        settings["mopping_enabled"] = settings["cleaning_program"] != "vacuum_only"
        await self._async_save()
        async_sync_cleaning_program_issues(self)
        self._notify_listeners()
        await self.async_evaluate(dry_run=True, reason=f"robot:{entity_id}:{key}")

    async def _async_downgrade_q10_max_plus(
        self,
        robot: DiscoveredRobot,
        room: DiscoveredRoom,
        candidate: Mapping[str, Any],
    ) -> None:
        """Persist the safe Max fallback after a failed Q10 Max+ custom clean.

        This intentionally changes only the setting that supplied the failed
        effective fan speed.  It never retries the physical start: a failed
        write is safe to record, while a failed or uncertain start remains
        governed by the normal global scheduler halt.
        """

        source = str((candidate.get("profile_sources") or {}).get("fan_speed"))
        settings = (
            self._room_settings(room)
            if source == "room"
            else self._robot_settings(robot)
        )
        if settings.get("fan_speed") != "max_plus":
            return
        settings["fan_speed"] = "max"
        await self._async_save()
        async_sync_cleaning_program_issues(self)
        self._notify_listeners()
        _LOGGER.warning(
            "Adaptive RoboVacs changed a rejected Q10 Max+ profile to Max: robot=%s room=%s source=%s",
            robot.entity_id,
            room.name,
            source or "robot",
        )

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

            if active and pending_completion_is_docked(
                state_text, str(active.get("phase"))
            ):
                completion = _as_datetime(active.get("cleaning_finished")) or (
                    state.last_changed if state else now
                )
                confidence = (
                    str(active.get("completion_confidence", "observed"))
                    if active.get("cleaning_finished")
                    else "observed_pending_completion"
                )
                self._complete_job(entity_id, active, completion, confidence)
                self.data["robot_holds"].pop(entity_id, None)
                continue

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
                hold = self.data["robot_holds"].get(entity_id) or hold or {}
                outcome = offline_held_recovery_outcome(
                    state_text,
                    str(hold.get("phase")),
                    _as_datetime(active.get("last_observed_at"))
                    if active
                    else _as_datetime(hold.get("last_observed_at")),
                    tracked_expected_minutes,
                    now,
                )
                if outcome == "complete" and active:
                    expected_end = _as_datetime(active.get("expected_end"))
                    if expected_end:
                        self._complete_job(
                            entity_id,
                            active,
                            expected_end,
                            "recovered_expected_end",
                        )
                        self.data["robot_holds"].pop(entity_id, None)
                        continue
                if outcome == "cancelled":
                    if active:
                        self._cancel_job(
                            entity_id,
                            active,
                            now,
                            "recovered_physical_cancellation",
                        )
                    self._apply_robot_cancellation_deferral(entity_id, now)
                    self.data["robot_holds"].pop(entity_id, None)
                    continue
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
            if not active:
                continue
            if (
                active.get("source") in {"scheduler", "manual_dashboard"}
                and not active.get("seen_cleaning")
                and active.get("phase")
                in {"dispatching", "accepted", "start_outcome_uncertain"}
                and state_text not in {"cleaning", "returning"}
            ):
                robot = self.discovery.robots.get(entity_id)
                room = self.discovery.rooms.get(str(active.get("room", "")))
                if robot and room and not self.data.get("scheduler_fault"):
                    await self._async_latch_scheduler_fault(
                        robot,
                        room,
                        "start_outcome_uncertain",
                        "restart_recovery",
                        native_command_may_have_started=True,
                        outcome_uncertain=True,
                    )
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
            if active.get("seen_cleaning") and state and state.state == "docked":
                if expected_end and now >= expected_end:
                    self._complete_job(entity_id, active, expected_end, "recovered_expected_end")
                else:
                    self._set_recovery_waiting(entity_id, active, now)
                continue
            if active.get("seen_cleaning") and state and state.state == "idle":
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
        active.setdefault("adapter_id", "generic")
        active.setdefault("adapter_schema_version", 1)
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
        self._cancel_start_confirmation(robot_id)
        self._cancel_recovery_timer(robot_id)

    def _apply_robot_cancellation_deferral(self, robot_id: str, cancelled_at: datetime) -> list[str]:
        return self.jobs.rebase_cancelled_floor(robot_id, cancelled_at)

    def _active_rooms(self, active: dict[str, Any]) -> list[str]:
        return self.jobs.active_rooms(active)

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
            self._async_create_task(
                self.async_evaluate(dry_run=False, reason=f"recovery-end:{robot_id}")
            )
            return

        @callback
        def reconcile_at_expected_end(_when: datetime) -> None:
            self._recovery_timers.pop(robot_id, None)
            self._async_create_task(
                self.async_evaluate(dry_run=False, reason=f"recovery-end:{robot_id}")
            )

        self._recovery_timers[robot_id] = async_track_point_in_utc_time(
            self.hass, reconcile_at_expected_end, expected_end
        )

    @callback
    def _on_home_assistant_started(self, _event: Event) -> None:
        self._async_create_task(self.async_evaluate(dry_run=True, reason="ha_started"))

    @callback
    def _on_call_service(self, event: Event) -> None:
        """Capture explicit user room-clean service calls."""

        if event.data.get("domain") != "vacuum":
            return
        if event.data.get("service") == "clean_area":
            self._async_create_task(self._async_track_manual_service_call(event))

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
                self._effective_duration(
                    room,
                    "vacuum",
                    1,
                    self.robot_registry_id(request.robot_id),
                )[0]
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
            self._async_create_task(
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
            self._async_create_task(
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

    def _robot_ready(
        self, robot: DiscoveredRobot, *, ignore_scheduler_fault: bool = False
    ) -> tuple[bool, str]:
        settings = self._robot_settings(robot)
        if self.data.get("scheduler_fault") and not ignore_scheduler_fault:
            return False, "scheduler dispatch halted"
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
        if not state or not can_start_scheduled_clean(state.state):
            return False, f"robot is {state.state if state else 'unavailable'}"
        battery = self._robot_battery(robot)
        if battery is None:
            return False, "battery unavailable"
        if battery < float(settings.get("minimum_battery", DEFAULT_MINIMUM_BATTERY)):
            return False, "battery below minimum"
        return True, "ready"

    def _manual_robot_ready(self, robot: DiscoveredRobot) -> tuple[bool, str]:
        """Apply the intentionally minimal readiness check for a manual clean."""

        if not robot.supports_area_clean:
            return False, "does not support Home Assistant area cleaning"
        state = self.hass.states.get(robot.entity_id)
        state_text = state.state if state else None
        if not manual_clean_robot_is_docked(state_text):
            return False, f"robot is {state_text or 'unavailable'}"
        return True, "docked"

    def _mop_ready(self, robot: DiscoveredRobot) -> bool:
        """Return whether the selected adapter verifies a mop operation."""

        return "mop" in robot.adapter_capabilities.supported_operations

    def _room_due(
        self, room: DiscoveredRoom, operation: str, now: datetime
    ) -> datetime:
        del operation
        detail = self._room_data(room.area_id)
        settings = self._room_settings(room)
        return due_at(
            _as_datetime(detail.get("cleaning")),
            float(settings["cleaning_interval"]),
            _as_datetime(detail.get("defer", {}).get("cleaning")),
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
        """Apply the room's effective window unless it explicitly ignores it."""

        window = self._desired_window(room)
        return desired_window_allows(
            bool(self._room_settings(room).get("ignore_desired_window", False)),
            _local(now),
            window.start,
            window.end,
        )

    def _unresolved_allowed(self, room: DiscoveredRoom, now: datetime) -> bool:
        """Permit unresolved occupancy only inside the desired cleaning window."""

        window = self._desired_window(room)
        return unresolved_occupancy_allowed(
            str(self._room_data(room.area_id).get("occupancy")),
            room.is_bedroom_transit,
            _local(now),
            window.start,
            window.end,
        )

    def _manual_candidate(
        self,
        room: DiscoveredRoom,
        now: datetime,
        mode: str,
        context_id: str | None,
        user_id: str | None,
    ) -> dict[str, Any]:
        """Build an explicit user override without scheduler eligibility gates."""

        settings = self._room_settings(room)
        return {
            "room": room,
            "operation": "vacuum",
            "due_at": now,
            "confidence": 1.0,
            "reason": "manual override",
            "duration_minutes": float(settings["expected_minutes"]),
            "duration_sample_count": 0,
            "passes": 1,
            "occurrence": None,
            "evaluated_at": now,
            "unresolved_window_allowed": True,
            "bypass_forecast": True,
            "manual_override": True,
            "source": "manual_dashboard",
            "manual_mode": mode,
            "manual_context_id": context_id,
            "manual_user_id": user_id,
            "bypass_desired_window": True,
        }

    def _room_candidate(self, room: DiscoveredRoom, now: datetime) -> tuple[dict[str, Any] | None, str]:
        """Return a due room or a persisted occurrence awaiting its next stage."""

        settings = self._room_settings(room)
        detail = self._room_data(room.area_id)
        occurrence = self.data.get("occurrences", {}).get(room.area_id)
        manual_override = bool(occurrence and occurrence.get("manual_override"))
        if not settings.get("enabled", True) and not manual_override:
            self.data.get("water_notification_episodes", {}).pop(room.area_id, None)
            return None, "room disabled"
        if occurrence:
            if occurrence.get("source") == "manual_dashboard":
                due = _as_datetime(occurrence.get("scheduled_at")) or now
            else:
                due = max(
                    (
                        value
                        for value in (
                            _as_datetime(occurrence.get("scheduled_at")),
                            _as_datetime(detail.get("defer", {}).get("cleaning")),
                        )
                        if value is not None
                    ),
                    default=now,
                )
        else:
            due = self._room_due(room, "cleaning", now)
        if due > now:
            return None, "not due"
        if occurrence:
            confirmation = self.data.get("water_confirmations", {}).get(
                str(occurrence.get("occurrence_id"))
            )
            if confirmation and confirmation.get("status") == "pending":
                return None, "waiting for water confirmation"
        if not manual_override and detail.get("occupancy") == "occupied":
            return None, f"occupancy {detail.get('occupancy')} ({detail.get('source')})"
        bypass_desired_window = bool(
            occurrence and occurrence.get("bypass_desired_window", False)
        )
        if not manual_override:
            if not self._desired_window_allows(room, now):
                if not bypass_desired_window:
                    return None, "waiting for desired cleaning window"
        unresolved_window_allowed = self._unresolved_allowed(room, now)
        if manual_override:
            unresolved_window_allowed = True
        if (
            not manual_override
            and detail.get("occupancy") != "unoccupied"
            and not unresolved_window_allowed
        ):
            if detail.get("occupancy") == "unresolved":
                if room.is_bedroom_transit:
                    return None, "unresolved occupancy; bedroom-transit excluded"
                return None, "unresolved occupancy; waiting for desired cleaning window"
            return None, f"occupancy {detail.get('occupancy')} ({detail.get('source')})"
        if room.is_bedroom_transit and not manual_override:
            allowed, reason = self._hall_allowed(now)
            if not allowed:
                return None, reason
        operation = "vacuum"
        passes = 1
        if occurrence:
            stage_index = int(occurrence.get("current_stage", 0))
            stages = occurrence.get("stages", [])
            if stage_index >= len(stages):
                return None, "occurrence is complete"
            operation = str(stages[stage_index].get("operation", "vacuum"))
            passes = int(stages[stage_index].get("passes", 1))
        duration_minutes, duration_sample_count = self._effective_duration(
            room, operation, passes
        )
        forecast = (
            Forecast(True, 0.0, "unresolved occupancy desired-window policy")
            if unresolved_window_allowed
            else Forecast(True, 0.0, "awaiting robot-specific vacancy forecast")
        )
        return {
            "room": room,
            "operation": operation,
            "due_at": due,
            "confidence": forecast.confidence,
            "reason": forecast.reason,
            "duration_minutes": duration_minutes,
            "duration_sample_count": duration_sample_count,
            "passes": passes,
            "occurrence": occurrence,
            "evaluated_at": now,
            "unresolved_window_allowed": unresolved_window_allowed,
            "bypass_forecast": manual_override,
            "manual_override": manual_override,
            "source": occurrence.get("source", "scheduler") if occurrence else "scheduler",
            "manual_mode": occurrence.get("manual_mode") if occurrence else None,
            "manual_context_id": (
                occurrence.get("manual_context_id") if occurrence else None
            ),
            "bypass_desired_window": bypass_desired_window,
        }, "ready"

    def _candidate_for_robot(
        self, candidate: dict[str, Any], robot: DiscoveredRobot
    ) -> dict[str, Any] | None:
        """Resolve immutable ordered stages for one compatible robot."""

        room: DiscoveredRoom = candidate["room"]
        occurrence = candidate.get("occurrence")
        if occurrence:
            if (
                not candidate.get("manual_override")
                and occurrence.get("robot_registry_id") != robot.registry_id
            ):
                return None
            stage_index = int(occurrence.get("current_stage", 0))
            stages = occurrence.get("stages", [])
            if stage_index >= len(stages):
                return None
            stage = stages[stage_index]
            operation = str(stage.get("operation"))
            passes = int(stage.get("passes", 1))
            if not robot.adapter_capabilities.supports(operation, passes):
                return None
            resolved_profile = stage.get("cleaning_profile")
            if not isinstance(resolved_profile, dict) or not resolved_profile:
                resolved = resolve_cleaning_profile(
                    operation,
                    self._room_settings(room),
                    self._robot_settings(robot),
                    robot.adapter_capabilities,
                )
                if resolved is None:
                    return None
                resolved_profile = resolved.to_mapping()
            elif not cleaning_profile_is_supported(
                resolved_profile, robot.adapter_capabilities
            ):
                return None
            duration, count = self._effective_duration(
                room, operation, passes, robot.registry_id
            )
            forecast = (
                Forecast(True, 1.0, "manual override")
                if candidate.get("bypass_forecast")
                else Forecast(True, 0.0, "unresolved occupancy desired-window policy")
                if candidate.get("unresolved_window_allowed")
                else self._forecast(
                    room,
                    candidate.get("evaluated_at") or _now(),
                    duration,
                )
            )
            if not forecast.allowed:
                return None
            return {
                **candidate,
                "operation": operation,
                "passes": passes,
                "duration_minutes": duration,
                "duration_sample_count": count,
                "confidence": forecast.confidence,
                "reason": forecast.reason,
                "occurrence_id": occurrence.get("occurrence_id"),
                "stage_index": stage_index,
                "program": occurrence.get("program"),
                "resolved_profile": dict(resolved_profile),
                "requested_profile": dict(stage.get("requested_profile") or {}),
                "profile_sources": dict(stage.get("profile_sources") or {}),
                "source": occurrence.get("source", "scheduler"),
                "manual_mode": occurrence.get("manual_mode"),
                "manual_context_id": occurrence.get("manual_context_id"),
            }

        room_settings = self._room_settings(room)
        robot_settings = self._robot_settings(robot)
        manual_mode = candidate.get("manual_mode")
        program = (
            {"vacuum_only": "vacuum_only", "mop_only": "mop_only"}.get(
                str(manual_mode)
            )
            if manual_mode and manual_mode != "configured"
            else effective_cleaning_program(
                room_settings.get("cleaning_program"),
                str(robot_settings.get("cleaning_program", "vacuum_only")),
            )
        )
        operations = expand_cleaning_program(program or "")
        if room_settings.get("carpet"):
            operations = tuple(operation for operation in operations if operation != "mop")
        if not operations:
            return None
        stages: list[dict[str, Any]] = []
        for operation in operations:
            passes = stage_pass_count(
                operation,
                room_settings.get("vacuum_pass_count"),
                room_settings.get("mop_pass_count"),
                bool(robot_settings.get("double_pass")),
                bool(robot_settings.get("mop_double_pass")),
                robot.adapter_capabilities,
            )
            if passes is None or not robot.adapter_capabilities.supports(operation, passes):
                return None
            resolved_profile = resolve_cleaning_profile(
                operation,
                room_settings,
                robot_settings,
                robot.adapter_capabilities,
            )
            if resolved_profile is None:
                return None
            stages.append(
                {
                    "operation": operation,
                    "passes": passes,
                    "status": "pending",
                    "reason": None,
                    "started_at": None,
                    "completed_at": None,
                    "cleaning_profile": resolved_profile.to_mapping(),
                    "requested_profile": requested_cleaning_profile(
                        room_settings, robot_settings
                    ).to_mapping(),
                    "profile_sources": cleaning_profile_sources(room_settings),
                }
            )
        operation = str(stages[0]["operation"])
        passes = int(stages[0]["passes"])
        duration, count = self._effective_duration(
            room, operation, passes, robot.registry_id
        )
        forecast = (
            Forecast(True, 1.0, "manual override")
            if candidate.get("bypass_forecast")
            else Forecast(True, 0.0, "unresolved occupancy desired-window policy")
            if candidate.get("unresolved_window_allowed")
            else self._forecast(
                room,
                candidate.get("evaluated_at") or _now(),
                duration,
            )
        )
        if not forecast.allowed:
            return None
        return {
            **candidate,
            "operation": operation,
            "passes": passes,
            "duration_minutes": duration,
            "duration_sample_count": count,
            "confidence": forecast.confidence,
            "reason": forecast.reason,
            "program": program,
            "new_stages": stages,
            "stage_index": 0,
            "resolved_profile": dict(stages[0]["cleaning_profile"]),
            "requested_profile": dict(stages[0]["requested_profile"]),
            "profile_sources": dict(stages[0]["profile_sources"]),
            "source": candidate.get("source", "scheduler"),
            "manual_mode": manual_mode,
            "manual_context_id": candidate.get("manual_context_id"),
        }

    async def _async_refresh_pending_profile_if_needed(
        self, robot: DiscoveredRobot, candidate: dict[str, Any]
    ) -> dict[str, Any]:
        """Refresh one invalid scheduler profile only while its robot is docked."""

        try:
            validation = await self.runtime.async_validate_profile(robot, candidate)
        except Exception:
            # Dispatch performs and logs the definitive validation, including
            # its scheduler-fault handling.  Recovery must never hide that.
            return candidate
        if validation.ready or validation.code != "profile_option_unsupported":
            return candidate

        occurrence = candidate.get("occurrence")
        if not isinstance(occurrence, dict):
            return candidate
        stage_index = int(candidate.get("stage_index", occurrence.get("current_stage", 0)))
        stages = occurrence.get("stages", [])
        if stage_index >= len(stages) or not isinstance(stages[stage_index], dict):
            return candidate
        stage = stages[stage_index]
        robot_state = self.hass.states.get(robot.entity_id)
        if not can_refresh_pending_occurrence_profile(
            occurrence,
            stage,
            robot_state.state if robot_state else None,
            bool(self.data.get("active", {}).get(robot.entity_id)),
        ):
            return candidate

        prior_profile = {
            key: stage[key]
            for key in ("cleaning_profile", "requested_profile", "profile_sources")
            if key in stage
        }
        for key in prior_profile:
            stage.pop(key, None)
        refreshed = self._candidate_for_robot(candidate, robot)
        if refreshed is None:
            stage.update(prior_profile)
            return candidate

        stage["cleaning_profile"] = dict(refreshed["resolved_profile"])
        stage["requested_profile"] = dict(refreshed.get("requested_profile") or {})
        stage["profile_sources"] = dict(refreshed.get("profile_sources") or {})
        await self._async_save()
        _LOGGER.info(
            "Adaptive RoboVacs refreshed an unsupported pending profile: robot=%s room=%s",
            robot.entity_id,
            candidate["room"].area_id,
        )
        return refreshed

    def _skip_occurrence_stage(
        self,
        area_id: str,
        stage_index: int,
        outcome: str,
        reason: str,
        when: datetime,
    ) -> bool:
        """Make one current stage terminal and finish cadence when appropriate."""

        occurrence = self.data.get("occurrences", {}).get(area_id)
        if not occurrence or int(occurrence.get("current_stage", 0)) != stage_index:
            return False
        stages = occurrence.get("stages", [])
        if stage_index >= len(stages) or stages[stage_index].get("status") != "pending":
            return False
        stage = stages[stage_index]
        stage["status"] = outcome
        stage["reason"] = reason
        stage["completed_at"] = _iso(when)
        occurrence["current_stage"] = stage_index + 1
        detail = self._room_data(area_id)
        detail["last_stage_outcome"] = outcome
        detail["last_stage_reason"] = reason
        detail["last_stage_at"] = _iso(when)
        if int(occurrence["current_stage"]) >= len(stages):
            if occurrence.get("source") == "manual_dashboard":
                if any(item.get("status") == "completed" for item in stages):
                    detail["cleaning"] = _iso(when)
                self._record_manual_event(
                    {
                        "at": _iso(when),
                        "robot": occurrence.get("robot_entity_id"),
                        "rooms": [area_id],
                        "operations": [
                            item.get("operation") for item in stages
                        ],
                        "context_id": occurrence.get("manual_context_id"),
                        "mode": occurrence.get("manual_mode"),
                        "outcome": outcome,
                        "reason": reason,
                        "source": "manual_dashboard",
                    }
                )
            else:
                detail["cleaning"] = _iso(when)
            self.data["occurrences"].pop(area_id, None)
            self.data.get("water_confirmations", {}).pop(
                str(occurrence.get("occurrence_id")), None
            )
        return True

    async def _async_notify_mop_skipped(
        self,
        room: DiscoveredRoom,
        robot: DiscoveredRobot,
        reason: str,
        occurrence: dict[str, Any],
        now: datetime,
    ) -> None:
        episodes = self.data.setdefault("water_notification_episodes", {})
        episode = episodes.get(room.area_id)
        last_sent = _as_datetime(episode.get("last_sent_at")) if episode else None
        if episode and episode.get("reason") == reason and last_sent and now - last_sent < timedelta(hours=24):
            return
        first_sent = episode.get("first_sent_at") if episode and episode.get("reason") == reason else _iso(now)
        episodes[room.area_id] = {
            "room_id": room.area_id,
            "reason": reason,
            "first_sent_at": first_sent,
            "last_sent_at": _iso(now),
        }
        vacuum_ran = any(
            stage.get("operation") == "vacuum" and stage.get("status") == "completed"
            for stage in occurrence.get("stages", [])
        )
        vacuum_scheduled = any(
            stage.get("operation") == "vacuum"
            for stage in occurrence.get("stages", [])
        )
        vacuum_message = (
            "Vacuuming completed. "
            if vacuum_ran
            else "Vacuuming remains scheduled. "
            if vacuum_scheduled
            else "No vacuum stage was scheduled. "
        )
        await self._async_save()
        await self._async_send_mobile_notification(
            {
                "title": "Adaptive RoboVacs skipped mopping",
                "message": (
                    f"{robot.name} could not mop {room.name} because water was not ready. "
                    + vacuum_message
                    + "Mopping will be tried at the next scheduled clean."
                ),
                "data": {
                    "channel": "Adaptive RoboVacs - Mop skipped",
                    "tag": f"adaptive_robovacs_mop_skipped_{self.entry.entry_id}_{room.area_id}",
                },
            }
        )

    async def _async_prepare_occurrence(
        self, robot: DiscoveredRobot, candidate: dict[str, Any], now: datetime
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Persist a due occurrence and satisfy the current mop water gate."""

        room: DiscoveredRoom = candidate["room"]
        occurrence = self.data.setdefault("occurrences", {}).get(room.area_id)
        if occurrence is None:
            occurrence_id = secrets.token_hex(12)
            occurrence = {
                "occurrence_id": occurrence_id,
                "room_id": room.area_id,
                "robot_registry_id": robot.registry_id,
                "robot_entity_id": robot.entity_id,
                "program": candidate["program"],
                "stages": candidate["new_stages"],
                "scheduled_at": _iso(candidate["due_at"]),
                "created_at": _iso(now),
                "adapter_id": robot.adapter_id,
                "adapter_schema_version": robot.adapter_schema_version,
                "current_stage": 0,
                "source": candidate.get("source", "scheduler"),
                "manual_mode": candidate.get("manual_mode"),
                "manual_override": bool(candidate.get("manual_override")),
                "bypass_desired_window": bool(
                    candidate.get("bypass_desired_window", False)
                ),
                "manual_context_id": candidate.get("manual_context_id"),
                "manual_user_id": candidate.get("manual_user_id"),
            }
            self.data["occurrences"][room.area_id] = occurrence
            candidate = {
                **candidate,
                "occurrence": occurrence,
                "occurrence_id": occurrence_id,
                "stage_index": 0,
            }
            await self._async_save()
        else:
            candidate = {
                **candidate,
                "occurrence": occurrence,
                "occurrence_id": occurrence["occurrence_id"],
                "stage_index": int(occurrence.get("current_stage", 0)),
            }

        if candidate["operation"] != "mop":
            return candidate, None
        if self._room_settings(room).get("carpet"):
            self._skip_occurrence_stage(
                room.area_id,
                int(candidate["stage_index"]),
                "skipped_no_mop",
                "room_is_carpeted",
                now,
            )
            await self._async_save()
            self._async_create_task(
                self.async_evaluate(reason="mop-stage-skipped-carpet")
            )
            return None, f"skipped mopping {room.name}: room excludes mopping"
        water = robot.adapter_capabilities.water_readiness
        if water.status == "sensor_ready" and water.ready:
            self.data.get("water_notification_episodes", {}).pop(room.area_id, None)
            return candidate, None
        if water.status == "sensor_blocked":
            occurrence_snapshot = {**occurrence, "stages": [dict(item) for item in occurrence["stages"]]}
            self._skip_occurrence_stage(
                room.area_id,
                int(candidate["stage_index"]),
                "skipped_no_water",
                water.reason,
                now,
            )
            await self._async_save()
            await self._async_notify_mop_skipped(
                room, robot, water.reason, occurrence_snapshot, now
            )
            self._async_create_task(
                self.async_evaluate(reason="mop-stage-skipped-no-water")
            )
            return None, f"skipped mopping {room.name}: water unavailable"
        if water.status != "confirmation_required":
            return None, "mopping is not supported"

        confirmations = self.data.setdefault("water_confirmations", {})
        request = confirmations.get(str(occurrence["occurrence_id"]))
        if request:
            expires = _as_datetime(request.get("expires_at")) or now
            if request.get("status") == "confirmed" and now < expires:
                return {**candidate, "water_confirmed": True}, None
            if request.get("status") in {"pending", "confirmed"} and now >= expires:
                request["status"] = "expired"
                request["responded_at"] = _iso(now)
                self._skip_occurrence_stage(
                    room.area_id,
                    int(candidate["stage_index"]),
                    "skipped_unconfirmed_water",
                    "water_confirmation_expired",
                    now,
                )
                await self._async_save()
                self._async_create_task(
                    self.async_evaluate(reason="water-confirmation-expired")
                )
                return None, "mopping cancelled: water confirmation expired"
            return None, "waiting for water confirmation"

        request_id = secrets.token_hex(12)
        confirm_action = f"ARV_CONFIRM_WATER_{secrets.token_urlsafe(24)}"
        cancel_action = f"ARV_CANCEL_MOP_{secrets.token_urlsafe(24)}"
        expires = now + timedelta(hours=1)
        tag = f"adaptive_robovacs_mop_confirm_{self.entry.entry_id}_{request_id}"
        request = {
            "request_id": request_id,
            "occurrence_id": occurrence["occurrence_id"],
            "room_id": room.area_id,
            "robot_registry_id": robot.registry_id,
            "stage_index": int(candidate["stage_index"]),
            "confirm_hash": self._action_hash(confirm_action),
            "cancel_hash": self._action_hash(cancel_action),
            "tag": tag,
            "sent_at": _iso(now),
            "expires_at": _iso(expires),
            "status": "pending",
            "responded_at": None,
        }
        confirmations[str(occurrence["occurrence_id"])] = request
        await self._async_save()
        delivered, total = await self._async_send_mobile_notification(
            {
                "title": f"{robot.name} wants to mop",
                "message": (
                    f"{robot.name} wants to mop {room.name}, but cannot check whether water "
                    "is onboard. Confirm water before mopping starts."
                ),
                "data": {
                    "channel": "Adaptive RoboVacs - Mop confirmation",
                    "tag": tag,
                    "timeout": 3600,
                    "adaptive_robovacs_request_id": request_id,
                    "actions": [
                        {"action": confirm_action, "title": "Confirm water", "authenticationRequired": True},
                        {"action": cancel_action, "title": "Cancel mopping", "authenticationRequired": True},
                    ],
                },
            }
        )
        if delivered == 0:
            request["status"] = "cancelled"
            request["responded_at"] = _iso(now)
            self._skip_occurrence_stage(
                room.area_id,
                int(candidate["stage_index"]),
                "skipped_unconfirmed_water",
                "water_confirmation_delivery_failed",
                now,
            )
            await self._async_save()
            self._async_create_task(
                self.async_evaluate(reason="water-confirmation-unreachable")
            )
            return None, "mopping cancelled: no notification target"
        if delivered < total:
            _LOGGER.warning(
                "Adaptive RoboVacs water confirmation reached %s of %s notification targets",
                delivered,
                total,
            )
        self._schedule_water_confirmation(request)
        return None, "waiting for water confirmation"

    async def _async_handle_mop_preflight_blocked(
        self,
        robot: DiscoveredRobot,
        candidate: dict[str, Any],
        reason: str,
        now: datetime,
    ) -> None:
        """Treat a just-in-time water block as a normal terminal mop skip."""

        room: DiscoveredRoom = candidate["room"]
        occurrence = self.data.get("occurrences", {}).get(room.area_id)
        if not occurrence:
            return
        snapshot = {**occurrence, "stages": [dict(item) for item in occurrence["stages"]]}
        self._skip_occurrence_stage(
            room.area_id,
            int(candidate.get("stage_index", occurrence.get("current_stage", 0))),
            "skipped_no_water",
            reason,
            now,
        )
        await self._async_save()
        await self._async_notify_mop_skipped(room, robot, reason, snapshot, now)
        self._async_create_task(
            self.async_evaluate(reason="mop-final-preflight-skipped")
        )

    async def _async_apply_profile(
        self, robot: DiscoveredRobot, operation: str, passes: int = 1
    ) -> None:
        await self.runtime.async_apply_profile(robot, operation, passes)

    async def _async_dispatch(
        self, robot: DiscoveredRobot, candidate: dict[str, Any], now: datetime
    ) -> tuple[bool, str]:
        if self._closing:
            return False, "coordinator shutting down"
        return await self.runtime.async_dispatch(robot, candidate, now)

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

            if (
                active
                and active.get("source") in {"scheduler", "manual_dashboard"}
                and active.get("phase") == "accepted"
                and not active.get("seen_cleaning")
            ):
                accepted_at = _as_datetime(
                    active.get("accepted_at") or active.get("started")
                )
                immediate_failure = state_text in {
                    "paused",
                    "error",
                    "returning",
                    "unavailable",
                    "unknown",
                }
                confirmation_expired = bool(
                    state_text != "cleaning"
                    and accepted_at
                    and now - accepted_at >= START_CONFIRMATION_TIMEOUT
                )
                if immediate_failure or confirmation_expired:
                    robot = self.discovery.robots.get(robot_id)
                    room = self.discovery.rooms.get(str(active.get("room", "")))
                    if robot and room:
                        if active.get("q10_max_plus_fallback"):
                            await self._async_downgrade_q10_max_plus(robot, room, active)
                        uncertain = state_text not in {"docked", "idle"}
                        await self._async_latch_scheduler_fault(
                            robot,
                            room,
                            (
                                "start_outcome_uncertain"
                                if uncertain
                                else "start_confirmation_failed"
                            ),
                            "start_confirmation",
                            native_command_may_have_started=uncertain,
                            outcome_uncertain=uncertain,
                        )
                        changed = True
                        continue

            if active and pending_completion_is_docked(
                state_text, str(active.get("phase"))
            ):
                completion = _as_datetime(active.get("cleaning_finished")) or (
                    state.last_changed if state else now
                )
                confidence = (
                    str(active.get("completion_confidence", "observed"))
                    if active.get("cleaning_finished")
                    else "observed_pending_completion"
                )
                self._complete_job(robot_id, active, completion, confidence)
                self.data["robot_holds"].pop(robot_id, None)
                changed = True
                continue

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
            if active.get("seen_cleaning") and state_text == "docked":
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
                    # Scheduler-owned jobs are handled by the bounded confirmation
                    # branch above. This legacy fallback is retained for malformed
                    # checkpoints that predate accepted_at.
                    continue
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
        self.jobs.cancel(robot_id, active, cancelled_at, reason)

    def _complete_job(self, robot_id: str, active: dict[str, Any], completion: datetime, confidence: str) -> None:
        self.jobs.complete(robot_id, active, completion, confidence)

    def _apply_manual_deferral(
        self,
        robot_entity_id: str,
        area_ids: list[str],
        operations: list[str],
        completed_at: datetime,
    ) -> list[str]:
        return self.jobs.apply_manual_deferral(robot_entity_id, area_ids, operations, completed_at)

    def _record_manual_event(self, event: dict[str, Any]) -> None:
        self.jobs.record_manual_event(event)

    async def async_evaluate(
        self,
        dry_run: bool = False,
        reason: str = "manual",
        transition: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Refresh state, publish a safe preview, and optionally dispatch work."""

        if self._closing:
            return {
                **self.data.get("last_preview", {}),
                "dispatches": ["coordinator shutting down"],
            }
        async with self._lock:
            if self._closing:
                return {
                    **self.data.get("last_preview", {}),
                    "dispatches": ["coordinator shutting down"],
                }
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
                resolved = [
                    (robot, resolved_candidate)
                    for robot in self.discovery.robots.values()
                    if robot.floor_id == room.floor_id
                    and robot.entity_id not in used_robots
                    and (
                        self._manual_robot_ready(robot)[0]
                        if candidate.get("manual_override")
                        else robot_ready[robot.entity_id][0]
                    )
                    and (resolved_candidate := self._candidate_for_robot(candidate, robot))
                    is not None
                ]
                if not resolved:
                    reasons.setdefault(room.area_id, "no ready compatible robot")
                    continue
                resolved.sort(
                    key=lambda item: (
                        self._robot_battery(item[0]) or 0,
                        item[0].entity_id,
                    ),
                    reverse=True,
                )
                robot, candidate = resolved[0]
                assignments.append((robot, candidate))
                used_robots.add(robot.entity_id)

            preview = {
                "at": _iso(now),
                "reason": reason,
                "observe_only": self.observe_only,
                "party_mode": self.party_mode,
                "dispatch_halted": bool(self.data.get("scheduler_fault")),
                "scheduler_fault": self.scheduler_fault_view(),
                "candidates": [
                    {
                        "room": item["room"].area_id,
                        "room_name": item["room"].name,
                        "operation": (
                            item["operation"] if item.get("occurrence") else "cleaning"
                        ),
                        "due_at": _iso(item["due_at"]),
                        "confidence": item["confidence"],
                        "basis": item["reason"],
                        "passes": item["passes"],
                    }
                    for item in candidates
                ],
                "assignments": [
                    {
                        "robot": robot.entity_id,
                        "room": item["room"].area_id,
                        "operation": item["operation"],
                        "program": item.get("program"),
                        "stage_index": item.get("stage_index"),
                        "passes": item["passes"],
                        "adapter_id": robot.adapter_id,
                        "cleaning_profile": item.get("resolved_profile"),
                        "source": item.get("source", "scheduler"),
                    }
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
            if (
                not dry_run
                and not self._closing
                and not self.observe_only
                and not self.party_mode
            ):
                for robot, candidate in assignments:
                    if self._closing:
                        break
                    if self.data.get("scheduler_fault") and not candidate.get(
                        "manual_override"
                    ):
                        dispatches.append("scheduler dispatch halted")
                        continue
                    # Earlier assignments may have awaited service calls. Recheck
                    # every room and robot gate before creating an occurrence or
                    # sending a water-confirmation notification.
                    prepare_now = _now()
                    self._observe_occupancy(prepare_now)
                    prepare_candidate, prepare_reason = self._room_candidate(
                        candidate["room"], prepare_now
                    )
                    robot_is_ready, robot_reason = (
                        self._manual_robot_ready(robot)
                        if candidate.get("manual_override")
                        else self._robot_ready(robot)
                    )
                    prepare_resolved = (
                        self._candidate_for_robot(prepare_candidate, robot)
                        if prepare_candidate and robot_is_ready
                        else None
                    )
                    if prepare_resolved is None:
                        wait_reason = (
                            prepare_reason
                            if not prepare_candidate
                            else robot_reason
                            if not robot_is_ready
                            else "cleaning program or vacancy forecast is no longer compatible"
                        )
                        dispatches.append(
                            f"waiting for {candidate['room'].name}: {wait_reason}"
                        )
                        continue
                    candidate = prepare_resolved
                    prepared, preparation_message = await self._async_prepare_occurrence(
                        robot, candidate, prepare_now
                    )
                    if prepared is None:
                        if preparation_message:
                            dispatches.append(preparation_message)
                        continue
                    # Service calls for an earlier assignment can take time. Refresh
                    # every physical safety gate immediately before this command so
                    # stage two never inherits stage one's eligibility.
                    dispatch_now = _now()
                    self._observe_occupancy(dispatch_now)
                    fresh_candidate, fresh_reason = self._room_candidate(
                        candidate["room"], dispatch_now
                    )
                    robot_is_ready, robot_reason = (
                        self._manual_robot_ready(robot)
                        if candidate.get("manual_override")
                        else self._robot_ready(robot)
                    )
                    fresh_resolved = (
                        self._candidate_for_robot(fresh_candidate, robot)
                        if fresh_candidate and robot_is_ready
                        else None
                    )
                    if fresh_resolved is None:
                        wait_reason = (
                            fresh_reason
                            if not fresh_candidate
                            else robot_reason
                            if not robot_is_ready
                            else "cleaning program is no longer compatible"
                        )
                        dispatches.append(
                            f"waiting for {candidate['room'].name}: "
                            f"{wait_reason}"
                        )
                        continue
                    if prepared.get("water_confirmed"):
                        fresh_resolved["water_confirmed"] = True
                    fresh_resolved = await self._async_refresh_pending_profile_if_needed(
                        robot, fresh_resolved
                    )
                    ok, message = await self._async_dispatch(
                        robot, fresh_resolved, dispatch_now
                    )
                    dispatches.append(message)
                    if not ok:
                        _LOGGER.warning("Adaptive RoboVacs: %s", message)
                        break
            elif self.data.get("scheduler_fault"):
                dispatches.append("scheduler dispatch halted")
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

    async def async_manual_clean_room(
        self,
        area_id: str,
        mode: str = "configured",
        *,
        context_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Start one explicit dashboard room-clean override immediately."""

        if mode not in {"configured", "vacuum_only", "mop_only"}:
            raise ValueError(f"Unknown manual cleaning mode: {mode}")
        if self._closing:
            return {"accepted": False, "status": "rejected", "reason": "coordinator shutting down"}
        async with self._lock:
            now = _now()
            await self.async_refresh_discovery()
            self._observe_occupancy(now)
            await self._async_reconcile_jobs(now)
            room = self.discovery.rooms.get(area_id)
            event = {
                "at": _iso(now),
                "robot": None,
                "rooms": [area_id],
                "operations": [],
                "context_id": context_id,
                "user_id": user_id,
                "mode": mode,
                "source": "manual_dashboard",
            }

            async def reject(reason: str) -> dict[str, Any]:
                self._record_manual_event({**event, "outcome": "rejected", "reason": reason})
                await self._async_save()
                self._notify_listeners()
                return {"accepted": False, "status": "rejected", "reason": reason}

            if room is None:
                return await reject("room is not discovered by this config entry")
            settings = self._room_settings(room)
            if mode == "mop_only" and settings.get("carpet"):
                return await reject("room excludes mopping")
            if self.observe_only:
                return await reject("observe-only mode")
            if self.party_mode:
                return await reject("party mode")
            base_candidate = self._manual_candidate(
                room, now, mode, context_id, user_id
            )
            resolved: list[tuple[DiscoveredRobot, dict[str, Any]]] = []
            readiness: list[str] = []
            for robot in self.discovery.robots.values():
                if robot.floor_id != room.floor_id:
                    continue
                ready, reason = self._manual_robot_ready(robot)
                if not ready:
                    readiness.append(f"{robot.name}: {reason}")
                    continue
                candidate = self._candidate_for_robot(base_candidate, robot)
                if candidate is not None:
                    resolved.append((robot, candidate))
            if not resolved:
                reason = (
                    "; ".join(readiness)
                    if readiness
                    else "no ready robot has a compatible cleaning profile"
                )
                return await reject(reason)
            resolved.sort(
                key=lambda item: (
                    self._robot_battery(item[0]) or 0,
                    item[0].entity_id,
                ),
                reverse=True,
            )
            robot, candidate = resolved[0]
            prior_occurrence = self.data.get("occurrences", {}).pop(area_id, None)
            if prior_occurrence:
                self.data.get("water_confirmations", {}).pop(
                    str(prior_occurrence.get("occurrence_id")), None
                )
            event["robot"] = robot.entity_id
            event["operations"] = [
                stage["operation"] for stage in candidate.get("new_stages", [])
            ]
            self._record_manual_event({**event, "outcome": "requested"})

            prepared, message = await self._async_prepare_occurrence(robot, candidate, now)
            if prepared is None:
                occurrence = self.data.get("occurrences", {}).get(area_id)
                if occurrence:
                    self._record_manual_event(
                        {
                            **event,
                            "outcome": "awaiting_confirmation"
                            if self.data.get("water_confirmations", {}).get(
                                str(occurrence.get("occurrence_id"))
                            )
                            else "accepted",
                            "reason": message,
                        }
                    )
                    await self._async_save()
                    self._notify_listeners()
                    return {
                        "accepted": True,
                        "status": "pending",
                        "reason": message,
                        "robot_entity_id": robot.entity_id,
                    }
                self._record_manual_event(
                    {**event, "outcome": "rejected", "reason": message}
                )
                await self._async_save()
                self._notify_listeners()
                return {"accepted": False, "status": "rejected", "reason": message}

            dispatch_now = _now()
            self._observe_occupancy(dispatch_now)
            fresh = {**prepared, "evaluated_at": dispatch_now}
            robot_ready, robot_reason = self._manual_robot_ready(robot)
            fresh_resolved = (
                self._candidate_for_robot(fresh, robot)
                if robot_ready
                else None
            )
            if fresh_resolved is None:
                occurrence = self.data.get("occurrences", {}).pop(area_id, None)
                if occurrence:
                    self.data.get("water_confirmations", {}).pop(
                        str(occurrence.get("occurrence_id")), None
                    )
                return await reject(
                    robot_reason if not robot_ready else "cleaning profile is no longer compatible"
                )
            if prepared.get("water_confirmed"):
                fresh_resolved["water_confirmed"] = True
            fresh_resolved = await self._async_refresh_pending_profile_if_needed(
                robot, fresh_resolved
            )
            changed_global_gate = (
                "coordinator shutting down"
                if self._closing
                else "observe-only mode"
                if self.observe_only
                else "party mode"
                if self.party_mode
                else None
            )
            if changed_global_gate:
                failed_occurrence = self.data.get("occurrences", {}).pop(area_id, None)
                if failed_occurrence:
                    self.data.get("water_confirmations", {}).pop(
                        str(failed_occurrence.get("occurrence_id")), None
                    )
                return await reject(changed_global_gate)
            ok, dispatch_message = await self._async_dispatch(
                robot, fresh_resolved, dispatch_now
            )
            if not ok:
                self._record_manual_event(
                    {**event, "outcome": "failed", "reason": dispatch_message}
                )
                if not self.data.get("active", {}).get(robot.entity_id):
                    failed_occurrence = self.data.get("occurrences", {}).pop(
                        area_id, None
                    )
                    if failed_occurrence:
                        self.data.get("water_confirmations", {}).pop(
                            str(failed_occurrence.get("occurrence_id")), None
                        )
            await self._async_save()
            self._notify_listeners()
            return {
                "accepted": ok,
                "status": "started" if ok else "failed",
                "reason": dispatch_message,
                "robot_entity_id": robot.entity_id,
            }

    def room_state(self, area_id: str) -> dict[str, Any]:
        return room_state(self, area_id)

    def robot_state(self, entity_id: str) -> dict[str, Any]:
        return robot_state(self, entity_id)
