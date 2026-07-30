"""Config flow para e-distribución (habla con el add-on `edistribucion`, no con la web directamente)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EdistribucionApiClient, EdistribucionApiError
from .const import DEFAULT_HOST, DEFAULT_PORT, DOMAIN

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
    }
)


class EdistribucionConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Flujo de configuración: host/puerto del add-on."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            client = EdistribucionApiClient(session, user_input[CONF_HOST], user_input[CONF_PORT])
            try:
                info = await client.async_get_info()
            except EdistribucionApiError:
                errors["base"] = "cannot_connect"
            else:
                unique_id = f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}"
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                title = info.get("name") or "e-distribución"
                return self.async_create_entry(title=title, data=user_input)

        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)
