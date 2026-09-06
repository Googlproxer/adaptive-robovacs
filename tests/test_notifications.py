"""Behavioral tests for Companion notification infrastructure."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from custom_components.adaptive_robovacs.notifications import NotificationService


class NotificationServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_targets_are_filtered_sorted_and_defensive(self) -> None:
        services = Mock()
        services.async_services.return_value = {
            "notify": {
                "mobile_app_zed": object(),
                "persistent_notification": object(),
                "mobile_app_alpha": object(),
                7: object(),
            }
        }
        service = NotificationService(SimpleNamespace(services=services))

        self.assertEqual(
            service.targets(),
            ("mobile_app_alpha", "mobile_app_zed"),
        )

        services.async_services.side_effect = AttributeError
        self.assertEqual(service.targets(), ())
        services.async_services.side_effect = TypeError
        self.assertEqual(service.targets(), ())

    async def test_send_aggregates_successes_without_exposing_failures(self) -> None:
        calls = AsyncMock()

        async def send(_domain, target, _payload, *, blocking):
            self.assertTrue(blocking)
            if target == "mobile_app_broken":
                raise RuntimeError("private endpoint detail")

        calls.side_effect = send
        services = SimpleNamespace(
            async_services=lambda: {
                "notify": {
                    "mobile_app_phone": object(),
                    "mobile_app_broken": object(),
                }
            },
            async_call=calls,
        )
        service = NotificationService(SimpleNamespace(services=services))

        result = await service.async_send({"message": "Water ready?"})

        self.assertEqual((result.delivered, result.targets, result.failed), (1, 2, 1))
        self.assertEqual(calls.await_count, 2)

    async def test_clear_uses_the_companion_clear_contract(self) -> None:
        calls = AsyncMock()
        services = SimpleNamespace(
            async_services=lambda: {"notify": {"mobile_app_phone": object()}},
            async_call=calls,
        )
        service = NotificationService(SimpleNamespace(services=services))

        result = await service.async_clear("adaptive-water-1")

        self.assertEqual(result.delivered, 1)
        calls.assert_awaited_once_with(
            "notify",
            "mobile_app_phone",
            {
                "message": "clear_notification",
                "data": {"tag": "adaptive-water-1"},
            },
            blocking=True,
        )


if __name__ == "__main__":
    unittest.main()
