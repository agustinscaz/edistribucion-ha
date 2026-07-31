"""DataUpdateCoordinator: pide datos al add-on cada X minutos, uno por suministro."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import EdistribucionApiClient, EdistribucionApiError, InvalidCredentialsError
from .const import (
    CONF_CONTRACTED_POWER_PUNTA,
    CONF_CONTRACTED_POWER_VALLE,
    CONF_SUPPLY_POINTS,
    CONSECUTIVE_FAILURES_FOR_REPAIR,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
)
from .esios import DEFAULT_PVPC_ZONE, EsiosError, async_get_pvpc_prices_for_day

_LOGGER = logging.getLogger(__name__)

RANGE_MONTH = "3"
RANGE_WEEK = "2"

ISSUE_CONNECTION = "addon_connection_failed"
ISSUE_INVALID_CREDENTIALS = "invalid_credentials"


class EdistribucionCoordinator(DataUpdateCoordinator):
    """Mantiene: lista de suministros (filtrados/con alias según opciones) + consumo (hoy/semana/mes),
    comparativa con el mismo mes del año anterior, y potencia de cada uno."""

    def __init__(self, hass: HomeAssistant, client: EdistribucionApiClient, entry: ConfigEntry) -> None:
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES)
        super().__init__(hass, _LOGGER, name="edistribucion", update_interval=timedelta(minutes=interval))
        self.client = client
        self.entry_id = entry.entry_id
        self.supply_point_options: dict[str, dict] = entry.options.get(CONF_SUPPLY_POINTS, {})
        # Término de potencia (kW contratados + €/kW/día) y zona PVPC son por CUPS — ver
        # supply_point_options. Los precios PVPC se cachean por zona, porque distintos CUPS podrían
        # usar zonas distintas (poco habitual, pero posible).
        self.pvpc_prices: dict[str, dict[str, float]] = {}
        self._pvpc_fetched_date: str | None = None
        self.last_success_time: datetime | None = None
        self._consecutive_failures = 0

    def _pvpc_zones_needed(self) -> set[str]:
        """Zonas de las que hace falta precio PVPC — no solo si la tarifa activa es "pvpc": el
        simulador de tarifas (ver costs.estimate_cost_as_tariff) también necesita el precio real
        aunque el CUPS esté en fija/tramos, para poder comparar "qué habría costado con pvpc"."""
        return {opts.get("pvpc_zone") or DEFAULT_PVPC_ZONE for opts in self.supply_point_options.values() if opts.get("track", True) is not False}

    async def _async_update_pvpc_prices(self) -> None:
        """Los precios PVPC solo cambian una vez al día (se publican ~20:15 para el día
        siguiente) — se pide como mucho una vez por día, no en cada ciclo de actualización, para no
        pedir de más a la API pública de ESIOS sin necesidad. El archivo público solo da UN día por
        petición, así que se piden uno a uno los días del mes que aún no tengamos en caché, para
        cada zona que use algún CUPS con tarifa pvpc."""
        zones = self._pvpc_zones_needed()
        if not zones:
            return
        today = dt_util.now().date()
        today_key = today.strftime("%Y-%m-%d")
        if self._pvpc_fetched_date == today_key:
            return

        session = async_get_clientsession(self.hass)
        tomorrow = today + timedelta(days=1)
        for zone in zones:
            zone_prices = self.pvpc_prices.setdefault(zone, {})
            day = today.replace(day=1)
            while day <= tomorrow:
                day_key_prefix = f"{day.strftime('%d/%m/%Y')} "
                if not any(k.startswith(day_key_prefix) for k in zone_prices):
                    try:
                        zone_prices.update(await async_get_pvpc_prices_for_day(session, zone, day))
                    except EsiosError as err:
                        _LOGGER.warning("No se pudieron obtener precios PVPC de ESIOS (zona %s) para %s: %s", zone, day, err)
                        break  # si ESIOS falla (red, baneo...), se reintenta en el próximo ciclo
                day += timedelta(days=1)
        self._pvpc_fetched_date = today_key

    async def async_force_refresh_pvpc_prices(self) -> None:
        """Fuerza un refresco de precios PVPC ya (botón de la integración), sin esperar al ciclo
        diario — útil si ESIOS falló antes, o si acaban de publicar los precios de mañana."""
        self._pvpc_fetched_date = None
        await self._async_update_pvpc_prices()
        await self.async_request_refresh()

    async def _async_update_data(self) -> dict:
        try:
            await self._async_update_pvpc_prices()
            supply_points = await self.client.async_get_supply_points()
            data: dict[str, dict] = {}
            for sp in supply_points:
                cont_id = sp["contId"]
                opts = self.supply_point_options.get(cont_id, {})
                if opts.get("track", True) is False:
                    continue  # el usuario decidió no seguir este suministro (opciones de la integración)
                # Se mezclan alias + tarifa/precios/excedentes configurados para ESTE CUPS en el
                # propio dict del suministro, para que sensor.py los tenga a mano sin plumbing extra.
                sp = {**sp, **{k: v for k, v in opts.items() if k != "track"}}

                cups_id = sp["cupsId"]
                bundle: dict = {
                    "supply_point": sp,
                    "consumption": None,
                    "week": None,
                    "month": None,
                    "month_last_year": None,
                    "max_power_demand": None,
                    "contract": None,
                }

                try:
                    # Potencia contratada real (punta/valle) + metadatos del contrato — sacada de la
                    # propia distribuidora, no de un valor que teclee el usuario (ver v1.11.0).
                    bundle["contract"] = await self.client.async_get_contracted_power(cont_id)
                    sp[CONF_CONTRACTED_POWER_PUNTA] = bundle["contract"].get("contractedPowerPuntaKw") or 0
                    sp[CONF_CONTRACTED_POWER_VALLE] = bundle["contract"].get("contractedPowerValleKw") or 0
                except EdistribucionApiError as err:
                    _LOGGER.warning("No se pudo leer la potencia contratada real de %s: %s", sp.get("cups"), err)

                try:
                    bundle["consumption"] = await self.client.async_get_consumption(cont_id)
                except EdistribucionApiError as err:
                    _LOGGER.warning("No se pudo leer consumo de hoy de %s: %s", sp.get("cups"), err)
                try:
                    bundle["week"] = await self.client.async_get_consumption(cont_id, RANGE_WEEK)
                except EdistribucionApiError as err:
                    _LOGGER.warning("No se pudo leer consumo semanal de %s: %s", sp.get("cups"), err)
                try:
                    bundle["month"] = await self.client.async_get_consumption(cont_id, RANGE_MONTH)
                except EdistribucionApiError as err:
                    _LOGGER.warning("No se pudo leer consumo mensual de %s: %s", sp.get("cups"), err)
                try:
                    a_year_ago = (dt_util.now() - timedelta(days=365)).strftime("%Y-%m-%d")
                    bundle["month_last_year"] = await self.client.async_get_consumption(cont_id, RANGE_MONTH, a_year_ago)
                except EdistribucionApiError as err:
                    # Normal si el contrato es más nuevo que un año — no hay nada que comparar todavía.
                    _LOGGER.debug("Sin histórico de hace un año para %s: %s", sp.get("cups"), err)
                try:
                    bundle["max_power_demand"] = await self.client.async_get_max_power_demand(cups_id)
                except EdistribucionApiError as err:
                    _LOGGER.debug("Sin potencia máxima para %s (normal si no tiene telegestión): %s", sp.get("cups"), err)

                data[cont_id] = bundle

            self.last_success_time = dt_util.utcnow()
            self._consecutive_failures = 0
            ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_CONNECTION}_{self.entry_id}")
            ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_INVALID_CREDENTIALS}_{self.entry_id}")
            return data
        except InvalidCredentialsError as err:
            # Caso inequívoco: no tiene sentido esperar a varios fallos seguidos como con un fallo de
            # red genérico — se avisa ya de que hace falta corregir dni/password en el add-on.
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"{ISSUE_INVALID_CREDENTIALS}_{self.entry_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="invalid_credentials",
            )
            raise UpdateFailed(f"Credenciales incorrectas en el add-on: {err}") from err
        except EdistribucionApiError as err:
            self._consecutive_failures += 1
            if self._consecutive_failures == CONSECUTIVE_FAILURES_FOR_REPAIR:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    f"{ISSUE_CONNECTION}_{self.entry_id}",
                    is_fixable=False,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key="addon_connection_failed",
                )
            raise UpdateFailed(f"Error hablando con el add-on de e-distribución: {err}") from err
