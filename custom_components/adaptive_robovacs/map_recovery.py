"""Non-root, capability-gated retained-map recovery for B01/Q10 vacuums.

This module deliberately has one narrow vendor boundary.  It reuses the
already-authenticated Home Assistant Roborock runtime when that private runtime
offers the needed Q10 stream methods.  Missing or changed private internals are
reported as an unavailable optional feature; they must never affect scheduler
dispatch or cause another MQTT login.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import hashlib
import logging
from typing import Any, Protocol
from uuid import uuid4

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    MAP_RECOVERY_RETENTION,
    MAP_RECOVERY_STORAGE_KEY,
    MAP_RECOVERY_STORE_VERSION,
    SIGNAL_DISCOVERY_UPDATED,
)
from .q10_map_frame import Q10MapFrame, Q10MapFrameError, parse_q10_map_frame, render_q10_map_preview


_LOGGER = logging.getLogger(__name__)
_CAPTURE_TIMEOUT = 5.0
_MAX_MAP_SLOTS = 8
_SETTLE_DELAY = timedelta(seconds=60)


class MapRecoveryError(RuntimeError):
    """A safe, user-presentable recovery failure."""


class MapRecoveryUnavailable(MapRecoveryError):
    """The installed HA Roborock runtime cannot provide Q10 map recovery."""


@dataclass(frozen=True, slots=True)
class RetainedMap:
    """One map slot reported live by the robot."""

    map_id: str
    name: str
    timestamp: str | None = None

    def as_response(self) -> dict[str, str | None]:
        return {"map_id": self.map_id, "name": self.name, "timestamp": self.timestamp}


@dataclass(frozen=True, slots=True)
class RecoveryCapability:
    """A stable, redaction-safe description of the optional bridge state."""

    state: str
    reason: str | None = None

    @property
    def available(self) -> bool:
        return self.state == "ready"


class Q10MapTransport(Protocol):
    """Small testable protocol boundary around the private Roborock runtime."""

    async def async_list_maps(self) -> list[RetainedMap]: ...

    async def async_get_map(self, map_id: str) -> Q10MapFrame: ...

    async def async_apply_map(self, map_id: str) -> None: ...


def _mapping(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _extract_map_list(value: object, multi_map_code: str) -> list[RetainedMap] | None:
    """Extract a MULTI_MAP list response across supported library shapes."""

    source = _mapping(value)
    if source is None:
        return None
    for key in (multi_map_code, int(multi_map_code), "data", "dps"):
        nested = source.get(key)
        if isinstance(nested, Mapping):
            found = _extract_map_list(nested, multi_map_code)
            if found is not None:
                return found
        if isinstance(nested, list):
            source = {"data": nested}
            break
    records = source.get("data")
    if not isinstance(records, list):
        return None
    maps: list[RetainedMap] = []
    for item in records:
        record = _mapping(item)
        if record is None or record.get("id") is None:
            continue
        maps.append(
            RetainedMap(
                map_id=str(record["id"]),
                name=str(record.get("name") or f"Map {record['id']}"),
                timestamp=str(record["timestamp"]) if record.get("timestamp") is not None else None,
            )
        )
    return maps


def _extract_bytes(message: object) -> bytes | None:
    """Find a binary Q10 map payload without assuming one library message class."""

    if isinstance(message, bytes):
        return message
    if isinstance(message, bytearray):
        return bytes(message)
    if isinstance(message, tuple):
        for item in message:
            found = _extract_bytes(item)
            if found is not None:
                return found
    if isinstance(message, Mapping):
        for key in ("payload", "data", "raw", "message"):
            if key in message:
                found = _extract_bytes(message[key])
                if found is not None:
                    return found
    for name in ("payload", "data", "raw"):
        found = _extract_bytes(getattr(message, name, None))
        if found is not None:
            return found
    return None


class Q10MapProtocolBridge:
    """Adapter for the current private Q10 runtime with strict capability checks."""

    def __init__(self, api: Any) -> None:
        self._api = api
        self._lock = asyncio.Lock()
        self._common: Any = None
        self._multi_map_code: str | None = None
        self._decode_rpc_response: Any = None
        self._channel: Any = None

    @classmethod
    def from_api(cls, api: Any) -> Q10MapProtocolBridge:
        bridge = cls(api)
        try:
            from roborock.data.b01_q10.b01_q10_code_mappings import B01_Q10_DP
            from roborock.protocols.b01_q10_protocol import decode_rpc_response
        except (ImportError, AttributeError) as err:
            raise MapRecoveryUnavailable("unsupported Home Assistant Roborock runtime") from err
        command = getattr(api, "command", None)
        if not callable(getattr(command, "send", None)):
            raise MapRecoveryUnavailable("Q10 command channel is unavailable")
        common = getattr(B01_Q10_DP, "COMMON", None)
        multi_map = getattr(B01_Q10_DP, "MULTI_MAP", None)
        code = getattr(multi_map, "code", None)
        if common is None or code is None:
            raise MapRecoveryUnavailable("Q10 multi-map protocol is unavailable")
        channel = next(
            (
                candidate
                for candidate in (
                    getattr(api, "channel", None),
                    getattr(api, "_channel", None),
                    getattr(getattr(api, "_api", None), "channel", None),
                    getattr(getattr(api, "_api", None), "_channel", None),
                )
                if callable(getattr(candidate, "subscribe_stream", None))
            ),
            None,
        )
        if channel is None:
            raise MapRecoveryUnavailable("Q10 map stream is unavailable")
        bridge._common = common
        bridge._multi_map_code = str(code)
        bridge._decode_rpc_response = decode_rpc_response
        bridge._channel = channel
        return bridge

    async def _async_stream(self) -> AsyncIterator[Any]:
        """Yield stream data from a supported ``subscribe_stream`` implementation."""

        subscription = self._channel.subscribe_stream()
        if hasattr(subscription, "__aenter__"):
            async with subscription as stream:
                async for item in stream:
                    yield item
            return
        if hasattr(subscription, "__aiter__"):
            try:
                async for item in subscription:
                    yield item
            finally:
                close = getattr(subscription, "aclose", None)
                if callable(close):
                    await close()
            return
        raise MapRecoveryUnavailable("Q10 map stream has an unsupported shape")

    def _decode_message(self, message: object) -> object:
        if isinstance(message, Mapping):
            return message
        payload = _extract_bytes(message)
        if payload is None or payload[:2] == b"\x01\x01":
            return message
        try:
            return self._decode_rpc_response(payload)
        except Exception:  # Third-party protocol errors are never dashboard text.
            return message

    async def _async_send_common(self, value: Mapping[str, object]) -> None:
        try:
            await self._api.command.send(self._common, {self._multi_map_code: dict(value)})
        except Exception as err:
            raise MapRecoveryError("Roborock rejected the map request") from err

    async def _async_wait_for(self, predicate) -> object:
        stream = self._async_stream()
        deadline = asyncio.get_running_loop().time() + _CAPTURE_TIMEOUT
        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise MapRecoveryError("Timed out waiting for the robot's map data")
                try:
                    item = await asyncio.wait_for(anext(stream), timeout=remaining)
                except TimeoutError as err:
                    raise MapRecoveryError(
                        "Timed out waiting for the robot's map data"
                    ) from err
                value = predicate(item)
                if value is not None:
                    return value
        finally:
            await stream.aclose()

    async def async_list_maps(self) -> list[RetainedMap]:
        async with self._lock:
            waiter = asyncio.create_task(
                self._async_wait_for(
                    lambda message: _extract_map_list(
                        self._decode_message(message), self._multi_map_code or "61"
                    )
                )
            )
            try:
                await asyncio.sleep(0)
                await self._async_send_common({"op": "list"})
                result = await waiter
            except BaseException:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
                raise
            maps = result if isinstance(result, list) else []
            if not maps:
                raise MapRecoveryError("the robot did not report any retained maps")
            if len(maps) > _MAX_MAP_SLOTS:
                raise MapRecoveryError("the robot reported too many retained maps")
            return maps

    async def async_get_map(self, map_id: str) -> Q10MapFrame:
        async with self._lock:
            expected = str(map_id)

            def match(message: object) -> Q10MapFrame | None:
                packet = _extract_bytes(message)
                if packet is None or not packet.startswith(b"\x01\x01"):
                    return None
                try:
                    frame = parse_q10_map_frame(packet)
                except Q10MapFrameError:
                    return None
                return frame if frame.map_id == expected else None

            waiter = asyncio.create_task(self._async_wait_for(match))
            try:
                await asyncio.sleep(0)
                await self._async_send_common({"op": "get", "id": expected})
                return await waiter
            except BaseException:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
                raise

    async def async_apply_map(self, map_id: str) -> None:
        async with self._lock:
            await self._async_send_common({"op": "apply", "id": str(map_id)})


class Q10RuntimeResolver:
    """Resolve a Q10 API through registry identity, never friendly names."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    def async_resolve(self, robot: Any) -> Q10MapTransport:
        if robot.platform != "roborock" or not robot.device_id:
            raise MapRecoveryUnavailable("not a Q10 Roborock vacuum")
        from homeassistant.helpers import device_registry as dr

        device = dr.async_get(self.hass).async_get(robot.device_id)
        if device is None:
            raise MapRecoveryUnavailable("vacuum device registry entry is unavailable")
        duids = {
            str(identifier[1])
            for identifier in device.identifiers
            if len(identifier) == 2 and identifier[0] == "roborock"
        }
        if len(duids) != 1:
            raise MapRecoveryUnavailable("vacuum cannot be matched to one Roborock device")
        candidates: list[Any] = []
        for entry in self.hass.config_entries.async_entries("roborock"):
            runtime_data = getattr(entry, "runtime_data", None)
            coordinators = getattr(runtime_data, "b01_q10", ()) or ()
            if isinstance(coordinators, Mapping):
                coordinators = coordinators.values()
            elif not isinstance(coordinators, Sequence) or isinstance(
                coordinators, (str, bytes)
            ):
                coordinators = ()
            for coordinator in coordinators:
                if str(getattr(coordinator, "duid", "")) in duids:
                    candidates.append(coordinator)
        if len(candidates) != 1:
            raise MapRecoveryUnavailable("Q10 Home Assistant runtime is unavailable or ambiguous")
        return Q10MapProtocolBridge.from_api(getattr(candidates[0], "api", None))


class MapRecoveryManager:
    """Persist capture sets and coordinate safe, no-motion recovery actions."""

    def __init__(self, coordinator: Any) -> None:
        self.coordinator = coordinator
        self.hass: HomeAssistant = coordinator.hass
        self.store: Store[dict[str, Any]] = Store(
            self.hass,
            MAP_RECOVERY_STORE_VERSION,
            f"{MAP_RECOVERY_STORAGE_KEY}.{coordinator.entry.entry_id}",
        )
        self._data: dict[str, Any] = {"schema_version": 1, "robots": {}}
        self._resolver = Q10RuntimeResolver(self.hass)
        self._lock_by_robot: dict[str, asyncio.Lock] = {}
        self._settle_tasks: dict[str, asyncio.Task[Any]] = {}
        self._seen_cleaning: set[str] = set()
        self._preview_selection: dict[str, tuple[str, str]] = {}
        self._storage_error: str | None = None

    async def async_initialize(self) -> None:
        try:
            stored = await self.store.async_load()
        except Exception:
            _LOGGER.exception("Adaptive RoboVacs could not load map recovery storage")
            self._storage_error = "map recovery storage could not be loaded"
            return
        if stored is None:
            return
        if (
            isinstance(stored, Mapping)
            and stored.get("schema_version") == 1
            and isinstance(stored.get("robots"), Mapping)
            and all(
                isinstance(robot_data, Mapping)
                and isinstance(robot_data.get("capture_sets", []), list)
                for robot_data in stored["robots"].values()
            )
        ):
            self._data = {"schema_version": 1, "robots": dict(stored["robots"])}
            return
        _LOGGER.error("Adaptive RoboVacs map recovery storage is malformed; recovery is disabled")
        self._storage_error = "map recovery storage is malformed"

    async def async_shutdown(self) -> None:
        for task in self._settle_tasks.values():
            task.cancel()
        if self._settle_tasks:
            await asyncio.gather(*self._settle_tasks.values(), return_exceptions=True)
        self._settle_tasks.clear()

    def _robot_key(self, robot: Any) -> str:
        return str(robot.registry_id)

    def _lock(self, robot: Any) -> asyncio.Lock:
        return self._lock_by_robot.setdefault(self._robot_key(robot), asyncio.Lock())

    async def _async_save(self) -> None:
        await self.store.async_save(self._data)

    def _robot_store(self, robot: Any) -> dict[str, Any]:
        if self._storage_error:
            raise MapRecoveryUnavailable(self._storage_error)
        return self._data["robots"].setdefault(self._robot_key(robot), {"capture_sets": [], "last_error": None})

    def _robot(self, entity_id: str) -> Any:
        if self._storage_error:
            raise MapRecoveryUnavailable(self._storage_error)
        robot = self.coordinator.discovery.robots.get(entity_id)
        if robot is None:
            raise MapRecoveryError("robot is not discovered by this config entry")
        return robot

    def capability(self, entity_id: str) -> RecoveryCapability:
        if self._storage_error:
            return RecoveryCapability("unavailable", self._storage_error)
        robot = self.coordinator.discovery.robots.get(entity_id)
        if robot is None:
            return RecoveryCapability("unavailable", "robot is not discovered")
        try:
            self._resolver.async_resolve(robot)
        except MapRecoveryUnavailable as err:
            capability = RecoveryCapability("unavailable", str(err))
        else:
            capability = RecoveryCapability("ready")
        return capability

    def summary(self, entity_id: str) -> dict[str, Any]:
        robot = self.coordinator.discovery.robots.get(entity_id)
        if robot is None:
            return {"state": "unavailable", "reason": "robot is not discovered"}
        capability = self.capability(entity_id)
        if self._storage_error:
            return {
                "state": "unavailable",
                "reason": self._storage_error,
                "retention": MAP_RECOVERY_RETENTION,
                "capture_count": 0,
                "recovery_pending": self._is_held(entity_id),
                "capture_sets": [],
                "available_maps": [],
            }
        stored = self._robot_store(robot)
        capture_sets = stored.get("capture_sets", [])
        latest = capture_sets[-1] if capture_sets and isinstance(capture_sets[-1], Mapping) else {}
        return {
            "state": "recovery pending" if self._is_held(entity_id) else capability.state,
            "reason": capability.reason,
            "retention": MAP_RECOVERY_RETENTION,
            "capture_count": len(capture_sets) if isinstance(capture_sets, list) else 0,
            "last_capture": capture_sets[-1].get("captured_at") if capture_sets else None,
            "last_error": stored.get("last_error"),
            "recovery_pending": self._is_held(entity_id),
            "capture_sets": [
                {
                    "snapshot_id": item.get("snapshot_id"),
                    "captured_at": item.get("captured_at"),
                    "trigger": item.get("trigger"),
                    "map_count": len(item.get("maps", [])),
                }
                for item in capture_sets[-MAP_RECOVERY_RETENTION:]
                if isinstance(item, Mapping)
            ],
            "available_maps": [
                {
                    "map_id": item.get("map_id"),
                    "name": item.get("name"),
                    "timestamp": item.get("robot_timestamp"),
                }
                for item in latest.get("maps", [])
                if isinstance(item, Mapping) and item.get("map_id") is not None
            ],
        }

    def _is_held(self, entity_id: str) -> bool:
        hold = self.coordinator.data.get("robot_holds", {}).get(entity_id, {})
        return isinstance(hold, Mapping) and hold.get("reason") == "map_recovery_pending"

    def _terminal(self, entity_id: str) -> bool:
        state = self.hass.states.get(entity_id)
        return bool(state and state.state in {"docked", "idle"})

    async def async_list_maps(self, entity_id: str) -> dict[str, Any]:
        robot = self._robot(entity_id)
        async with self._lock(robot):
            try:
                bridge = self._resolver.async_resolve(robot)
                maps = await bridge.async_list_maps()
            except MapRecoveryError:
                raise
            except Exception as err:
                _LOGGER.debug("Map list failed for registered robot %s", robot.registry_id, exc_info=True)
                raise MapRecoveryError("Could not retrieve the robot's retained maps") from err
        return {**self.summary(entity_id), "retained_maps": [item.as_response() for item in maps]}

    async def _async_capture(self, robot: Any, trigger: str, *, force: bool) -> dict[str, Any]:
        if not self._terminal(robot.entity_id):
            raise MapRecoveryError("robot must be docked or idle to capture maps")
        bridge = self._resolver.async_resolve(robot)
        maps = await bridge.async_list_maps()
        if not maps or len(maps) > _MAX_MAP_SLOTS:
            raise MapRecoveryError("robot did not report a safe retained-map list")
        captured: list[dict[str, Any]] = []
        for slot in maps:
            frame = await bridge.async_get_map(slot.map_id)
            captured.append(
                {
                    "map_id": slot.map_id,
                    "name": slot.name,
                    "robot_timestamp": slot.timestamp,
                    "packet_sha256": frame.sha256,
                    "packet_b64": base64.b64encode(frame.packet).decode("ascii"),
                    "preview_png_b64": base64.b64encode(render_q10_map_preview(frame)).decode("ascii"),
                    "decoded_summary": {
                        "width": frame.width,
                        "height": frame.height,
                        "rooms": [asdict(room) for room in frame.rooms],
                    },
                }
            )
        combined = hashlib.sha256("".join(item["packet_sha256"] for item in captured).encode()).hexdigest()
        stored = self._robot_store(robot)
        previous = stored.get("capture_sets", [])
        if not force and previous and previous[-1].get("combined_sha256") == combined:
            stored["last_error"] = None
            await self._async_save()
            return {"snapshot_id": previous[-1].get("snapshot_id"), "deduplicated": True, "map_count": len(captured)}
        snapshot = {
            "snapshot_id": str(uuid4()),
            "captured_at": dt_util.utcnow().isoformat(),
            "trigger": trigger,
            "active_map_id": None,
            "combined_sha256": combined,
            "maps": captured,
        }
        sets = [item for item in previous if isinstance(item, Mapping)]
        sets.append(snapshot)
        stored["capture_sets"] = sets[-MAP_RECOVERY_RETENTION:]
        stored["last_error"] = None
        await self._async_save()
        return {"snapshot_id": snapshot["snapshot_id"], "deduplicated": False, "map_count": len(captured), "digest": combined}

    async def async_capture(self, entity_id: str, *, trigger: str = "manual") -> dict[str, Any]:
        robot = self._robot(entity_id)
        async with self._lock(robot):
            try:
                result = await self._async_capture(robot, trigger, force=trigger == "manual")
            except (MapRecoveryError, Q10MapFrameError) as err:
                if not self._storage_error:
                    self._robot_store(robot)["last_error"] = str(err)
                    await self._async_save()
                raise MapRecoveryError(str(err)) from err
        self.coordinator._notify_listeners()
        async_dispatcher_send(self.hass, SIGNAL_DISCOVERY_UPDATED, self.coordinator.entry.entry_id)
        return result

    async def async_activate(self, entity_id: str, map_id: str, *, confirm: bool) -> dict[str, Any]:
        if not confirm:
            raise MapRecoveryError("activation requires confirm: true")
        robot = self._robot(entity_id)
        if self.coordinator.observe_only:
            raise MapRecoveryError("observe-only mode is enabled")
        if self.coordinator.party_mode:
            raise MapRecoveryError("party mode is enabled")
        if self.coordinator.data.get("active", {}).get(entity_id):
            raise MapRecoveryError("robot has an active scheduler job")
        if self._is_held(entity_id):
            raise MapRecoveryError("map recovery verification is already pending")
        if not self._terminal(entity_id):
            raise MapRecoveryError("robot must be docked or idle to activate a map")
        lock = self._lock(robot)
        if lock.locked():
            raise MapRecoveryError("another map recovery operation is already running")
        async with lock:
            bridge = self._resolver.async_resolve(robot)
            maps = await bridge.async_list_maps()
            if str(map_id) not in {item.map_id for item in maps}:
                raise MapRecoveryError("requested map is no longer retained by the robot")
            now = dt_util.utcnow().isoformat()
            self.coordinator.data.setdefault("robot_holds", {})[entity_id] = {
                "reason": "map_recovery_pending",
                "phase": "manual_verification",
                "requested_map_id": str(map_id),
                "held_at": now,
                "last_observed_at": now,
            }
            await self.coordinator._async_save()
            try:
                before = await self._async_capture(robot, "pre_activation", force=True)
            except Exception:
                # No robot mutation has occurred yet, so a failed safety read
                # must not create a scheduler hold the user cannot explain.
                self.coordinator.data["robot_holds"].pop(entity_id, None)
                await self.coordinator._async_save()
                raise
            await bridge.async_apply_map(str(map_id))
            confirmed = False
            try:
                refreshed = await bridge.async_list_maps()
                if str(map_id) in {item.map_id for item in refreshed}:
                    frame = await bridge.async_get_map(str(map_id))
                    confirmed = frame.map_id == str(map_id)
            except MapRecoveryError:
                pass
        self.coordinator._notify_listeners()
        return {
            "pre_activation_snapshot_id": before.get("snapshot_id"),
            "requested_map_id": str(map_id),
            "activation": "confirmed" if confirmed else "requested_unverified",
            "recovery_pending": True,
        }

    async def async_verify(self, entity_id: str, *, confirm: bool) -> dict[str, Any]:
        if not confirm:
            raise MapRecoveryError("verification requires confirm: true")
        if not self._is_held(entity_id):
            raise MapRecoveryError("no map recovery verification is pending")
        if not self._terminal(entity_id) or self.coordinator.data.get("active", {}).get(entity_id):
            raise MapRecoveryError("robot must be docked or idle with no active job")
        robot = self._robot(entity_id)
        lock = self._lock(robot)
        if lock.locked():
            raise MapRecoveryError("another map recovery operation is already running")
        async with lock:
            # Refresh registry-derived robot mapping before releasing the
            # explicit hold, then prove that the selected retained frame and
            # every currently assigned room segment still agree with HA.
            await self.coordinator.async_refresh_discovery(notify=False)
            robot = self._robot(entity_id)
            hold = self.coordinator.data.get("robot_holds", {}).get(entity_id, {})
            requested_map_id = str(hold.get("requested_map_id", ""))
            if not requested_map_id:
                raise MapRecoveryError("the pending recovery has no selected map")
            bridge = self._resolver.async_resolve(robot)
            retained = await bridge.async_list_maps()
            if requested_map_id not in {item.map_id for item in retained}:
                raise MapRecoveryError("the selected map is no longer retained by the robot")
            frame = await bridge.async_get_map(requested_map_id)
            if frame.map_id != requested_map_id:
                raise MapRecoveryError("the selected retained map could not be verified")
            self._preflight_room_mapping(robot)
            self.coordinator.data["robot_holds"].pop(entity_id, None)
            await self.coordinator._async_save()
        result = await self.coordinator.async_evaluate(dry_run=True, reason="map-recovery-verified")
        self.coordinator._notify_listeners()
        return {"verified": True, "preview": result}

    def _preflight_room_mapping(self, robot: Any) -> None:
        """Reuse the existing Roborock segment-map proof without dispatching."""

        if not robot.supports_area_clean:
            raise MapRecoveryError("Home Assistant area mapping is unavailable")
        from homeassistant.helpers import entity_registry as er
        from .adapters.roborock import RoborockMappingError, resolve_roborock_area_mapping

        entry = er.async_get(self.hass).async_get(robot.entity_id)
        options = getattr(entry, "options", {}) if entry else {}
        vacuum_options = options.get("vacuum", {}) if isinstance(options, Mapping) else {}
        mapping = vacuum_options.get("area_mapping") if isinstance(vacuum_options, Mapping) else None
        if not isinstance(mapping, Mapping) or not mapping:
            raise MapRecoveryError("Home Assistant area mapping is unavailable")
        try:
            for area_id in mapping:
                resolve_roborock_area_mapping(vacuum_options, (str(area_id),))
        except RoborockMappingError as err:
            raise MapRecoveryError("Home Assistant room mapping needs to be refreshed") from err

    @callback
    def handle_state_transition(self, entity_id: str, old_state: str | None, new_state: str | None) -> None:
        """Schedule a post-clean capture from observed robot state only."""

        if entity_id not in self.coordinator.discovery.robots:
            return
        if new_state in {"cleaning", "returning", "unavailable", "unknown"}:
            if new_state in {"unavailable", "unknown"}:
                self._seen_cleaning.discard(entity_id)
            else:
                self._seen_cleaning.add(entity_id)
            task = self._settle_tasks.pop(entity_id, None)
            if task:
                task.cancel()
            return
        if entity_id not in self._seen_cleaning or new_state not in {"docked", "idle"}:
            return
        self._seen_cleaning.discard(entity_id)
        task = self._settle_tasks.pop(entity_id, None)
        if task:
            task.cancel()

        async def capture_after_settle() -> None:
            try:
                await asyncio.sleep(_SETTLE_DELAY.total_seconds())
                await self.async_capture(entity_id, trigger="post_clean")
            except asyncio.CancelledError:
                raise
            except MapRecoveryError:
                _LOGGER.debug("Post-clean map capture was unavailable", exc_info=True)
            finally:
                self._settle_tasks.pop(entity_id, None)

        self._settle_tasks[entity_id] = self.hass.async_create_task(capture_after_settle())

    def preview(self, entity_id: str, snapshot_id: str | None = None, map_id: str | None = None) -> bytes | None:
        """Return an archived preview image without exposing raw map bytes."""

        robot = self.coordinator.discovery.robots.get(entity_id)
        if robot is None:
            return None
        sets = self._robot_store(robot).get("capture_sets", [])
        selected = next((item for item in sets if item.get("snapshot_id") == snapshot_id), None) if snapshot_id else (sets[-1] if sets else None)
        if not isinstance(selected, Mapping):
            return None
        maps = selected.get("maps", [])
        record = next((item for item in maps if item.get("map_id") == map_id), None) if map_id else (maps[0] if maps else None)
        if not isinstance(record, Mapping) or not isinstance(record.get("preview_png_b64"), str):
            return None
        try:
            return base64.b64decode(record["preview_png_b64"], validate=True)
        except ValueError:
            return None

    def preview_options(self, entity_id: str) -> tuple[str, ...]:
        """Return read-only snapshot/map choices for the preview select."""

        robot = self.coordinator.discovery.robots.get(entity_id)
        if robot is None:
            return ()
        options: list[str] = []
        for capture in reversed(self._robot_store(robot).get("capture_sets", [])):
            if not isinstance(capture, Mapping):
                continue
            captured_at = str(capture.get("captured_at", "unknown"))
            snapshot_id = str(capture.get("snapshot_id", ""))
            for record in capture.get("maps", []):
                if isinstance(record, Mapping) and record.get("map_id") is not None:
                    options.append(
                        f"{snapshot_id}|{record['map_id']}|{captured_at}|{record.get('name', 'map')}"
                    )
        return tuple(options)

    def selected_preview_option(self, entity_id: str) -> str | None:
        options = self.preview_options(entity_id)
        if not options:
            return None
        selected = self._preview_selection.get(entity_id)
        if selected:
            prefix = f"{selected[0]}|{selected[1]}|"
            match = next((option for option in options if option.startswith(prefix)), None)
            if match:
                return match
        return options[0]

    def select_preview_option(self, entity_id: str, option: str) -> None:
        if option not in self.preview_options(entity_id):
            raise MapRecoveryError("selected map preview is no longer available")
        snapshot_id, map_id, *_ = option.split("|", 3)
        self._preview_selection[entity_id] = (snapshot_id, map_id)
        self.coordinator._notify_listeners()

    def selected_preview(self, entity_id: str) -> bytes | None:
        option = self.selected_preview_option(entity_id)
        if not option:
            return None
        snapshot_id, map_id, *_ = option.split("|", 3)
        return self.preview(entity_id, snapshot_id=snapshot_id, map_id=map_id)
