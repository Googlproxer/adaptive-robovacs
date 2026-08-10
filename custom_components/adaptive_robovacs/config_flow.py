"""Config flow for Adaptive RoboVacs."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_FORECAST_CONFIDENCE,
    CONF_HALL_END,
    CONF_HALL_START,
    CONF_OBSERVE_ONLY,
    CONF_UNRESOLVED_END,
    CONF_UNRESOLVED_START,
    DEFAULT_FORECAST_CONFIDENCE,
    DEFAULT_HALL_END,
    DEFAULT_HALL_START,
    DEFAULT_UNRESOLVED_END,
    DEFAULT_UNRESOLVED_START,
    DOMAIN,
    NAME,
)

DAILY_TIME = vol.Match(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class AdaptiveRoboVacsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Create a single registry-driven scheduler entry."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle initial setup."""

        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(title=NAME, data=user_input)

        schema = vol.Schema(
            {
                vol.Required(CONF_OBSERVE_ONLY, default=True): bool,
                vol.Required(
                    CONF_FORECAST_CONFIDENCE, default=DEFAULT_FORECAST_CONFIDENCE
                ): vol.All(vol.Coerce(int), vol.Range(min=50, max=95)),
                vol.Required(CONF_HALL_START, default=DEFAULT_HALL_START): DAILY_TIME,
                vol.Required(CONF_HALL_END, default=DEFAULT_HALL_END): DAILY_TIME,
                vol.Required(
                    CONF_UNRESOLVED_START, default=DEFAULT_UNRESOLVED_START
                ): DAILY_TIME,
                vol.Required(
                    CONF_UNRESOLVED_END, default=DEFAULT_UNRESOLVED_END
                ): DAILY_TIME,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)
