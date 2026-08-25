"""Q10 map-bridge tests with a small fake of Home Assistant's runtime."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
import types
import unittest


PACKAGE_PATH = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs"
PACKAGE_NAME = "adaptive_robovacs_map_bridge_test"
package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(PACKAGE_PATH)]
sys.modules[PACKAGE_NAME] = package

homeassistant = types.ModuleType("homeassistant")
core = types.ModuleType("homeassistant.core")
core.HomeAssistant = object
core.callback = lambda function: function
helpers = types.ModuleType("homeassistant.helpers")
dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
dispatcher.async_dispatcher_send = lambda *_args, **_kwargs: None
storage = types.ModuleType("homeassistant.helpers.storage")


class _Store:
    def __init__(self, *_args, **_kwargs) -> None:
        pass


storage.Store = _Store
util = types.ModuleType("homeassistant.util")
dt = types.ModuleType("homeassistant.util.dt")
dt.utcnow = lambda: datetime.now(timezone.utc)
util.dt = dt
sys.modules.update(
    {
        "homeassistant": homeassistant,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.dispatcher": dispatcher,
        "homeassistant.helpers.storage": storage,
        "homeassistant.util": util,
        "homeassistant.util.dt": dt,
    }
)

SPEC = importlib.util.spec_from_file_location(
    f"{PACKAGE_NAME}.map_recovery", PACKAGE_PATH / "map_recovery.py"
)
assert SPEC and SPEC.loader
recovery = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = recovery
SPEC.loader.exec_module(recovery)


def _literal_lz4(value: bytes) -> bytes:
    if len(value) < 15:
        return bytes([len(value) << 4]) + value
    return b"\xf0" + bytes([len(value) - 15]) + value


def _packet(map_id: int) -> bytes:
    grid = b"\x04\x04\x08\x08"
    record = bytearray(47)
    record[0:2] = (1).to_bytes(2, "big")
    record[26] = 4
    record[27:31] = b"Test"
    layout = grid + b"\x01\x01" + bytes(record)
    compressed = _literal_lz4(layout)
    header = bytearray(29)
    header[0:2] = b"\x01\x01"
    header[2:6] = map_id.to_bytes(4, "big")
    header[7:9] = (2).to_bytes(2, "big")
    header[9:11] = (2).to_bytes(2, "big")
    header[25:27] = len(layout).to_bytes(2, "big")
    header[27:29] = len(compressed).to_bytes(2, "big")
    return bytes(header) + compressed


class _Subscription:
    def __init__(self, queue: asyncio.Queue[object]) -> None:
        self.queue = queue
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self) -> object:
        return await self.queue.get()

    async def aclose(self) -> None:
        self.closed = True


class _Channel:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[object] = asyncio.Queue()
        self.subscriptions: list[_Subscription] = []

    def subscribe_stream(self) -> _Subscription:
        subscription = _Subscription(self.queue)
        self.subscriptions.append(subscription)
        return subscription


class _Command:
    def __init__(self, channel: _Channel) -> None:
        self.channel = channel
        self.calls: list[dict[str, object]] = []

    async def send(self, _common: object, payload: dict[str, object]) -> None:
        self.calls.append(payload)
        request = payload["61"]
        assert isinstance(request, dict)
        if request["op"] == "list":
            self.channel.queue.put_nowait(
                {"61": {"data": [{"id": "1234", "name": "Saved map"}]}}
            )
        elif request["op"] == "get":
            self.channel.queue.put_nowait(_packet(int(str(request["id"]))))


class _Api:
    def __init__(self) -> None:
        self.channel = _Channel()
        self.command = _Command(self.channel)


class _DpsKey:
    code = 61


class _DpsUpdate:
    def __init__(self) -> None:
        self.dps = {_DpsKey(): {"data": [{"id": "1234", "name": "Saved map"}]}}


class _WireMessage:
    payload = b'{"dps":{"101":{"61":{"data":[]}}}}'


def _bridge(api: _Api):
    bridge = recovery.Q10MapProtocolBridge(api)
    bridge._common = object()
    bridge._multi_map_code = "61"
    bridge._channel = api.channel
    bridge._decode_rpc_response = lambda payload: payload
    return bridge


class Q10MapBridgeTests(unittest.IsolatedAsyncioTestCase):
    def test_decodes_typed_dps_updates(self) -> None:
        bridge = _bridge(_Api())

        decoded = bridge._decode_message(_DpsUpdate())

        self.assertEqual(
            recovery._extract_map_list(decoded, "61")[0].map_id,
            "1234",
        )

    def test_decodes_a_raw_wire_message_before_its_payload(self) -> None:
        bridge = _bridge(_Api())
        message = _WireMessage()
        bridge._decode_rpc_response = lambda value: (
            {_DpsKey(): {"data": [{"id": "1234", "name": "Saved map"}]}}
            if value is message
            else (_ for _ in ()).throw(TypeError("expected wire message"))
        )

        decoded = bridge._decode_message(message)

        self.assertEqual(
            recovery._extract_map_list(decoded, "61")[0].map_id,
            "1234",
        )

    def test_extract_bytes_ignores_empty_object_attributes(self) -> None:
        self.assertIsNone(recovery._extract_bytes(object()))

    async def test_list_retries_the_read_only_request_until_the_reply_arrives(self) -> None:
        api = _Api()
        bridge = _bridge(api)
        calls = 0

        async def delayed_send(_common: object, payload: dict[str, object]) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                api.channel.queue.put_nowait(
                    {"61": {"op": "list", "data": [{"id": "1234", "name": "Saved map"}]}}
                )

        api.command.send = delayed_send
        previous_timeout = recovery._LIST_TIMEOUT
        previous_interval = recovery._LIST_RETRY_INTERVAL
        recovery._LIST_TIMEOUT = 0.1
        recovery._LIST_RETRY_INTERVAL = 0.01
        try:
            maps = await bridge.async_list_maps()
        finally:
            recovery._LIST_TIMEOUT = previous_timeout
            recovery._LIST_RETRY_INTERVAL = previous_interval

        self.assertEqual(calls, 2)
        self.assertEqual(maps[0].map_id, "1234")

    async def test_list_get_apply_and_stream_cleanup(self) -> None:
        api = _Api()
        bridge = _bridge(api)

        maps = await bridge.async_list_maps()
        frame = await bridge.async_get_map("1234")
        await bridge.async_apply_map("1234")

        self.assertEqual(maps[0].map_id, "1234")
        self.assertEqual(frame.map_id, "1234")
        self.assertEqual(
            [payload["61"]["op"] for payload in api.command.calls],
            ["list", "get", "apply"],
        )
        self.assertTrue(all(item.closed for item in api.channel.subscriptions))

    async def test_mismatched_map_packet_times_out_and_unsubscribes(self) -> None:
        api = _Api()
        bridge = _bridge(api)

        async def mismatched_send(_common: object, _payload: dict[str, object]) -> None:
            api.channel.queue.put_nowait(_packet(9999))

        api.command.send = mismatched_send
        previous_timeout = recovery._CAPTURE_TIMEOUT
        recovery._CAPTURE_TIMEOUT = 0.01
        try:
            with self.assertRaises(recovery.MapRecoveryError):
                await bridge.async_get_map("1234")
        finally:
            recovery._CAPTURE_TIMEOUT = previous_timeout
        self.assertTrue(api.channel.subscriptions[-1].closed)


if __name__ == "__main__":
    unittest.main()
