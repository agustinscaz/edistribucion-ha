"""Config flow para e-distribución (habla con el add-on `edistribucion`, no con la web directamente)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .api import EdistribucionApiClient, EdistribucionApiError
from .const import (
    CONF_CONTRACTED_POWER_P1,
    CONF_CONTRACTED_POWER_P2,
    CONF_SUPPLY_POINTS,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    POWER_TERM_KEYS,
    TARIFF_TRAMOS,
    TARIFF_TYPES,
)

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

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> FlowResult:
        """El add-on se anuncia por mDNS (_edistribucion._tcp.local) — si aparece uno en la red,
        no hace falta teclear host/puerto a mano, solo confirmar."""
        host = discovery_info.host
        port = discovery_info.port
        await self.async_set_unique_id(f"{host}:{port}")
        self._abort_if_unique_id_configured()
        self._discovered_data = {CONF_HOST: host, CONF_PORT: port}
        self.context["title_placeholders"] = {"host": host}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            client = EdistribucionApiClient(session, self._discovered_data[CONF_HOST], self._discovered_data[CONF_PORT])
            try:
                info = await client.async_get_info()
            except EdistribucionApiError:
                return self.async_abort(reason="cannot_connect")
            title = info.get("name") or "e-distribución"
            return self.async_create_entry(title=title, data=self._discovered_data)

        return self.async_show_form(
            step_id="zeroconf_confirm",
            description_placeholders={"host": self._discovered_data[CONF_HOST], "port": str(self._discovered_data[CONF_PORT])},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> EdistribucionOptionsFlow:
        return EdistribucionOptionsFlow(config_entry)


class EdistribucionOptionsFlow(config_entries.OptionsFlow):
    """Intervalo de actualización, término de potencia (global), y por cada suministro: si
    seguirlo, alias, tipo de tarifa de energía (fija/tramos/pvpc) con sus precios, y compensación
    de excedentes."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry
        self._supply_points: list[dict] | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if self._supply_points is None:
            session = async_get_clientsession(self.hass)
            client = EdistribucionApiClient(session, self._config_entry.data[CONF_HOST], self._config_entry.data[CONF_PORT])
            try:
                self._supply_points = await client.async_get_supply_points()
            except EdistribucionApiError:
                self._supply_points = []

        current_supply_opts: dict[str, dict] = self._config_entry.options.get(CONF_SUPPLY_POINTS, {})

        if user_input is not None:
            supply_points_opt: dict[str, dict] = {}
            for sp in self._supply_points:
                cont_id = sp["contId"]
                cups = sp["cups"]
                supply_points_opt[cont_id] = {
                    "track": user_input.pop(f"track_{cups}", True),
                    "alias": user_input.pop(f"alias_{cups}", "").strip(),
                    "tariff_type": user_input.pop(f"tariff_type_{cups}", TARIFF_TRAMOS),
                    "fixed_price": user_input.pop(f"fixed_price_{cups}", 0),
                    "price_punta": user_input.pop(f"price_punta_{cups}", 0),
                    "price_llano": user_input.pop(f"price_llano_{cups}", 0),
                    "price_valle": user_input.pop(f"price_valle_{cups}", 0),
                    "pvpc_entity": user_input.pop(f"pvpc_entity_{cups}", None),
                    "surplus_compensation": user_input.pop(f"surplus_compensation_{cups}", False),
                    "surplus_price": user_input.pop(f"surplus_price_{cups}", 0),
                }
            user_input[CONF_SUPPLY_POINTS] = supply_points_opt
            return self.async_create_entry(data=user_input)

        current_interval = self._config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES)
        schema_dict: dict[Any, Any] = {
            vol.Required(CONF_SCAN_INTERVAL, default=current_interval): vol.All(int, vol.Range(min=5, max=1440)),
        }
        for power_key in (CONF_CONTRACTED_POWER_P1, CONF_CONTRACTED_POWER_P2):
            current_power = self._config_entry.options.get(power_key, 0)
            schema_dict[vol.Optional(power_key, default=current_power)] = vol.All(vol.Coerce(float), vol.Range(min=0, max=100))
        for price_power_key in POWER_TERM_KEYS[2:]:
            current_price_power = self._config_entry.options.get(price_power_key, 0)
            schema_dict[vol.Optional(price_power_key, default=current_price_power)] = vol.All(vol.Coerce(float), vol.Range(min=0, max=5))

        listing_lines = []
        for sp in self._supply_points:
            cont_id = sp["contId"]
            cups = sp["cups"]
            prev = current_supply_opts.get(cont_id, {})

            schema_dict[vol.Required(f"track_{cups}", default=prev.get("track", True))] = bool
            schema_dict[vol.Optional(f"alias_{cups}", default=prev.get("alias", ""))] = str
            schema_dict[vol.Optional(f"tariff_type_{cups}", default=prev.get("tariff_type", TARIFF_TRAMOS))] = vol.In(TARIFF_TYPES)
            schema_dict[vol.Optional(f"fixed_price_{cups}", default=prev.get("fixed_price", 0))] = vol.All(
                vol.Coerce(float), vol.Range(min=0, max=10)
            )
            schema_dict[vol.Optional(f"price_punta_{cups}", default=prev.get("price_punta", 0))] = vol.All(
                vol.Coerce(float), vol.Range(min=0, max=10)
            )
            schema_dict[vol.Optional(f"price_llano_{cups}", default=prev.get("price_llano", 0))] = vol.All(
                vol.Coerce(float), vol.Range(min=0, max=10)
            )
            schema_dict[vol.Optional(f"price_valle_{cups}", default=prev.get("price_valle", 0))] = vol.All(
                vol.Coerce(float), vol.Range(min=0, max=10)
            )
            schema_dict[vol.Optional(f"pvpc_entity_{cups}", default=prev.get("pvpc_entity") or "")] = str
            schema_dict[vol.Optional(f"surplus_compensation_{cups}", default=prev.get("surplus_compensation", False))] = bool
            schema_dict[vol.Optional(f"surplus_price_{cups}", default=prev.get("surplus_price", 0))] = vol.All(
                vol.Coerce(float), vol.Range(min=0, max=10)
            )

            estado = "activo" if sp.get("active") else "histórico"
            listing_lines.append(f"- {cups} ({estado}): {sp.get('address', '')}")

        placeholders = {
            "supply_points_list": "\n".join(listing_lines)
            or "(no se pudo obtener la lista de suministros — ¿está el add-on arrancado?)"
        }
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_dict),
            description_placeholders=placeholders,
        )
