"""Typed scheduler settings, floor-plan, and configuration state access."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ..discovery import DiscoveredRobot, DiscoveredRoom, DiscoverySnapshot
from ..floor_plans import FloorPlanWrite, replace_floor_plan, replace_room_adjacency
from ..models import (
    ROOM_PROFILE_OVERRIDE_KEYS,
    AdjacencyMode,
    CleaningProgram,
    JobPhase,
    ResolvedDailyWindow,
    is_native_mop_profile_value,
    is_valid_daily_time,
    mop_stage_start_is_observed,
    native_mop_profile_default_migration,
    resolve_daily_window,
    room_cleaning_period,
    room_cleaning_period_update,
    room_cleaning_profile,
    room_cleaning_profile_update,
)
from ..planner import ScheduleCandidate
from ..presentation import floor_plan_attributes
from ..projections import ProjectionSource
from ..projections import floor_plan_view as build_floor_plan_view
from ..state import (
    ActiveJob,
    FloorPlanState,
    RobotSettings,
    RoomHistory,
    RoomSettings,
    SchedulerState,
)

_LOGGER = logging.getLogger(__name__)


def _now() -> datetime:
    from . import core

    return core._now()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class ApplicationSettingsMixin:
    """Expose typed settings and atomically mutate durable configuration."""

    hass: HomeAssistant
    entry: ConfigEntry
    state: SchedulerState
    discovery: DiscoverySnapshot
    _closing: bool
    _storage_safe_mode: bool
    _startup_state_settle_until: datetime | None
    _identity_migrated: bool
    _lock: asyncio.Lock

    if TYPE_CHECKING:

        async def _async_save(self) -> None: ...

        async def async_refresh_discovery(self, *, notify: bool = True) -> None: ...

        def _notify_listeners(self) -> None: ...

        async def async_evaluate(
            self, dry_run: bool = False, reason: str = "manual"
        ) -> dict[str, Any]: ...

        def scheduler_fault_view(self) -> dict[str, Any] | None: ...

        def _fault_views(self, section: str) -> list[dict[str, Any]]: ...

        def _sync_cleaning_program_issues(self) -> None: ...

        def _cancel_start_confirmation(self, robot_id: str) -> None: ...

    def _room_data(self, area_id: str) -> RoomHistory:
        return self.state.room_history.setdefault(area_id, RoomHistory())

    def _room_settings(self, room: DiscoveredRoom) -> RoomSettings:
        return self.state.room_settings.setdefault(
            room.area_id,
            RoomSettings.defaults(room.is_bedroom),
        )

    def _desired_window(self, room: DiscoveredRoom) -> ResolvedDailyWindow:
        """Resolve one room's independently inherited daily window."""

        settings = self._room_settings(room)
        return resolve_daily_window(
            settings.desired_window_start,
            settings.desired_window_end,
            self.state.global_settings.unresolved_start,
            self.state.global_settings.unresolved_end,
        )

    def _robot_settings(self, robot: DiscoveredRobot) -> RobotSettings:
        settings = self.state.robot_settings.setdefault(
            robot.registry_id,
            RobotSettings.defaults(
                "mop" in robot.adapter_capabilities.supported_operations
            ),
        )
        if (
            not settings.cleaning_depth_configured
            and settings.cleaning_depth is not None
        ):
            settings.cleaning_depth_configured = True
        elif (
            not settings.cleaning_depth_configured
            and robot.adapter_capabilities.cleaning_depth_options
        ):
            settings.cleaning_depth = "daily"
            settings.cleaning_depth_configured = True
        migration = (
            native_mop_profile_default_migration(settings)
            if robot.adapter_capabilities.native_mop_profile
            else None
        )
        if migration is not None:
            settings.direct_custom_mop_migrated = bool(
                migration["direct_custom_mop_migrated"]
            )
            if "mop_mode" in migration:
                settings.mop_mode = str(migration["mop_mode"])
            if "mop_intensity" in migration:
                settings.mop_intensity = str(migration["mop_intensity"])
            self._identity_migrated = True
        return settings

    def robot_unique_fragment(self, entity_id: str) -> str:
        """Return the original entity-ID fragment retained for stable unique IDs."""

        robot = self.discovery.robots.get(entity_id)
        if robot is None:
            return entity_id
        return str(
            self.state.robot_entity_aliases.get(robot.registry_id, robot.entity_id)
        )

    def robot_registry_id(self, entity_id: str) -> str:
        """Resolve a current runtime entity ID to durable registry identity."""

        robot = self.discovery.robots.get(entity_id)
        return robot.registry_id if robot else entity_id

    @property
    def observe_only(self) -> bool:
        return self._storage_safe_mode or self.state.global_settings.observe_only

    @property
    def party_mode(self) -> bool:
        return self.state.global_settings.party_mode

    @property
    def scheduler_halted(self) -> bool:
        """Retain the legacy accessor; scoped faults limit, not halt, dispatch."""

        return self.scheduler_limited

    @property
    def scheduler_limited(self) -> bool:
        return bool(
            self.state.robot_faults
            or self.state.room_faults
            or self.state.room_recoveries
        )

    @property
    def storage_safe_mode(self) -> bool:
        """Return whether persisted data failed validation and dispatch is disabled."""

        return self._storage_safe_mode

    def get_global_setting(self, key: str) -> Any:
        """Return a global control value without exposing mutable Store data."""

        if key not in {
            "observe_only",
            "party_mode",
            "forecast_confidence",
            "unresolved_start",
            "unresolved_end",
            "adjacency_night_start",
            "adjacency_night_end",
        }:
            raise ValueError(f"Unknown global setting: {key}")
        if key == "observe_only":
            return self.observe_only
        return getattr(self.state.global_settings, key)

    def get_room_setting(self, area_id: str, key: str) -> Any:
        """Return one discovered room setting without exposing mutable state."""

        room = self.discovery.rooms.get(area_id)
        if room is None:
            raise ValueError(f"Unknown room area: {area_id}")
        if key not in {
            "enabled",
            "adjacency_mode",
            "cleaning_interval",
            "vacuum_interval",
            "expected_minutes",
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
        aliases = {
            "vacuum_interval": "cleaning_interval",
            "pass_count": "vacuum_pass_count",
        }
        return getattr(self._room_settings(room), aliases.get(key, key))

    def room_cleaning_period(self, area_id: str) -> str:
        """Return one room's mobile-friendly schedule choice."""

        room = self.discovery.rooms.get(area_id)
        if room is None:
            raise ValueError(f"Unknown room area: {area_id}")
        return room_cleaning_period(self._room_settings(room))

    def room_cleaning_profile(self, area_id: str) -> str:
        """Return whether one room exposes detailed profile overrides."""

        room = self.discovery.rooms.get(area_id)
        if room is None:
            raise ValueError(f"Unknown room area: {area_id}")
        return room_cleaning_profile(self._room_settings(room))

    def scheduler_summary(self) -> dict[str, Any]:
        """Return the scheduler metadata used by the status sensor."""

        return {
            "last_evaluation": _iso(self.state.evaluation.last_evaluation_at),
            "preview": self.state.evaluation.last_preview.to_mapping(),
            "scheduler_fault": self.scheduler_fault_view(),
            "robot_faults": self._fault_views("robot_faults"),
            "room_faults": self._fault_views("room_faults"),
            "floor_plan": self.floor_plan_view(),
        }

    def _floor_plan(self) -> FloorPlanState:
        """Decode the current durable graph before a topology mutation."""

        return self.state.floor_plan

    def floor_plan_view(self) -> dict[str, Any]:
        """Return a card-safe floor-plan projection built from live discovery."""

        # The concrete application supplies the remaining projection methods
        # through its policy/job mixins; mypy cannot infer the composed MRO.
        source = cast(ProjectionSource, self)
        return floor_plan_attributes(build_floor_plan_view(source))

    def _require_floor_plan_write_ready(self) -> None:
        if self._closing:
            raise ValueError("Adaptive RoboVacs is shutting down")
        if self._storage_safe_mode:
            raise ValueError("floor-plan changes are disabled while storage is unsafe")

    async def async_save_floor_plan(
        self,
        request: FloorPlanWrite,
    ) -> dict[str, Any]:
        """Atomically replace one floor's live layout, markers, and links."""

        async with self._lock:
            self._require_floor_plan_write_ready()
            self.state.floor_plan = replace_floor_plan(
                self._floor_plan(),
                request,
                room_floor_by_id={
                    area_id: room.floor_id
                    for area_id, room in self.discovery.rooms.items()
                },
                sensor_owner_by_registry_id={
                    source.registry_id: room.area_id
                    for room in self.discovery.rooms.values()
                    for source in room.occupancy_sources
                },
            )
            await self._async_save()
        await self.async_evaluate(dry_run=True, reason="floor_plan")
        return self.floor_plan_view()

    async def async_set_room_adjacency(
        self, area_id: str, neighbor_area_ids: list[str]
    ) -> dict[str, Any]:
        """Replace one room's direct same-floor neighbours for scripts."""

        async with self._lock:
            self._require_floor_plan_write_ready()
            self.state.floor_plan = replace_room_adjacency(
                self._floor_plan(),
                area_id,
                tuple(neighbor_area_ids),
                room_floor_by_id={
                    room_id: room.floor_id
                    for room_id, room in self.discovery.rooms.items()
                },
            )
            await self._async_save()
        await self.async_evaluate(dry_run=True, reason="room_adjacency")
        return self.floor_plan_view()

    def _mop_washing_is_observed(
        self, robot: DiscoveredRobot | None, active: ActiveJob | None
    ) -> bool:
        """Return whether one accepted Roborock Mop command is washing first."""

        if (
            robot is None
            or active is None
            or active.seen_cleaning
            or active.phase not in {"accepted", "mop_washing"}
        ):
            return False
        readiness_entity_id = robot.adapter_capabilities.readiness_entity_id
        detailed_state = (
            self.hass.states.get(readiness_entity_id) if readiness_entity_id else None
        )
        return mop_stage_start_is_observed(
            active.operation,
            detailed_state.state if detailed_state else None,
            robot.adapter_capabilities.mop_start_states,
        )

    def _mark_mop_washing_started(
        self,
        robot: DiscoveredRobot,
        active: ActiveJob,
        now: datetime,
    ) -> bool:
        """Record Mop washing as command-start evidence, not room completion."""

        if active.phase == "mop_washing":
            return False
        readiness_entity_id = robot.adapter_capabilities.readiness_entity_id
        detailed_state = (
            self.hass.states.get(readiness_entity_id) if readiness_entity_id else None
        )
        active.phase = JobPhase.MOP_WASHING
        active.mop_washing_at = detailed_state.last_changed if detailed_state else now
        self._cancel_start_confirmation(robot.entity_id)
        _LOGGER.info(
            "Adaptive RoboVacs confirmed Mop start from dock washing: robot=%s room=%s",
            robot.entity_id,
            active.room_id,
        )
        return True

    async def async_set_global(self, key: str, value: Any) -> None:
        """Update a global control exposed by a native entity."""

        if key not in {
            "observe_only",
            "party_mode",
            "forecast_confidence",
            "unresolved_start",
            "unresolved_end",
            "adjacency_night_start",
            "adjacency_night_end",
        }:
            raise ValueError(f"Unknown global setting: {key}")
        if key in {"adjacency_night_start", "adjacency_night_end"}:
            if not isinstance(value, str) or not is_valid_daily_time(value):
                raise ValueError("Adjacency night bounds must be HH:MM times")
            if int(value[-2:]) % 15:
                raise ValueError("Adjacency night bounds must use 15-minute steps")
            other_key = (
                "adjacency_night_end"
                if key.endswith("start")
                else "adjacency_night_start"
            )
            if value == getattr(self.state.global_settings, other_key):
                raise ValueError("Adjacency night start and end must differ")
        if key in {"unresolved_start", "unresolved_end"}:
            global_start = (
                str(value)
                if key == "unresolved_start"
                else str(self.state.global_settings.unresolved_start)
            )
            global_end = (
                str(value)
                if key == "unresolved_end"
                else str(self.state.global_settings.unresolved_end)
            )
            resolve_daily_window(None, None, global_start, global_end)
        setattr(self.state.global_settings, key, value)
        await self._async_save()
        await self.async_evaluate(dry_run=True, reason=f"global:{key}")

    async def async_set_room_cleaning_period(self, area_id: str, option: str) -> None:
        """Apply one simple room schedule choice in a single durable update."""

        room = self.discovery.rooms.get(area_id)
        if room is None:
            raise ValueError(f"Unknown room area: {area_id}")
        settings = self._room_settings(room)
        updates = room_cleaning_period_update(option)
        configured_start = updates.get(
            "desired_window_start", settings.desired_window_start
        )
        configured_end = updates.get("desired_window_end", settings.desired_window_end)
        resolve_daily_window(
            configured_start if isinstance(configured_start, str) else None,
            configured_end if isinstance(configured_end, str) else None,
            self.state.global_settings.unresolved_start,
            self.state.global_settings.unresolved_end,
        )
        for update_key, update_value in updates.items():
            setattr(settings, update_key, update_value)
        await self._async_save()
        await self.async_evaluate(
            dry_run=True,
            reason=f"room:{area_id}:cleaning_period",
        )

    async def async_set_room_cleaning_profile(self, area_id: str, option: str) -> None:
        """Apply one room-wide profile choice in a single durable update."""

        room = self.discovery.rooms.get(area_id)
        if room is None:
            raise ValueError(f"Unknown room area: {area_id}")
        settings = self._room_settings(room)
        for update_key, update_value in room_cleaning_profile_update(option).items():
            setattr(settings, update_key, update_value)
        await self._async_save()
        self._sync_cleaning_program_issues()
        await self.async_evaluate(
            dry_run=True,
            reason=f"room:{area_id}:cleaning_profile",
        )

    async def async_set_room_setting(self, area_id: str, key: str, value: Any) -> None:
        """Update a discovered room's persistent scheduling setting."""

        if area_id not in self.discovery.rooms:
            raise ValueError(f"Unknown room area: {area_id}")
        if key not in {
            "enabled",
            "adjacency_mode",
            "cleaning_interval",
            "vacuum_interval",
            "expected_minutes",
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
        if key == "adjacency_mode":
            value = AdjacencyMode(value)
        key = {
            "vacuum_interval": "cleaning_interval",
            "pass_count": "vacuum_pass_count",
        }.get(key, key)
        if key in {"vacuum_pass_count", "mop_pass_count"} and value not in {None, 1, 2}:
            raise ValueError("Room pass count must be Robot default, 1, or 2")
        if key == "cleaning_program" and value not in {
            None,
            "vacuum_only",
            "mop_only",
            "vacuum_then_mop",
            "mop_then_vacuum",
        }:
            raise ValueError("Unknown room cleaning program")
        if key in {
            "fan_speed",
            "mode",
            "mop_mode",
            "mop_intensity",
            "cleaning_depth",
        } and not (value is None or isinstance(value, str)):
            raise ValueError(
                "Room cleaning profile options must be strings or Robot default"
            )
        if key in {"desired_window_start", "desired_window_end"}:
            configured_start = (
                value
                if key == "desired_window_start"
                else settings.desired_window_start
            )
            configured_end = (
                value if key == "desired_window_end" else settings.desired_window_end
            )
            resolve_daily_window(
                configured_start,
                configured_end,
                self.state.global_settings.unresolved_start,
                self.state.global_settings.unresolved_end,
            )
        setattr(settings, key, value)
        if key in ROOM_PROFILE_OVERRIDE_KEYS:
            settings.profile_custom = True
        await self._async_save()
        self._sync_cleaning_program_issues()
        await self.async_evaluate(
            dry_run=True,
            reason=f"room:{area_id}:{key}",
        )

    async def async_set_robot_setting(
        self, entity_id: str, key: str, value: Any
    ) -> None:
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
        robot = self.discovery.robots[entity_id]
        settings = self._robot_settings(robot)
        if key == "cleaning_program" and value not in {
            "vacuum_only",
            "mop_only",
            "vacuum_then_mop",
            "mop_then_vacuum",
        }:
            raise ValueError("Unknown robot cleaning program")
        if key in {
            "fan_speed",
            "mode",
            "mop_mode",
            "mop_intensity",
            "cleaning_depth",
        } and not (value is None or isinstance(value, str)):
            raise ValueError(
                "Robot cleaning profile options must be strings or Not configured"
            )
        if (
            robot.adapter_capabilities.native_mop_profile
            and key in {"mop_mode", "mop_intensity"}
            and not is_native_mop_profile_value(key, value)
        ):
            raise ValueError(
                "Native mop-only cleaning requires a concrete route and water intensity"
            )
        if key == "mopping_enabled":
            settings.cleaning_program = (
                CleaningProgram.VACUUM_THEN_MOP
                if value
                else CleaningProgram.VACUUM_ONLY
            )
        else:
            setattr(settings, key, value)
        if key == "cleaning_depth":
            settings.cleaning_depth_configured = True
        await self._async_save()
        self._sync_cleaning_program_issues()
        await self.async_evaluate(
            dry_run=True,
            reason=f"robot:{entity_id}:{key}",
        )

    async def _async_downgrade_q10_max_plus(
        self,
        robot: DiscoveredRobot,
        room: DiscoveredRoom,
        candidate: ScheduleCandidate | ActiveJob,
    ) -> None:
        """Persist the safe Max fallback after a failed Q10 Max+ custom clean.

        This intentionally changes only the setting that supplied the failed
        effective fan speed.  It never retries the physical start: a failed
        write is safe to record, while a failed or uncertain start remains
        governed by the normal global scheduler halt.
        """

        source = dict(candidate.profile_sources).get("fan_speed", "")
        settings = (
            self._room_settings(room)
            if source == "room"
            else self._robot_settings(robot)
        )
        if settings.fan_speed != "max_plus":
            return
        settings.fan_speed = "max"
        await self._async_save()
        self._sync_cleaning_program_issues()
        self._notify_listeners()
        _LOGGER.warning(
            "Adaptive RoboVacs changed a rejected Q10 Max+ profile to Max: "
            "robot=%s room=%s source=%s",
            robot.entity_id,
            room.name,
            source or "robot",
        )
