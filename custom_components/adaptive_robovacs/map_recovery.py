"""Map-recovery application service.

The service coordinates typed archive storage, the Roborock transport bridge,
and scheduler-owned holds through explicit callbacks. It deliberately keeps no
coordinator or application back-reference.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .adapters.roborock import RoborockMappingError, resolve_roborock_area_mapping
from .const import MAP_RECOVERY_RETENTION, SIGNAL_DISCOVERY_UPDATED
from .discovery import DiscoveredRobot
from .map_recovery_models import (
    ArchivedMap,
    ArchivedRoomSummary,
    AvailableMapSummary,
    CaptureSetSummary,
    MapActivationResult,
    MapCaptureResult,
    MapCaptureSet,
    MapListResult,
    MapRecoveryArchive,
    MapRecoveryError,
    MapRecoverySummary,
    MapRecoveryUnavailable,
    MapVerificationResult,
    RecoveryCapability,
    RecoveryCapabilityState,
    RobotMapArchive,
)
from .map_recovery_roborock import Q10MapTransport, Q10RuntimeResolver
from .map_recovery_store import MapRecoveryStore
from .q10_map_frame import Q10MapFrameError, render_q10_map_preview
from .state import ActiveJob, RobotHold

_LOGGER = logging.getLogger(__name__)
_MAX_MAP_SLOTS = 8
_SETTLE_DELAY = timedelta(seconds=60)

type RobotLookup = Callable[[str], DiscoveredRobot | None]
type HoldLookup = Callable[[str], RobotHold | None]
type ActiveJobLookup = Callable[[str], ActiveJob | None]
type SetHold = Callable[[str, RobotHold | None], Awaitable[None]]
type RefreshDiscovery = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class MapRecoveryDependencies:
    """Narrow application capabilities required by map recovery."""

    robot_for_entity_id: RobotLookup
    robot_for_registry_id: RobotLookup
    hold_for_registry_id: HoldLookup
    active_job_for_registry_id: ActiveJobLookup
    dispatch_block_reason: Callable[[], str | None]
    async_set_hold: SetHold
    async_refresh_discovery: RefreshDiscovery
    publish_snapshot: Callable[[], None]


class MapRecoveryService:
    """Coordinate safe, checkpointed map capture and selection actions."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        dependencies: MapRecoveryDependencies,
        *,
        store: MapRecoveryStore | None = None,
        resolver: Q10RuntimeResolver | None = None,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._dependencies = dependencies
        self._store = store or MapRecoveryStore(hass, entry_id)
        self._archive: MapRecoveryArchive | None = None
        self._resolver = resolver or Q10RuntimeResolver(hass)
        self._lock_by_robot: dict[str, asyncio.Lock] = {}
        self._settle_tasks: dict[str, asyncio.Task[None]] = {}
        self._seen_cleaning: set[str] = set()
        self._preview_selection: dict[str, tuple[str, str]] = {}
        self._storage_error: str | None = None

    async def async_initialize(self) -> None:
        """Load and validate the independent map archive."""

        loaded = await self._store.async_load()
        self._archive = loaded.archive
        self._storage_error = loaded.error
        if loaded.error:
            _LOGGER.error(
                "Adaptive RoboVacs map capture storage is unavailable; "
                "the original Store will not be overwritten"
            )

    async def async_shutdown(self) -> None:
        """Cancel pending passive captures without abandoning active calls."""

        tasks = tuple(self._settle_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._settle_tasks.clear()

    def _require_archive(self) -> MapRecoveryArchive:
        if self._archive is None:
            raise MapRecoveryUnavailable("map capture storage is not initialized")
        if self._storage_error:
            raise MapRecoveryUnavailable(self._storage_error)
        return self._archive

    def _robot_archive(self, registry_id: str) -> RobotMapArchive:
        archive = self._require_archive()
        return archive.robots.setdefault(registry_id, RobotMapArchive())

    async def _async_save(self) -> None:
        if self._storage_error:
            raise MapRecoveryUnavailable(self._storage_error)
        await self._store.async_save(self._require_archive())

    def _robot(self, entity_id: str) -> DiscoveredRobot:
        if self._storage_error:
            raise MapRecoveryUnavailable(self._storage_error)
        robot = self._dependencies.robot_for_entity_id(entity_id)
        if robot is None:
            raise MapRecoveryError("robot is not discovered by this config entry")
        return robot

    def _current_robot(self, registry_id: str) -> DiscoveredRobot:
        robot = self._dependencies.robot_for_registry_id(registry_id)
        if robot is None:
            raise MapRecoveryError("robot is not discovered by this config entry")
        return robot

    def _lock(self, registry_id: str) -> asyncio.Lock:
        return self._lock_by_robot.setdefault(registry_id, asyncio.Lock())

    def _is_held(self, registry_id: str) -> bool:
        hold = self._dependencies.hold_for_registry_id(registry_id)
        return bool(hold and hold.reason == "map_recovery_pending")

    def _terminal(self, robot: DiscoveredRobot) -> bool:
        state = self._hass.states.get(robot.entity_id)
        return bool(state and state.state in {"docked", "idle"})

    def _publish(self) -> None:
        self._dependencies.publish_snapshot()
        async_dispatcher_send(
            self._hass,
            SIGNAL_DISCOVERY_UPDATED,
            self._entry_id,
        )

    def capability(self, entity_id: str) -> RecoveryCapability:
        """Return a safe capability result for one current entity ID."""

        if self._storage_error:
            return RecoveryCapability(
                RecoveryCapabilityState.UNAVAILABLE,
                self._storage_error,
            )
        robot = self._dependencies.robot_for_entity_id(entity_id)
        if robot is None:
            return RecoveryCapability(
                RecoveryCapabilityState.UNAVAILABLE,
                "robot is not discovered",
            )
        try:
            self._resolver.async_resolve(robot)
        except MapRecoveryUnavailable as err:
            return RecoveryCapability(
                RecoveryCapabilityState.UNAVAILABLE,
                str(err),
            )
        return RecoveryCapability(RecoveryCapabilityState.READY)

    def summary(self, entity_id: str) -> MapRecoverySummary:
        """Build immutable presentation data from the typed map archive."""

        robot = self._dependencies.robot_for_entity_id(entity_id)
        if robot is None:
            return MapRecoverySummary(
                state="unavailable",
                reason="robot is not discovered",
                retention=MAP_RECOVERY_RETENTION,
                capture_count=0,
                last_capture=None,
                last_error=None,
                map_selection_pending=False,
                capture_sets=(),
                available_maps=(),
            )
        held = self._is_held(robot.registry_id)
        capability = self.capability(entity_id)
        if self._storage_error or self._archive is None:
            return MapRecoverySummary(
                state="unavailable",
                reason=self._storage_error or "map capture storage is not initialized",
                retention=MAP_RECOVERY_RETENTION,
                capture_count=0,
                last_capture=None,
                last_error=None,
                map_selection_pending=held,
                capture_sets=(),
                available_maps=(),
            )
        archive = self._robot_archive(robot.registry_id)
        captures = archive.capture_sets
        latest = captures[-1] if captures else None
        return MapRecoverySummary(
            state="map selection pending" if held else capability.state.value,
            reason=capability.reason,
            retention=MAP_RECOVERY_RETENTION,
            capture_count=len(captures),
            last_capture=captures[-1].captured_at if captures else None,
            last_error=archive.last_error,
            map_selection_pending=held,
            capture_sets=tuple(
                CaptureSetSummary(
                    snapshot_id=capture.snapshot_id,
                    captured_at=capture.captured_at,
                    trigger=capture.trigger,
                    map_count=len(capture.maps),
                )
                for capture in captures[-MAP_RECOVERY_RETENTION:]
            ),
            available_maps=tuple(
                AvailableMapSummary(item.map_id, item.name, item.robot_timestamp)
                for item in (latest.maps if latest else ())
            ),
        )

    async def async_list_maps(self, entity_id: str) -> MapListResult:
        """List live retained maps without mutating the robot."""

        robot = self._robot(entity_id)
        async with self._lock(robot.registry_id):
            try:
                maps = await self._resolver.async_resolve(robot).async_list_maps()
            except MapRecoveryError:
                raise
            except Exception as err:
                _LOGGER.debug(
                    "Map list failed for registered robot %s",
                    robot.registry_id,
                    exc_info=True,
                )
                raise MapRecoveryError(
                    "Could not retrieve the robot's retained maps"
                ) from err
        return MapListResult(self.summary(entity_id), tuple(maps))

    async def _async_capture(
        self,
        robot: DiscoveredRobot,
        trigger: str,
        *,
        force: bool,
    ) -> MapCaptureResult:
        if not self._terminal(robot):
            raise MapRecoveryError("robot must be docked or idle to capture maps")
        bridge: Q10MapTransport = self._resolver.async_resolve(robot)
        maps = await bridge.async_list_maps()
        if not maps or len(maps) > _MAX_MAP_SLOTS:
            raise MapRecoveryError("robot did not report a safe retained-map list")
        captured: list[ArchivedMap] = []
        for slot in maps:
            frame = await bridge.async_get_map(slot.map_id)
            captured.append(
                ArchivedMap(
                    map_id=slot.map_id,
                    name=slot.name,
                    robot_timestamp=slot.timestamp,
                    packet_sha256=frame.sha256,
                    packet=frame.packet,
                    preview_png=render_q10_map_preview(frame),
                    width=frame.width,
                    height=frame.height,
                    rooms=tuple(
                        ArchivedRoomSummary(
                            room_id=room.room_id,
                            name=room.name,
                            order_hint=room.order_hint,
                            pixel_count=room.pixel_count,
                        )
                        for room in frame.rooms
                    ),
                )
            )
        combined = hashlib.sha256(
            "".join(item.packet_sha256 for item in captured).encode()
        ).hexdigest()
        archive = self._robot_archive(robot.registry_id)
        previous = archive.capture_sets
        if not force and previous and previous[-1].combined_sha256 == combined:
            archive.last_error = None
            await self._async_save()
            return MapCaptureResult(
                snapshot_id=previous[-1].snapshot_id,
                deduplicated=True,
                map_count=len(captured),
            )
        capture = MapCaptureSet(
            snapshot_id=str(uuid4()),
            captured_at=datetime.now(UTC),
            trigger=trigger,
            combined_sha256=combined,
            maps=tuple(captured),
        )
        archive.capture_sets = [*previous, capture][-MAP_RECOVERY_RETENTION:]
        archive.last_error = None
        await self._async_save()
        return MapCaptureResult(
            snapshot_id=capture.snapshot_id,
            deduplicated=False,
            map_count=len(captured),
            digest=combined,
        )

    async def async_capture(
        self,
        entity_id: str,
        *,
        trigger: str = "manual",
    ) -> MapCaptureResult:
        """Capture every retained map into the independent typed archive."""

        robot = self._robot(entity_id)
        async with self._lock(robot.registry_id):
            try:
                result = await self._async_capture(
                    robot,
                    trigger,
                    force=trigger == "manual",
                )
            except (MapRecoveryError, Q10MapFrameError) as err:
                if not self._storage_error:
                    archive = self._robot_archive(robot.registry_id)
                    archive.last_error = str(err)
                    await self._async_save()
                raise MapRecoveryError(str(err)) from err
        self._publish()
        return result

    async def async_activate(
        self,
        entity_id: str,
        map_id: str,
        *,
        confirm: bool,
    ) -> MapActivationResult:
        """Checkpoint a hold before asking the existing runtime to select a map."""

        if not confirm:
            raise MapRecoveryError("activation requires confirm: true")
        if (reason := self._dependencies.dispatch_block_reason()) is not None:
            raise MapRecoveryError(reason)
        robot = self._robot(entity_id)
        registry_id = robot.registry_id
        if self._dependencies.active_job_for_registry_id(registry_id):
            raise MapRecoveryError("robot has an active scheduler job")
        if self._is_held(registry_id):
            raise MapRecoveryError("map selection confirmation is already pending")
        if not self._terminal(robot):
            raise MapRecoveryError("robot must be docked or idle to activate a map")
        lock = self._lock(registry_id)
        if lock.locked():
            raise MapRecoveryError("another map operation is already running")
        async with lock:
            bridge = self._resolver.async_resolve(robot)
            retained = await bridge.async_list_maps()
            requested_map_id = str(map_id)
            if requested_map_id not in {item.map_id for item in retained}:
                raise MapRecoveryError(
                    "requested map is no longer retained by the robot"
                )
            now = datetime.now(UTC)
            await self._dependencies.async_set_hold(
                registry_id,
                RobotHold(
                    reason="map_recovery_pending",
                    phase="manual_verification",
                    requested_map_id=requested_map_id,
                    held_at=now,
                    last_observed_at=now,
                ),
            )
            try:
                before = await self._async_capture(
                    robot,
                    "pre_activation",
                    force=True,
                )
            except Exception:
                await self._dependencies.async_set_hold(registry_id, None)
                raise
            await bridge.async_apply_map(requested_map_id)
            confirmed = False
            try:
                refreshed = await bridge.async_list_maps()
                if requested_map_id in {item.map_id for item in refreshed}:
                    frame = await bridge.async_get_map(requested_map_id)
                    confirmed = frame.map_id == requested_map_id
            except MapRecoveryError:
                pass
        self._publish()
        return MapActivationResult(
            pre_activation_snapshot_id=before.snapshot_id,
            requested_map_id=requested_map_id,
            confirmed=confirmed,
        )

    async def async_verify(
        self,
        entity_id: str,
        *,
        confirm: bool,
    ) -> MapVerificationResult:
        """Verify selected map and mappings before releasing the scheduler hold."""

        if not confirm:
            raise MapRecoveryError("verification requires confirm: true")
        robot = self._robot(entity_id)
        registry_id = robot.registry_id
        if not self._is_held(registry_id):
            raise MapRecoveryError("no map selection confirmation is pending")
        if not self._terminal(robot) or self._dependencies.active_job_for_registry_id(
            registry_id
        ):
            raise MapRecoveryError("robot must be docked or idle with no active job")
        lock = self._lock(registry_id)
        if lock.locked():
            raise MapRecoveryError("another map operation is already running")
        async with lock:
            await self._dependencies.async_refresh_discovery()
            robot = self._current_robot(registry_id)
            hold = self._dependencies.hold_for_registry_id(registry_id)
            requested_map_id = hold.requested_map_id if hold else None
            if not requested_map_id:
                raise MapRecoveryError("the pending map selection has no selected map")
            bridge = self._resolver.async_resolve(robot)
            retained = await bridge.async_list_maps()
            if requested_map_id not in {item.map_id for item in retained}:
                raise MapRecoveryError(
                    "the selected map is no longer retained by the robot"
                )
            frame = await bridge.async_get_map(requested_map_id)
            if frame.map_id != requested_map_id:
                raise MapRecoveryError(
                    "the selected retained map could not be verified"
                )
            self._preflight_room_mapping(robot)
            await self._dependencies.async_set_hold(registry_id, None)
        self._publish()
        return MapVerificationResult()

    def _preflight_room_mapping(self, robot: DiscoveredRobot) -> None:
        if not robot.supports_area_clean:
            raise MapRecoveryError("Home Assistant area mapping is unavailable")
        entry = er.async_get(self._hass).async_get(robot.entity_id)
        options = getattr(entry, "options", {}) if entry else {}
        vacuum_options = (
            options.get("vacuum", {}) if isinstance(options, Mapping) else {}
        )
        mapping = (
            vacuum_options.get("area_mapping")
            if isinstance(vacuum_options, Mapping)
            else None
        )
        if not isinstance(mapping, Mapping) or not mapping:
            raise MapRecoveryError("Home Assistant area mapping is unavailable")
        try:
            for area_id in mapping:
                resolve_roborock_area_mapping(vacuum_options, (str(area_id),))
        except RoborockMappingError as err:
            raise MapRecoveryError(
                "Home Assistant room mapping needs to be refreshed"
            ) from err

    @callback
    def handle_state_transition(
        self,
        entity_id: str,
        old_state: str | None,
        new_state: str | None,
    ) -> None:
        """Schedule a post-clean capture from observed state transitions."""

        del old_state
        robot = self._dependencies.robot_for_entity_id(entity_id)
        if robot is None:
            return
        registry_id = robot.registry_id
        if new_state in {"cleaning", "returning", "unavailable", "unknown"}:
            if new_state in {"unavailable", "unknown"}:
                self._seen_cleaning.discard(registry_id)
            else:
                self._seen_cleaning.add(registry_id)
            if task := self._settle_tasks.pop(registry_id, None):
                task.cancel()
            return
        if registry_id not in self._seen_cleaning or new_state not in {
            "docked",
            "idle",
        }:
            return
        self._seen_cleaning.discard(registry_id)
        if task := self._settle_tasks.pop(registry_id, None):
            task.cancel()

        async def capture_after_settle() -> None:
            try:
                await asyncio.sleep(_SETTLE_DELAY.total_seconds())
                current = self._dependencies.robot_for_registry_id(registry_id)
                if current is not None:
                    await self.async_capture(
                        current.entity_id,
                        trigger="post_clean",
                    )
            except asyncio.CancelledError:
                raise
            except MapRecoveryError:
                _LOGGER.debug(
                    "Post-clean map capture was unavailable",
                    exc_info=True,
                )
            finally:
                self._settle_tasks.pop(registry_id, None)

        self._settle_tasks[registry_id] = self._hass.async_create_task(
            capture_after_settle()
        )

    def preview(
        self,
        entity_id: str,
        snapshot_id: str | None = None,
        map_id: str | None = None,
    ) -> bytes | None:
        """Return an archived preview without exposing a raw map packet."""

        robot = self._dependencies.robot_for_entity_id(entity_id)
        if robot is None or self._archive is None or self._storage_error:
            return None
        captures = self._robot_archive(robot.registry_id).capture_sets
        selected = (
            next(
                (capture for capture in captures if capture.snapshot_id == snapshot_id),
                None,
            )
            if snapshot_id
            else (captures[-1] if captures else None)
        )
        if selected is None:
            return None
        record = (
            next((item for item in selected.maps if item.map_id == map_id), None)
            if map_id
            else (selected.maps[0] if selected.maps else None)
        )
        return record.preview_png if record else None

    def _preview_entries(
        self,
        entity_id: str,
    ) -> tuple[tuple[str, str, str], ...]:
        robot = self._dependencies.robot_for_entity_id(entity_id)
        if robot is None or self._archive is None or self._storage_error:
            return ()
        entries: list[tuple[str, str, str]] = []
        captures = self._robot_archive(robot.registry_id).capture_sets
        for capture in reversed(captures):
            captured_at = capture.captured_at.isoformat()
            for record in capture.maps:
                entries.append(
                    (
                        f"{record.name} - {captured_at}",
                        capture.snapshot_id,
                        record.map_id,
                    )
                )
        return tuple(entries)

    def preview_options(self, entity_id: str) -> tuple[str, ...]:
        """Return stable labels for cached preview choices."""

        return tuple(entry[0] for entry in self._preview_entries(entity_id))

    def selected_preview_option(self, entity_id: str) -> str | None:
        """Return the selected preview label, defaulting to the newest map."""

        entries = self._preview_entries(entity_id)
        if not entries:
            return None
        robot = self._dependencies.robot_for_entity_id(entity_id)
        if (
            robot
            and (selected := self._preview_selection.get(robot.registry_id))
            and (
                match := next(
                    (
                        label
                        for label, snapshot_id, map_id in entries
                        if (snapshot_id, map_id) == selected
                    ),
                    None,
                )
            )
        ):
            return match
        return entries[0][0]

    def select_preview_option(self, entity_id: str, option: str) -> None:
        """Select one cached preview without contacting the robot."""

        robot = self._robot(entity_id)
        entry = next(
            (
                candidate
                for candidate in self._preview_entries(entity_id)
                if candidate[0] == option
            ),
            None,
        )
        if entry is None:
            raise MapRecoveryError("selected map preview is no longer available")
        _, snapshot_id, map_id = entry
        self._preview_selection[robot.registry_id] = (snapshot_id, map_id)
        self._publish()

    def selected_preview(self, entity_id: str) -> bytes | None:
        """Return the selected cached preview image."""

        option = self.selected_preview_option(entity_id)
        if not option:
            return None
        entry = next(
            (
                candidate
                for candidate in self._preview_entries(entity_id)
                if candidate[0] == option
            ),
            None,
        )
        if entry is None:
            return None
        _, snapshot_id, map_id = entry
        return self.preview(
            entity_id,
            snapshot_id=snapshot_id,
            map_id=map_id,
        )
