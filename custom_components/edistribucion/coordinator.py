"""DataUpdateCoordinator: pide datos al add-on cada X minutos, uno por suministro."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import EdistribucionApiClient, EdistribucionApiError, InvalidCredentialsError
from .const import (
    CONF_SUPPLY_POINTS,
    CONSECUTIVE_FAILURES_FOR_REPAIR,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    POWER_TERM_KEYS,
)

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
        # Término de potencia: kW contratados + €/kW/día para cada uno de los dos periodos (P1/P2).
        self.contracted_power_p1: float = entry.options.get(POWER_TERM_KEYS[0], 0) or 0
        self.contracted_power_p2: float = entry.options.get(POWER_TERM_KEYS[1], 0) or 0
        self.price_power_p1: float = entry.options.get(POWER_TERM_KEYS[2], 0) or 0
        self.price_power_p2: float = entry.options.get(POWER_TERM_KEYS[3], 0) or 0
        self.last_success_time: datetime | None = None
        self._consecutive_failures = 0

    @property
    def daily_power_cost(self) -> float:
        """Término de potencia fijo por día — se factura siempre, no depende de qué franja horaria
        sea (a diferencia de la energía)."""
        return round(self.contracted_power_p1 * self.price_power_p1 + self.contracted_power_p2 * self.price_power_p2, 4)

    async def _async_update_data(self) -> dict:
        try:
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
                }

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
