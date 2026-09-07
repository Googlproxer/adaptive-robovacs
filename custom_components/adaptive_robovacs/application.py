"""Application service for the Adaptive RoboVacs scheduler."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .application_actions import ApplicationActionsMixin
from .application_dispatch import ApplicationDispatchMixin
from .application_evaluation import ApplicationEvaluationMixin
from .application_events import ApplicationEventsMixin
from .application_faults import ApplicationFaultMixin
from .application_jobs import ApplicationJobsMixin
from .application_policy import ApplicationPolicyMixin
from .application_recovery import ApplicationRecoveryMixin
from .application_room_recovery import ApplicationRoomRecoveryMixin
from .application_settings import ApplicationSettingsMixin
from .application_water import ApplicationWaterMixin
from .command_queue import ApplicationCommandQueue
from .commands import (
    AcknowledgeRobotErrorCommand,
    AcknowledgeRoomRecoveryCommand,
    ActivateRetainedMapCommand,
    CaptureMapSnapshotCommand,
    ClearLegacyDeferralsCommand,
    CommandResult,
    EvaluateCommand,
    ExpireWaterConfirmationCommand,
    ListRetainedMapsCommand,
    ManualCleanRoomCommand,
    ObservedManualCleanCommand,
    RecheckAndResumeCommand,
    RecheckCleaningProgramCommand,
    RecheckNotificationTargetsCommand,
    RecheckRoomFaultCommand,
    RecheckTwoPassCompatibilityCommand,
    RecordManualCleanCommand,
    RefreshDiscoveryCommand,
    SaveFloorPlanCommand,
    SchedulerCommand,
    SchedulerCommandResult,
    SelectMapPreviewCommand,
    SetGlobalCommand,
    SetRobotSettingCommand,
    SetRoomAdjacencyCommand,
    SetRoomCleaningPeriodCommand,
    SetRoomCleaningProfileCommand,
    SetRoomSettingCommand,
    StateChangedCommand,
    StopAndReturnCommand,
    VerifyRetainedMapCommand,
    WaterConfirmationResponseCommand,
)
from .const import (
    DOMAIN,
    SIGNAL_DISCOVERY_UPDATED,
    STARTUP_STATE_SETTLE_DELAY,
)
from .discovery import (
    DiscoverySnapshot,
    async_discover,
)
from .dispatch import DispatchDependencies, DispatchPipeline
from .gateway import HomeAssistantVacuumGateway
from .lifecycle import SchedulerRuntime
from .map_recovery import MapRecoveryDependencies, MapRecoveryService
from .models import (
    EvaluationCause,
    EvaluationMode,
    effective_cleaning_program,
    expand_cleaning_program,
)
from .notifications import NotificationService
from .observations import HomeAssistantObserver
from .projections import (
    MapRecoveryProjectionSource,
    build_snapshot,
)
from .repair_service import RepairService
from .snapshots import IntegrationSnapshot
from .state import (
    SchedulerState,
    UnresolvedRobotReference,
    migrate_robot_identity,
)
from .storage import SchedulerStore

_LOGGER = logging.getLogger(__name__)
ROOM_DECISION_LIMIT = 100
DOCK_COMPLETION_DWELL = timedelta(minutes=5)

type EntityListener = Callable[[IntegrationSnapshot | Exception], None]


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


def _track_point(
    hass: HomeAssistant,
    action: Callable[[datetime], None],
    deadline: datetime,
) -> Callable[[], None]:
    """Register a point timer through one patchable application clock seam."""

    return async_track_point_in_utc_time(hass, action, deadline)


class SchedulerApplication(
    ApplicationSettingsMixin,
    ApplicationEventsMixin,
    ApplicationEvaluationMixin,
    ApplicationDispatchMixin,
    ApplicationActionsMixin,
    ApplicationPolicyMixin,
    ApplicationFaultMixin,
    ApplicationRecoveryMixin,
    ApplicationRoomRecoveryMixin,
    ApplicationJobsMixin,
    ApplicationWaterMixin,
):
    """Own scheduler state and orchestrate safe Home Assistant actions."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.storage = SchedulerStore(hass, entry.entry_id)
        self.repairs = RepairService(hass, entry.entry_id)
        self.state = SchedulerState.create(entry.data)
        self._storage_safe_mode = False
        self.discovery = DiscoverySnapshot.empty()
        self._lock = asyncio.Lock()
        self._listeners: set[EntityListener] = set()
        self._snapshot: IntegrationSnapshot | None = None
        self._discovery_signal_pending = False
        self._watch_entity_ids: set[str] = set()
        self._recovery_timers: dict[str, Callable[[], None]] = {}
        self._start_confirmation_timers: dict[str, Callable[[], None]] = {}
        self._ready_confirmation_timers: dict[str, Callable[[], None]] = {}
        self._ready_since: dict[str, datetime] = {}
        self._room_recovery_since: dict[str, datetime] = {}
        self._room_recovery_timers: dict[str, Callable[[], None]] = {}
        self._water_confirmation_timers: dict[str, Callable[[], None]] = {}
        self._startup_state_settle_until: datetime | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closing = False
        self._identity_migrated = False
        self.commands = ApplicationCommandQueue(
            hass,
            entry,
            self._async_execute_command,
        )
        self.gateway = HomeAssistantVacuumGateway(
            hass,
            lambda: not self._closing,
        )
        self.dispatch = DispatchPipeline(
            self.gateway,
            DispatchDependencies(
                room_for_id=lambda area_id: self.discovery.rooms.get(area_id),
                is_closing=lambda: self._closing,
                async_latch_fault=self._async_latch_dispatch_fault,
                async_handle_mop_preflight=(self._async_handle_mop_preflight_blocked),
                async_handle_mop_mode=self._async_handle_mop_mode_unconfirmed,
                async_downgrade_max_plus=self._async_downgrade_q10_max_plus,
                async_checkpoint=self._async_checkpoint_dispatch,
                async_abandon_checkpoint=self._async_abandon_dispatch_checkpoint,
                async_accept=self._async_accept_dispatch,
            ),
        )
        self.observer = HomeAssistantObserver(hass)
        self.notifications = NotificationService(hass)
        self.map_recovery = MapRecoveryService(
            hass,
            entry.entry_id,
            MapRecoveryDependencies(
                robot_for_entity_id=lambda entity_id: self.discovery.robots.get(
                    entity_id
                ),
                robot_for_registry_id=self.robot_for_registry_id,
                hold_for_registry_id=lambda registry_id: self.state.robot_holds.get(
                    registry_id
                ),
                active_job_for_registry_id=lambda registry_id: (
                    self.state.active_jobs.get(registry_id)
                ),
                dispatch_block_reason=self._map_recovery_dispatch_block_reason,
                async_set_hold=self._async_set_map_recovery_hold,
                async_refresh_discovery=self._async_refresh_for_map_recovery,
                publish_snapshot=self._notify_listeners,
            ),
        )
        self.lifecycle = SchedulerRuntime(
            hass,
            interval_handler=self._async_interval,
            call_service_handler=self._on_call_service,
            state_changed_handler=self._on_state_changed,
            device_registry_handler=self._on_device_registry_updated,
            notification_action_handler=self._on_mobile_notification_action,
            notification_cleared_handler=self._on_mobile_notification_cleared,
            home_assistant_started_handler=self._on_home_assistant_started,
            submit=self.async_execute,
            create_task=self._async_create_task,
        )

    async def async_initialize(self) -> None:
        """Restore state, discover the house, and begin passive observation."""

        self._startup_state_settle_until = _now() + STARTUP_STATE_SETTLE_DELAY
        loaded = await self.storage.async_load(self.entry.data)
        self.state = loaded.state
        migrated = loaded.migrated
        if loaded.safe_mode:
            # Do not overwrite a Store written by a newer version or a malformed
            # payload.  A fresh observe-only view keeps the robot authoritative.
            self._storage_safe_mode = True
            _LOGGER.error(
                "Adaptive RoboVacs could not safely load persisted scheduler state; "
                "dispatch is disabled until the Store is repaired: %s",
                loaded.error,
            )
            self.repairs.set_storage_unsafe(True, str(loaded.error))
        else:
            self.repairs.set_storage_unsafe(False)
        await self.async_refresh_discovery()
        await self.map_recovery.async_initialize()
        baseline_initialized = False
        if self.state.first_scheduler_online_at is None:
            self.state.first_scheduler_online_at = _now()
            baseline_initialized = True
        if migrated or self._identity_migrated or baseline_initialized:
            await self._async_save()
        if self.state.robot_faults or self.state.room_faults:
            self._sync_dispatch_fault_issues()
        await self._async_recover_active_jobs()
        self._sync_room_recovery_issues()
        await self._async_restore_water_confirmations()
        await self.async_execute(
            EvaluateCommand(
                mode=EvaluationMode.PREVIEW,
                cause=EvaluationCause.STARTUP,
            )
        )
        await self.commands.async_start()
        await self.lifecycle.async_start(self._startup_state_settle_until)

    async def async_shutdown(self) -> None:
        """Stop callbacks, drain coordinator work, and persist once."""

        self._closing = True
        await self.lifecycle.async_stop()
        await self.commands.async_close()
        await self.map_recovery.async_shutdown()
        while self._recovery_timers:
            self._recovery_timers.popitem()[1]()
        while self._start_confirmation_timers:
            self._start_confirmation_timers.popitem()[1]()
        while self._ready_confirmation_timers:
            self._ready_confirmation_timers.popitem()[1]()
        while self._room_recovery_timers:
            self._room_recovery_timers.popitem()[1]()
        while self._water_confirmation_timers:
            self._water_confirmation_timers.popitem()[1]()
        current = asyncio.current_task()
        tasks = [
            task for task in self._tasks if task is not current and not task.done()
        ]
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
        self.commands.begin_shutdown()

    def cancel_shutdown(self) -> None:
        """Resume normal work when platform unload is rejected."""

        self._closing = False
        self.commands.cancel_shutdown()

    def _shutdown_started(self) -> bool:
        """Read the shutdown latch again after an await boundary."""

        return self._closing

    async def async_execute(self, command: SchedulerCommand) -> SchedulerCommandResult:
        """Execute one typed command through the per-entry FIFO worker."""

        return await self.commands.async_execute(command)

    async def _async_execute_command(
        self, command: SchedulerCommand
    ) -> SchedulerCommandResult:
        """Dispatch a command after the queue has serialized it."""

        match command:
            case EvaluateCommand():
                return CommandResult.from_mapping(
                    await self.async_evaluate(
                        dry_run=command.dry_run,
                        reason=command.reason,
                    )
                )
            case StateChangedCommand(
                entity_id=entity_id,
                old_state=old_state,
                new_state=new_state,
            ):
                if old_state != new_state:
                    for robot in self.discovery.robots.values():
                        capabilities = robot.adapter_capabilities
                        if entity_id in {
                            robot.entity_id,
                            capabilities.readiness_entity_id,
                            capabilities.completion_status_entity_id,
                            *capabilities.error_entity_ids,
                        }:
                            self._reset_room_recovery_dock(robot.registry_id)
                if entity_id in self.discovery.robots:
                    self.map_recovery.handle_state_transition(
                        entity_id,
                        old_state,
                        new_state,
                    )
                return CommandResult.from_mapping(
                    await self.async_evaluate(
                        dry_run=False,
                        reason=f"state:{entity_id}",
                    )
                )
            case RefreshDiscoveryCommand():
                await self._async_refresh_discovery_after_device_label_change()
            case SetGlobalCommand(key=key, value=value):
                await self.async_set_global(key, value)
            case SetRoomSettingCommand(area_id=area_id, key=key, value=value):
                await self.async_set_room_setting(area_id, key, value)
            case SetRobotSettingCommand(
                robot_entity_id=entity_id, key=key, value=value
            ):
                await self.async_set_robot_setting(entity_id, key, value)
            case SetRoomCleaningPeriodCommand(area_id=area_id, option=option):
                await self.async_set_room_cleaning_period(area_id, option)
            case SetRoomCleaningProfileCommand(area_id=area_id, option=option):
                await self.async_set_room_cleaning_profile(area_id, option)
            case ManualCleanRoomCommand(
                area_id=area_id,
                mode=mode,
                context_id=context_id,
                user_id=user_id,
            ):
                return CommandResult.from_mapping(
                    await self.async_manual_clean_room(
                        area_id,
                        mode,
                        context_id=context_id,
                        user_id=user_id,
                    )
                )
            case RecordManualCleanCommand(
                robot_entity_id=entity_id,
                area_ids=area_ids,
                operations=operations,
            ):
                return CommandResult.from_mapping(
                    await self.async_record_manual_clean(
                        entity_id,
                        list(area_ids),
                        list(operations),
                    )
                )
            case ObservedManualCleanCommand(request=request, context_id=context_id):
                await self._async_record_observed_manual_clean(request, context_id)
            case WaterConfirmationResponseCommand(
                action=action,
                request_id=request_id,
                tag=tag,
                dismissed=dismissed,
            ):
                await self._async_handle_water_confirmation(
                    action=action,
                    request_id=request_id,
                    tag=tag,
                    dismissed=dismissed,
                )
            case ExpireWaterConfirmationCommand(request_id=request_id):
                await self._async_expire_water_confirmation(request_id)
            case StopAndReturnCommand(robot_entity_id=entity_id, context=context):
                return CommandResult.from_mapping(
                    await self.async_stop_and_return_to_dock(
                        entity_id,
                        context=context,
                    )
                )
            case RecheckAndResumeCommand(robot_registry_id=registry_id):
                halt_result = await self.async_recheck_and_resume(registry_id)
                return CommandResult.from_mapping(
                    {
                        "cleared": halt_result.cleared,
                        "reason": halt_result.reason,
                        "robot_state": halt_result.robot_state,
                    }
                )
            case RecheckRoomFaultCommand(area_id=area_id):
                return CommandResult.from_mapping(
                    {"cleared": await self.async_recheck_room_fault(area_id)}
                )
            case AcknowledgeRoomRecoveryCommand(
                area_id=area_id, recovery_id=recovery_id
            ):
                return CommandResult.from_mapping(
                    await self.async_acknowledge_room_recovery(area_id, recovery_id)
                )
            case AcknowledgeRobotErrorCommand(
                robot_registry_id=registry_id, held_at=held_at
            ):
                return CommandResult.from_mapping(
                    await self.async_acknowledge_robot_error(registry_id, held_at)
                )
            case RecheckTwoPassCompatibilityCommand(area_id=area_id):
                return CommandResult.from_mapping(
                    {"cleared": await self.async_recheck_room_compatibility(area_id)}
                )
            case RecheckCleaningProgramCommand(area_id=area_id):
                return CommandResult.from_mapping(
                    {
                        "cleared": (
                            await self.async_recheck_cleaning_program_compatibility(
                                area_id
                            )
                        )
                    }
                )
            case RecheckNotificationTargetsCommand():
                available = self.has_notification_targets()
                if available:
                    self.repairs.set_notification_delivery_issue(False)
                return CommandResult.from_mapping({"cleared": available})
            case ClearLegacyDeferralsCommand(area_ids=area_ids):
                return CommandResult.from_mapping(
                    await self.async_clear_legacy_deferrals(list(area_ids))
                )
            case SetRoomAdjacencyCommand(
                area_id=area_id, neighbor_area_ids=neighbor_area_ids
            ):
                return CommandResult.from_mapping(
                    await self.async_set_room_adjacency(
                        area_id,
                        list(neighbor_area_ids),
                    )
                )
            case SaveFloorPlanCommand(request=request):
                return CommandResult.from_mapping(
                    await self.async_save_floor_plan(request)
                )
            case ListRetainedMapsCommand(robot_entity_id=entity_id):
                map_list = await self.map_recovery.async_list_maps(entity_id)
                return CommandResult.from_mapping(map_list.as_response())
            case CaptureMapSnapshotCommand(robot_entity_id=entity_id, trigger=trigger):
                capture = await self.map_recovery.async_capture(
                    entity_id,
                    trigger=trigger,
                )
                self._notify_listeners()
                return CommandResult.from_mapping(capture.as_response())
            case ActivateRetainedMapCommand(
                robot_entity_id=entity_id,
                map_id=map_id,
                confirm=confirm,
            ):
                activation = await self.map_recovery.async_activate(
                    entity_id,
                    map_id,
                    confirm=confirm,
                )
                self._notify_listeners()
                return CommandResult.from_mapping(activation.as_response())
            case VerifyRetainedMapCommand(robot_entity_id=entity_id, confirm=confirm):
                verification = await self.map_recovery.async_verify(
                    entity_id,
                    confirm=confirm,
                )
                preview = await self.async_evaluate(
                    dry_run=True,
                    reason="map-selection-confirmed",
                )
                return CommandResult.from_mapping(verification.as_response(preview))
            case SelectMapPreviewCommand(robot_entity_id=entity_id, option=option):
                self.map_recovery.select_preview_option(entity_id, option)
                self._notify_listeners()
        return None

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

    def current_snapshot(self) -> IntegrationSnapshot:
        """Return the last settled immutable application snapshot."""

        if self._snapshot is None:
            self._snapshot = build_snapshot(self)
        return self._snapshot

    @property
    def map_recovery_projection(self) -> MapRecoveryProjectionSource:
        """Expose the map archive's read-only snapshot port."""

        # The concrete service intentionally also owns command methods. Keep
        # that mutable surface out of the presentation protocol.
        return cast(MapRecoveryProjectionSource, self.map_recovery)

    @callback
    def _notify_listeners(self) -> None:
        """Build once, then publish one transactionally settled snapshot."""

        try:
            snapshot = build_snapshot(self)
        except Exception as err:
            _LOGGER.exception("Adaptive RoboVacs could not build its snapshot")
            for listener in tuple(self._listeners):
                listener(err)
            return
        self._snapshot = snapshot
        for listener in tuple(self._listeners):
            listener(snapshot)
        if self._discovery_signal_pending:
            # Dynamic entity factories read coordinator.data. Application
            # listeners therefore must publish the new snapshot before the
            # dispatcher signal allows those factories to run.
            self._discovery_signal_pending = False
            async_dispatcher_send(
                self.hass,
                SIGNAL_DISCOVERY_UPDATED,
                self.entry.entry_id,
            )

    async def _async_save(self) -> None:
        if self._storage_safe_mode:
            return
        await self.storage.async_save(self.state)

    async def async_refresh_discovery(self, *, notify: bool = True) -> None:
        """Refresh registry state and reset only changed room occupancy models.

        Evaluation publishes its final projection after recalculating room and
        robot state, so it suppresses this intermediate notification. Direct
        refresh callers retain the existing immediate publication behaviour.
        """

        prior_discovery = self.discovery
        self.discovery = await async_discover(self.hass)
        self._identity_migrated = (
            self._migrate_runtime_robot_identity(prior_discovery)
            or self._identity_migrated
        )
        self._identity_migrated = (
            self._reconcile_unresolved_robot_references() or self._identity_migrated
        )

        for room in self.discovery.rooms.values():
            detail = self._room_data(room.area_id)
            fingerprint = ",".join(
                (*room.radar_entity_ids, "|", *room.fallback_entity_ids)
            )
            if detail.source_fingerprint not in {None, fingerprint}:
                detail.occupancy_samples.clear()
                detail.unoccupied_since = None
                detail.occupancy = "unresolved"
                detail.occupancy_source = "sources_changed"
            detail.source_fingerprint = fingerprint
            self._room_settings(room)

        for robot in self.discovery.robots.values():
            self._robot_settings(robot)
            self.state.active_jobs.setdefault(robot.registry_id, None)

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

        for area_id in tuple(self.state.water_notification_episodes):
            episode_room = self.discovery.rooms.get(area_id)
            room_settings = self._room_settings(episode_room) if episode_room else None
            has_mop_program = False
            water_ready = False
            if episode_room and room_settings and room_settings.enabled:
                for robot in self.discovery.robots.values():
                    if robot.floor_id != episode_room.floor_id:
                        continue
                    program = effective_cleaning_program(
                        room_settings.cleaning_program,
                        self._robot_settings(robot).cleaning_program,
                    )
                    has_mop_program = (
                        has_mop_program
                        or "mop" in expand_cleaning_program(program or "")
                    )
                    water_ready = water_ready or bool(
                        robot.adapter_capabilities.water_readiness.ready
                    )
            if not has_mop_program or water_ready:
                self.state.water_notification_episodes.pop(area_id, None)

        if prior_discovery != self.discovery:
            # Evaluation deliberately suppresses its intermediate snapshot.
            # Retain the signal until that transaction publishes its final
            # snapshot so dynamic entity factories never observe stale data.
            self._discovery_signal_pending = True
        self._sync_two_pass_issues()
        self._sync_cleaning_program_issues()
        if notify:
            self._notify_listeners()

    def _migrate_runtime_robot_identity(
        self, prior_discovery: DiscoverySnapshot
    ) -> bool:
        """Bind stable robot keys to current entity IDs without losing aliases."""

        return migrate_robot_identity(
            self.state,
            {
                robot.registry_id: robot.entity_id
                for robot in self.discovery.robots.values()
            },
            {
                robot.registry_id: robot.entity_id
                for robot in prior_discovery.robots.values()
            },
        )

    def _reconcile_unresolved_robot_references(self) -> bool:
        """Resolve exact aliases and quarantine ambiguous legacy robot keys."""

        current = {
            robot.registry_id: robot.entity_id
            for robot in self.discovery.robots.values()
        }
        changed = False
        for legacy_key, reference in tuple(
            self.state.unresolved_robot_references.items()
        ):
            matches = {
                registry_id
                for registry_id, entity_id in current.items()
                if legacy_key
                in {
                    registry_id,
                    entity_id,
                    self.state.robot_entity_aliases.get(registry_id),
                }
            }
            if len(matches) != 1:
                continue
            registry_id = matches.pop()
            if reference.settings is not None:
                self.state.robot_settings.setdefault(
                    registry_id,
                    reference.settings,
                )
            # Discovery seeds known robots with a ``None`` checkpoint. A
            # resolved legacy checkpoint must replace that empty slot or
            # restart recovery would silently discard in-flight work.
            if (
                reference.active_job is not None
                and self.state.active_jobs.get(registry_id) is None
            ):
                self.state.active_jobs[registry_id] = reference.active_job
            if reference.hold is not None:
                self.state.robot_holds.setdefault(registry_id, reference.hold)
            if reference.cooldown is not None:
                self.state.robot_cooldowns.setdefault(
                    registry_id,
                    reference.cooldown,
                )
            for area_id in reference.occurrence_room_ids:
                occurrence = self.state.occurrences.get(area_id)
                if occurrence is not None:
                    occurrence.robot_registry_id = registry_id
                    occurrence.robot_entity_id = current[registry_id]
            self.state.audit.manual_events = [
                replace(record, robot_registry_id=registry_id)
                if record.robot_registry_id == legacy_key
                else record
                for record in self.state.audit.manual_events
            ]
            self.state.audit.recovery_events = [
                replace(record, robot_registry_id=registry_id)
                if record.robot_registry_id == legacy_key
                else record
                for record in self.state.audit.recovery_events
            ]
            self.state.robot_entity_aliases.setdefault(registry_id, legacy_key)
            del self.state.unresolved_robot_references[legacy_key]
            changed = True

        known_registry_ids = set(current) | set(self.state.robot_entity_aliases)
        audit_registry_ids = {
            record.robot_registry_id
            for record in self.state.audit.manual_events
            if record.robot_registry_id
        } | {
            record.robot_registry_id
            for record in self.state.audit.recovery_events
            if record.robot_registry_id
        }
        legacy_keys = (
            set(self.state.robot_settings)
            | set(self.state.active_jobs)
            | set(self.state.robot_holds)
            | set(self.state.robot_cooldowns)
            | audit_registry_ids
            | {
                occurrence.robot_registry_id
                for occurrence in self.state.occurrences.values()
            }
        ) - known_registry_ids
        now = _now()
        for legacy_key in sorted(legacy_keys):
            occurrence_room_ids = tuple(
                sorted(
                    area_id
                    for area_id, occurrence in self.state.occurrences.items()
                    if occurrence.robot_registry_id == legacy_key
                    or occurrence.robot_entity_id == legacy_key
                )
            )
            owned_record_present = any(
                legacy_key in section
                for section in (
                    self.state.robot_settings,
                    self.state.active_jobs,
                    self.state.robot_holds,
                    self.state.robot_cooldowns,
                )
            )
            settings = self.state.robot_settings.pop(legacy_key, None)
            active_job = self.state.active_jobs.pop(legacy_key, None)
            hold = self.state.robot_holds.pop(legacy_key, None)
            cooldown = self.state.robot_cooldowns.pop(legacy_key, None)
            existing = self.state.unresolved_robot_references.get(legacy_key)
            if existing is None:
                self.state.unresolved_robot_references[legacy_key] = (
                    UnresolvedRobotReference(
                        legacy_key=legacy_key,
                        reason="robot_registry_identity_unresolved",
                        first_seen_at=now,
                        settings=settings,
                        active_job=active_job,
                        hold=hold,
                        cooldown=cooldown,
                        occurrence_room_ids=occurrence_room_ids,
                    )
                )
                changed = True
                continue

            # Audit and occurrence records keep the legacy key visible on every
            # discovery pass. Preserve the first quarantine checkpoint and
            # merge any newly found records instead of replacing it with Nones.
            if existing.settings is None and settings is not None:
                existing.settings = settings
            if existing.active_job is None and active_job is not None:
                existing.active_job = active_job
            if existing.hold is None and hold is not None:
                existing.hold = hold
            if existing.cooldown is None and cooldown is not None:
                existing.cooldown = cooldown
            merged_room_ids = tuple(
                sorted(set(existing.occurrence_room_ids) | set(occurrence_room_ids))
            )
            if merged_room_ids != existing.occurrence_room_ids:
                existing.occurrence_room_ids = merged_room_ids
                changed = True
            changed = owned_record_present or changed

        unresolved = tuple(sorted(self.state.unresolved_robot_references))
        self.repairs.sync_unresolved_robot_references(unresolved)
        return changed
