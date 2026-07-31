"""Config flow para e-distribución (habla con el add-on `edistribucion`, no con la web directamente)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .api import EdistribucionApiClient, EdistribucionApiError
from .const import (
    CONF_HOLIDAY_REGION,
    CONF_PRICE_POWER_PUNTA,
    CONF_PRICE_POWER_VALLE,
    CONF_PVPC_ZONE,
    CONF_SUPPLY_POINTS,
    DEFAULT_HOLIDAY_REGION,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    HOLIDAY_REGIONS,
    TARIFF_TRAMOS,
    TARIFF_TYPES,
)
from .esios import DEFAULT_PVPC_ZONE, PVPC_ZONES

# Traducción de "activo"/"histórico" para el placeholder {estado} del paso por CUPS — no puede ir
# en strings.json porque description_placeholders son sustituciones de DATOS, no de texto (HA no
# las vuelve a traducir), así que sin esto el estado saldría siempre en español pese al idioma
# elegido en Home Assistant. Mismos idiomas que las traducciones de la integración.
_ESTADO_TRANSLATIONS = {
    "es": ("activo", "histórico"),
    "en": ("active", "historical"),
    "ca": ("actiu", "històric"),
    "gl": ("activo", "histórico"),
    "eu": ("aktibo", "historikoa"),
    "it": ("attivo", "storico"),
    "de": ("aktiv", "historisch"),
    "fr": ("actif", "historique"),
    "ru": ("активен", "исторический"),
}


def _estado_word(hass, active: bool) -> str:
    lang = (hass.config.language or "es").split("-")[0]
    active_word, historic_word = _ESTADO_TRANSLATIONS.get(lang, _ESTADO_TRANSLATIONS["es"])
    return active_word if active else historic_word


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


_SUPPLY_POINT_FIELD_DEFAULTS = {
    "track": True,
    "alias": "",
    "tariff_type": TARIFF_TRAMOS,
    CONF_PRICE_POWER_PUNTA: 0,
    CONF_PRICE_POWER_VALLE: 0,
    "fixed_price": 0,
    "price_punta": 0,
    "price_llano": 0,
    "price_valle": 0,
    CONF_HOLIDAY_REGION: DEFAULT_HOLIDAY_REGION,
    "surplus_compensation": False,
    "surplus_price": 0,
    CONF_PVPC_ZONE: DEFAULT_PVPC_ZONE,
}


class EdistribucionOptionsFlow(config_entries.OptionsFlow):
    """Paso 1: solo el intervalo de actualización (global). Un paso más por cada suministro, uno
    detrás de otro: si seguirlo, alias, tipo de tarifa (fija/tramos/pvpc) con sus precios, precio del
    término de potencia, zona PVPC y compensación de excedentes — TODO por CUPS (distintos contratos
    pueden tener tarifas distintas), cada uno en su propia pantalla en vez de un formulario gigante
    con todos mezclados. La potencia contratada (kW) NO se pide — se lee en vivo de e-distribución
    (ver coordinator.py)."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry
        self._supply_points: list[dict] | None = None
        self._collected: dict[str, Any] = {}
        self._collected_supply_points: dict[str, dict] = {}
        self._current_index = 0

    async def _async_ensure_supply_points(self) -> None:
        if self._supply_points is not None:
            return
        session = async_get_clientsession(self.hass)
        client = EdistribucionApiClient(session, self._config_entry.data[CONF_HOST], self._config_entry.data[CONF_PORT])
        try:
            self._supply_points = await client.async_get_supply_points()
        except EdistribucionApiError:
            self._supply_points = []

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        await self._async_ensure_supply_points()

        if user_input is not None:
            self._collected = dict(user_input)
            return await self.async_step_supply_point()

        current_interval = self._config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES)
        schema = vol.Schema(
            {vol.Required(CONF_SCAN_INTERVAL, default=current_interval): vol.All(int, vol.Range(min=5, max=1440))}
        )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={
                "num_supplies": str(len(self._supply_points)),
            },
        )

    async def async_step_supply_point(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        current_supply_opts: dict[str, dict] = self._config_entry.options.get(CONF_SUPPLY_POINTS, {})

        # Guardar lo que se acaba de enviar (si venimos de un submit) antes de mirar si hay más CUPS.
        if user_input is not None and self._supply_points:
            cont_id = self._supply_points[self._current_index]["contId"]
            self._collected_supply_points[cont_id] = {
                "track": user_input.get("track", True),
                "alias": user_input.get("alias", "").strip(),
                "tariff_type": user_input.get("tariff_type", TARIFF_TRAMOS),
                CONF_PRICE_POWER_PUNTA: user_input.get(CONF_PRICE_POWER_PUNTA, 0),
                CONF_PRICE_POWER_VALLE: user_input.get(CONF_PRICE_POWER_VALLE, 0),
                "fixed_price": user_input.get("fixed_price", 0),
                "price_punta": user_input.get("price_punta", 0),
                "price_llano": user_input.get("price_llano", 0),
                "price_valle": user_input.get("price_valle", 0),
                CONF_HOLIDAY_REGION: user_input.get(CONF_HOLIDAY_REGION, DEFAULT_HOLIDAY_REGION),
                "surplus_compensation": user_input.get("surplus_compensation", False),
                "surplus_price": user_input.get("surplus_price", 0),
                CONF_PVPC_ZONE: user_input.get(CONF_PVPC_ZONE, DEFAULT_PVPC_ZONE),
            }
            self._current_index += 1

        if not self._supply_points or self._current_index >= len(self._supply_points):
            self._collected[CONF_SUPPLY_POINTS] = self._collected_supply_points
            return self.async_create_entry(data=self._collected)

        sp = self._supply_points[self._current_index]
        cont_id = sp["contId"]
        cups = sp["cups"]
        prev = current_supply_opts.get(cont_id, _SUPPLY_POINT_FIELD_DEFAULTS)

        schema = vol.Schema(
            {
                vol.Required("track", default=prev.get("track", True)): bool,
                vol.Optional("alias", default=prev.get("alias", "")): str,
                vol.Optional("tariff_type", default=prev.get("tariff_type", TARIFF_TRAMOS)): vol.In(TARIFF_TYPES),
                vol.Optional(CONF_PRICE_POWER_PUNTA, default=prev.get(CONF_PRICE_POWER_PUNTA, 0)): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=5)
                ),
                vol.Optional(CONF_PRICE_POWER_VALLE, default=prev.get(CONF_PRICE_POWER_VALLE, 0)): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=5)
                ),
                vol.Optional("fixed_price", default=prev.get("fixed_price", 0)): vol.All(vol.Coerce(float), vol.Range(min=0, max=10)),
                vol.Optional("price_punta", default=prev.get("price_punta", 0)): vol.All(vol.Coerce(float), vol.Range(min=0, max=10)),
                vol.Optional("price_llano", default=prev.get("price_llano", 0)): vol.All(vol.Coerce(float), vol.Range(min=0, max=10)),
                vol.Optional("price_valle", default=prev.get("price_valle", 0)): vol.All(vol.Coerce(float), vol.Range(min=0, max=10)),
                vol.Optional(CONF_HOLIDAY_REGION, default=prev.get(CONF_HOLIDAY_REGION, DEFAULT_HOLIDAY_REGION)): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[selector.SelectOptionDict(value=k, label=v) for k, v in HOLIDAY_REGIONS.items()],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional("surplus_compensation", default=prev.get("surplus_compensation", False)): bool,
                vol.Optional("surplus_price", default=prev.get("surplus_price", 0)): vol.All(vol.Coerce(float), vol.Range(min=0, max=10)),
                vol.Optional(CONF_PVPC_ZONE, default=prev.get(CONF_PVPC_ZONE, DEFAULT_PVPC_ZONE)): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[selector.SelectOptionDict(value=k, label=v) for k, v in PVPC_ZONES.items()],
                        mode=selector.SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        estado = _estado_word(self.hass, bool(sp.get("active")))
        return self.async_show_form(
            step_id="supply_point",
            data_schema=schema,
            description_placeholders={
                "position": str(self._current_index + 1),
                "total": str(len(self._supply_points)),
                "cups": cups,
                "estado": estado,
                "address": sp.get("address", ""),
            },
        )
