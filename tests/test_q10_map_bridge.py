"""Behavioral tests for the Q10 bridge over an existing HA runtime."""

from __future__ import annotations

import asyncio
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from custom_components.adaptive_robovacs import map_recovery_roborock as recovery


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
        self.list_requests = 0

    async def send(self, _common: object, payload: dict[str, object]) -> None:
        self.calls.append(payload)
        request = payload.get("61", payload)
        assert isinstance(request, dict)
        if request["op"] == "list":
            self.list_requests += 1
            if self.list_requests == 1:
                self.channel.queue.put_nowait(
                    {
                        "61": {
                            "op": "list",
                            "data": [{"id": "1234", "name": "Saved map"}],
                        }
                    }
                )
            else:
                self.channel.queue.put_nowait(_packet(1234))
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
    bridge._multi_map = object()
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

    async def test_list_retries_the_read_only_request_until_the_reply_arrives(
        self,
    ) -> None:
        api = _Api()
        bridge = _bridge(api)
        calls = 0

        async def delayed_send(_common: object, payload: dict[str, object]) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                api.channel.queue.put_nowait(
                    {
                        "61": {
                            "op": "list",
                            "data": [{"id": "1234", "name": "Saved map"}],
                        }
                    }
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
            [(payload.get("61", payload))["op"] for payload in api.command.calls],
            ["list", "list", "apply"],
        )
        self.assertTrue(all(item.closed for item in api.channel.subscriptions))

    async def test_mismatched_map_packet_times_out_and_unsubscribes(self) -> None:
        api = _Api()
        bridge = _bridge(api)

        async def mismatched_send(_common: object, _payload: dict[str, object]) -> None:
            api.channel.queue.put_nowait(_packet(9999))

        api.command.send = mismatched_send
        previous_timeout = recovery._FRAME_TIMEOUT
        previous_interval = recovery._FRAME_RETRY_INTERVAL
        recovery._FRAME_TIMEOUT = 0.03
        recovery._FRAME_RETRY_INTERVAL = 0.001
        try:
            with self.assertRaises(recovery.MapRecoveryError):
                await bridge.async_get_map("1234")
        finally:
            recovery._FRAME_TIMEOUT = previous_timeout
            recovery._FRAME_RETRY_INTERVAL = previous_interval
        self.assertTrue(api.channel.subscriptions[-1].closed)

    def test_extract_helpers_accept_supported_shapes_and_reject_noise(self) -> None:
        self.assertIsNone(recovery._extract_map_list(None, "61"))
        self.assertIsNone(recovery._extract_map_list({"op": "apply"}, "61"))
        self.assertIsNone(recovery._extract_map_list({"data": "bad"}, "61"))
        values = recovery._extract_map_list(
            {
                61: {
                    "data": [
                        None,
                        {},
                        {"id": 1},
                        {"id": "2", "name": "Upstairs", "timestamp": 123},
                    ]
                }
            },
            "61",
        )
        self.assertEqual([item.name for item in values or []], ["Map 1", "Upstairs"])
        self.assertEqual((values or [])[1].timestamp, "123")

        marker = bytearray(b"abc")
        self.assertEqual(recovery._extract_bytes(marker), b"abc")
        self.assertEqual(recovery._extract_bytes(memoryview(b"def")), b"def")
        self.assertEqual(recovery._extract_bytes((None, {"raw": b"ghi"})), b"ghi")
        self.assertEqual(recovery._extract_bytes(SimpleNamespace(data=b"jkl")), b"jkl")
        self.assertIsNone(recovery._normalise_dps(object()))
        self.assertEqual(
            recovery._normalise_dps(_DpsUpdate())["61"]["data"][0]["id"], "1234"
        )

    def test_from_api_requires_command_protocol_and_stream_capabilities(self) -> None:
        mapping_module = ModuleType("roborock.data.b01_q10.b01_q10_code_mappings")
        protocol_module = ModuleType("roborock.protocols.b01_q10_protocol")
        protocol_module.decode_rpc_response = lambda value: value
        modules = {
            mapping_module.__name__: mapping_module,
            protocol_module.__name__: protocol_module,
        }
        with patch.dict(sys.modules, modules):
            mapping_module.B01_Q10_DP = SimpleNamespace(
                COMMON=object(), MULTI_MAP=SimpleNamespace(code=61)
            )
            with self.assertRaisesRegex(recovery.MapRecoveryUnavailable, "command"):
                recovery.Q10MapProtocolBridge.from_api(SimpleNamespace())

            api = SimpleNamespace(command=SimpleNamespace(send=AsyncMock()))
            mapping_module.B01_Q10_DP = SimpleNamespace(
                COMMON=None, MULTI_MAP=SimpleNamespace(code=61)
            )
            with self.assertRaisesRegex(recovery.MapRecoveryUnavailable, "protocol"):
                recovery.Q10MapProtocolBridge.from_api(api)

            mapping_module.B01_Q10_DP = SimpleNamespace(
                COMMON=object(), MULTI_MAP=SimpleNamespace(code=61)
            )
            with self.assertRaisesRegex(recovery.MapRecoveryUnavailable, "stream"):
                recovery.Q10MapProtocolBridge.from_api(api)

            channel = _Channel()
            api._api = SimpleNamespace(_channel=SimpleNamespace(_mqtt_channel=channel))
            bridge = recovery.Q10MapProtocolBridge.from_api(api)
            self.assertIs(bridge._channel, channel)
            self.assertEqual(bridge._multi_map_code, "61")

    async def test_stream_context_manager_and_unsupported_shape(self) -> None:
        class ContextSubscription:
            def __init__(self) -> None:
                self.items = iter(("one", "two"))
                self.exited = False

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                self.exited = True

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self.items)
                except StopIteration as err:
                    raise StopAsyncIteration from err

        subscription = ContextSubscription()
        bridge = _bridge(_Api())
        bridge._channel = SimpleNamespace(subscribe_stream=lambda: subscription)
        self.assertEqual(
            [item async for item in bridge._async_stream()], ["one", "two"]
        )
        self.assertTrue(subscription.exited)

        bridge._channel = SimpleNamespace(subscribe_stream=lambda: object())
        with self.assertRaisesRegex(recovery.MapRecoveryUnavailable, "shape"):
            await anext(bridge._async_stream())

    def test_decode_message_falls_back_without_leaking_protocol_errors(self) -> None:
        bridge = _bridge(_Api())
        self.assertEqual(bridge._decode_message({"data": []}), {"data": []})
        packet = _packet(1234)
        self.assertIs(bridge._decode_message(packet), packet)

        wire = SimpleNamespace(payload=b"not-a-frame")
        bridge._decode_rpc_response = Mock(side_effect=(ValueError(), {61: "ok"}))
        self.assertEqual(bridge._decode_message(wire), {"61": "ok"})
        bridge._decode_rpc_response = Mock(side_effect=ValueError())
        self.assertIs(bridge._decode_message(wire), wire)

    async def test_send_failures_and_active_refresh_are_normalized(self) -> None:
        bridge = _bridge(_Api())
        bridge._api.command.send = AsyncMock(side_effect=RuntimeError("private"))
        with self.assertRaisesRegex(recovery.MapRecoveryError, "rejected"):
            await bridge._async_send_common({"op": "list"})
        with self.assertRaisesRegex(recovery.MapRecoveryError, "active map"):
            await bridge._async_send_active_frame_request({"op": "list"})

        bridge = _bridge(_Api())
        bridge._api.refresh = AsyncMock()
        bridge._api.command.send = AsyncMock()
        await bridge._async_send_active_frame_request({"op": "list"})
        bridge._api.refresh.assert_awaited_once()

    async def test_list_bounds_and_invalid_frame_results_are_rejected(self) -> None:
        bridge = _bridge(_Api())
        bridge._async_request_and_wait = AsyncMock(return_value=[])
        with self.assertRaisesRegex(recovery.MapRecoveryError, "did not report"):
            await bridge.async_list_maps()

        bridge._async_request_and_wait.return_value = [
            recovery.RetainedMap(str(index), f"Map {index}") for index in range(9)
        ]
        with self.assertRaisesRegex(recovery.MapRecoveryError, "too many"):
            await bridge.async_list_maps()

        bridge._async_request_and_wait.return_value = object()
        with self.assertRaisesRegex(recovery.MapRecoveryError, "invalid map frame"):
            await bridge.async_get_map("1")

    async def test_request_wait_cancels_waiter_when_sender_fails(self) -> None:
        bridge = _bridge(_Api())
        closed = asyncio.Event()

        async def wait_forever(_predicate, *, timeout_seconds):
            del timeout_seconds
            try:
                await asyncio.Future()
            finally:
                closed.set()

        bridge._async_wait_for = wait_forever

        async def fail(_request):
            raise RuntimeError("send failed")

        with self.assertRaisesRegex(RuntimeError, "send failed"):
            await bridge._async_request_and_wait(
                {"op": "list"},
                lambda value: value,
                timeout_seconds=1,
                sender=fail,
            )
        self.assertTrue(closed.is_set())


class Q10RuntimeResolverTests(unittest.TestCase):
    def test_resolver_requires_one_registry_identity_and_one_live_runtime(self) -> None:
        registry = SimpleNamespace(async_get=Mock(return_value=None))
        entries = []
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_entries=lambda _domain: entries)
        )
        resolver = recovery.Q10RuntimeResolver(hass)

        with self.assertRaisesRegex(recovery.MapRecoveryUnavailable, "not a Q10"):
            resolver.async_resolve(
                SimpleNamespace(platform="generic", device_id="device")
            )

        with patch.object(recovery.dr, "async_get", return_value=registry):
            with self.assertRaisesRegex(
                recovery.MapRecoveryUnavailable, "registry entry"
            ):
                resolver.async_resolve(
                    SimpleNamespace(platform="roborock", device_id="device")
                )

            registry.async_get.return_value = SimpleNamespace(identifiers=set())
            with self.assertRaisesRegex(recovery.MapRecoveryUnavailable, "matched"):
                resolver.async_resolve(
                    SimpleNamespace(platform="roborock", device_id="device")
                )

            registry.async_get.return_value = SimpleNamespace(
                identifiers={("roborock", "duid-1")}
            )
            entries.append(
                SimpleNamespace(
                    runtime_data=SimpleNamespace(
                        b01_q10={"one": SimpleNamespace(duid="duid-1", api="api")}
                    )
                )
            )
            expected = object()
            with patch.object(
                recovery.Q10MapProtocolBridge, "from_api", return_value=expected
            ) as from_api:
                self.assertIs(
                    resolver.async_resolve(
                        SimpleNamespace(platform="roborock", device_id="device")
                    ),
                    expected,
                )
            from_api.assert_called_once_with("api")

            entries.append(
                SimpleNamespace(
                    runtime_data=SimpleNamespace(
                        b01_q10=[SimpleNamespace(duid="duid-1", api="other")]
                    )
                )
            )
            with self.assertRaisesRegex(recovery.MapRecoveryUnavailable, "ambiguous"):
                resolver.async_resolve(
                    SimpleNamespace(platform="roborock", device_id="device")
                )


if __name__ == "__main__":
    unittest.main()
