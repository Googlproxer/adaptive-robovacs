"""Roborock bridge for Q10 retained-map recovery.

The bridge deliberately reuses Home Assistant's already-authenticated runtime.
It never creates a second vendor session or stores credentials.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .discovery import DiscoveredRobot
from .map_recovery_models import (
    MapRecoveryError,
    MapRecoveryUnavailable,
    RetainedMap,
)
from .q10_map_frame import Q10MapFrame, Q10MapFrameError, parse_q10_map_frame

_CAPTURE_TIMEOUT = 5.0
_LIST_TIMEOUT = 15.0
_LIST_REQUEST_ATTEMPTS = 3
_LIST_RETRY_INTERVAL = 4.0
_FRAME_TIMEOUT = 25.0
_FRAME_REQUEST_ATTEMPTS = 3
_FRAME_RETRY_INTERVAL = 4.0
_MAX_MAP_SLOTS = 8

type ResponsePredicate = Callable[[object], object | None]
type RequestSender = Callable[[Mapping[str, object]], Awaitable[None]]


class Q10MapTransport(Protocol):
    """Small structural boundary around one existing Roborock runtime."""

    async def async_list_maps(self) -> list[RetainedMap]:
        """Return every retained map slot."""

        ...

    async def async_get_map(self, map_id: str) -> Q10MapFrame:
        """Return one validated retained-map frame."""

        ...

    async def async_apply_map(self, map_id: str) -> None:
        """Ask the robot to activate one retained map."""

        ...


def _mapping(value: object) -> Mapping[object, object] | None:
    return value if isinstance(value, Mapping) else None


def _extract_map_list(value: object, multi_map_code: str) -> list[RetainedMap] | None:
    """Extract a MULTI_MAP list response across supported library shapes."""

    source = _mapping(value)
    if source is None:
        return None
    operation = source.get("op")
    if operation is not None and operation != "list":
        return None
    keys: tuple[object, ...] = (multi_map_code, "data", "dps")
    if multi_map_code.isdecimal():
        keys = (multi_map_code, int(multi_map_code), "data", "dps")
    for key in keys:
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
        map_id = str(record["id"])
        maps.append(
            RetainedMap(
                map_id=map_id,
                name=str(record.get("name") or f"Map {map_id}"),
                timestamp=(
                    str(record["timestamp"])
                    if record.get("timestamp") is not None
                    else None
                ),
            )
        )
    return maps


def _extract_bytes(message: object) -> bytes | None:
    """Find a binary map payload without assuming one library message class."""

    if message is None:
        return None
    if isinstance(message, bytes):
        return message
    if isinstance(message, bytearray):
        return bytes(message)
    if isinstance(message, memoryview):
        return message.tobytes()
    if isinstance(message, tuple):
        for item in message:
            if (found := _extract_bytes(item)) is not None:
                return found
    if isinstance(message, Mapping):
        for key in ("payload", "data", "raw", "message"):
            if key in message and (found := _extract_bytes(message[key])) is not None:
                return found
    for name in ("payload", "data", "raw"):
        value = getattr(message, name, None)
        if (
            value is not None
            and value is not message
            and (found := _extract_bytes(value)) is not None
        ):
            return found
    return None


def _normalise_dps(value: object) -> Mapping[str, object] | None:
    """Return a string-keyed DPS mapping from current library message shapes."""

    source = getattr(value, "dps", value)
    if not isinstance(source, Mapping):
        return None
    return {str(getattr(key, "code", key)): item for key, item in source.items()}


class Q10MapProtocolBridge(Q10MapTransport):
    """Adapter for the private Q10 runtime with strict capability checks."""

    def __init__(self, api: Any) -> None:
        self._api = api
        self._lock = asyncio.Lock()
        self._common: Any = None
        self._multi_map: Any = None
        self._multi_map_code: str | None = None
        self._decode_rpc_response: Any = None
        self._channel: Any = None

    @classmethod
    def from_api(cls, api: Any) -> Q10MapProtocolBridge:
        """Create a bridge only when every required private API is present."""

        bridge = cls(api)
        try:
            from roborock.data.b01_q10.b01_q10_code_mappings import B01_Q10_DP
            from roborock.protocols.b01_q10_protocol import decode_rpc_response
        except (ImportError, AttributeError) as err:
            raise MapRecoveryUnavailable(
                "unsupported Home Assistant Roborock runtime"
            ) from err
        command = getattr(api, "command", None)
        if not callable(getattr(command, "send", None)):
            raise MapRecoveryUnavailable("Q10 command channel is unavailable")
        common = getattr(B01_Q10_DP, "COMMON", None)
        multi_map = getattr(B01_Q10_DP, "MULTI_MAP", None)
        code = getattr(multi_map, "code", None)
        if common is None or code is None:
            raise MapRecoveryUnavailable("Q10 multi-map protocol is unavailable")
        channel_roots = (
            getattr(api, "channel", None),
            getattr(api, "_channel", None),
            getattr(getattr(api, "_api", None), "channel", None),
            getattr(getattr(api, "_api", None), "_channel", None),
        )
        channel = next(
            (
                candidate
                for root in channel_roots
                for candidate in (getattr(root, "_mqtt_channel", None), root)
                if callable(getattr(candidate, "subscribe_stream", None))
            ),
            None,
        )
        if channel is None:
            raise MapRecoveryUnavailable("Q10 map stream is unavailable")
        bridge._common = common
        bridge._multi_map = multi_map
        bridge._multi_map_code = str(code)
        bridge._decode_rpc_response = decode_rpc_response
        bridge._channel = channel
        return bridge

    async def _async_stream(self) -> AsyncGenerator[object]:
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
        if (dps := _normalise_dps(message)) is not None:
            return dps
        payload = _extract_bytes(message)
        if payload is None or payload[:2] == b"\x01\x01":
            return message
        try:
            decoded = self._decode_rpc_response(message)
        except Exception:  # Third-party protocol details stay out of UI errors.
            try:
                decoded = self._decode_rpc_response(payload)
            except Exception:
                return message
        return _normalise_dps(decoded) or decoded

    async def _async_send_common(self, value: Mapping[str, object]) -> None:
        try:
            await self._api.command.send(
                self._common,
                {self._multi_map_code: dict(value)},
            )
        except Exception as err:
            raise MapRecoveryError("Roborock rejected the map request") from err

    async def _async_send_active_frame_request(
        self, value: Mapping[str, object]
    ) -> None:
        try:
            await self._api.command.send(self._multi_map, dict(value))
            refresh = getattr(self._api, "refresh", None)
            if callable(refresh):
                await refresh()
        except Exception as err:
            raise MapRecoveryError("Roborock rejected the active map request") from err

    async def _async_wait_for(
        self,
        predicate: ResponsePredicate,
        *,
        timeout_seconds: float = _CAPTURE_TIMEOUT,
    ) -> object:
        stream = self._async_stream()
        deadline = asyncio.get_running_loop().time() + timeout_seconds
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
                if (value := predicate(item)) is not None:
                    return value
        finally:
            await stream.aclose()

    async def _async_request_and_wait(
        self,
        request: Mapping[str, object],
        predicate: ResponsePredicate,
        *,
        timeout_seconds: float,
        attempts: int = 1,
        retry_interval: float = 0.0,
        sender: RequestSender | None = None,
    ) -> object:
        waiter = asyncio.create_task(
            self._async_wait_for(predicate, timeout_seconds=timeout_seconds)
        )
        try:
            await asyncio.sleep(0)
            send = sender or self._async_send_common
            for attempt in range(attempts):
                await send(request)
                if attempt + 1 >= attempts:
                    break
                done, _ = await asyncio.wait({waiter}, timeout=retry_interval)
                if done:
                    return waiter.result()
            return await waiter
        except BaseException:
            # Cancellation must also close the private subscription promptly.
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            raise

    async def async_list_maps(self) -> list[RetainedMap]:
        async with self._lock:
            result = await self._async_request_and_wait(
                {"op": "list"},
                lambda message: _extract_map_list(
                    self._decode_message(message),
                    self._multi_map_code or "61",
                ),
                timeout_seconds=_LIST_TIMEOUT,
                attempts=_LIST_REQUEST_ATTEMPTS,
                retry_interval=_LIST_RETRY_INTERVAL,
            )
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

            result = await self._async_request_and_wait(
                {"op": "list"},
                match,
                timeout_seconds=_FRAME_TIMEOUT,
                attempts=_FRAME_REQUEST_ATTEMPTS,
                retry_interval=_FRAME_RETRY_INTERVAL,
                sender=self._async_send_active_frame_request,
            )
            if not isinstance(result, Q10MapFrame):
                raise MapRecoveryError("the robot returned an invalid map frame")
            return result

    async def async_apply_map(self, map_id: str) -> None:
        async with self._lock:
            await self._async_send_common({"op": "apply", "id": str(map_id)})


class Q10RuntimeResolver:
    """Resolve a Q10 API through registry identity, never friendly names."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def async_resolve(self, robot: DiscoveredRobot) -> Q10MapTransport:
        """Resolve one matching API from Home Assistant's live runtime."""

        if robot.platform != "roborock" or not robot.device_id:
            raise MapRecoveryUnavailable("not a Q10 Roborock vacuum")
        device = dr.async_get(self._hass).async_get(robot.device_id)
        if device is None:
            raise MapRecoveryUnavailable("vacuum device registry entry is unavailable")
        duids = {
            str(identifier[1])
            for identifier in device.identifiers
            if len(identifier) == 2 and identifier[0] == "roborock"
        }
        if len(duids) != 1:
            raise MapRecoveryUnavailable(
                "vacuum cannot be matched to one Roborock device"
            )
        candidates: list[object] = []
        for entry in self._hass.config_entries.async_entries("roborock"):
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
            raise MapRecoveryUnavailable(
                "Q10 Home Assistant runtime is unavailable or ambiguous"
            )
        return Q10MapProtocolBridge.from_api(getattr(candidates[0], "api", None))
