"""Companion notification delivery infrastructure."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NotificationDeliveryResult:
    """Aggregate delivery result without exposing integration exceptions."""

    delivered: int
    targets: int

    @property
    def failed(self) -> int:
        return self.targets - self.delivered


class NotificationService:
    """Discover current Companion targets and deliver notification payloads."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def targets(self) -> tuple[str, ...]:
        try:
            services = self._hass.services.async_services().get("notify", {})
        except AttributeError, TypeError:
            return ()
        return tuple(
            sorted(
                name
                for name in services
                if isinstance(name, str) and name.startswith("mobile_app_")
            )
        )

    async def async_send(self, payload: dict[str, Any]) -> NotificationDeliveryResult:
        targets = self.targets()
        delivered = 0
        for service in targets:
            try:
                await self._hass.services.async_call(
                    "notify",
                    service,
                    payload,
                    blocking=True,
                )
            except Exception:
                _LOGGER.exception(
                    "Adaptive RoboVacs notification delivery failed: target=%s",
                    service,
                )
            else:
                delivered += 1
        return NotificationDeliveryResult(delivered, len(targets))

    async def async_clear(self, tag: str) -> NotificationDeliveryResult:
        return await self.async_send(
            {"message": "clear_notification", "data": {"tag": tag}}
        )
