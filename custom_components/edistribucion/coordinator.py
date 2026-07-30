"""DataUpdateCoordinator: pide datos al add-on cada X minutos, uno por suministro."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import EdistribucionApiClient, EdistribucionApiError
from .const import DEFAULT_SCAN_INTERVAL_MINUTES

_LOGGER = logging.getLogger(__name__)

RANGE_WEEK = "2"
RANGE_MONTH = "3"


class EdistribucionCoordinator(DataUpdateCoordinator):
    """Mantiene: lista de suministros + consumo (hoy/semana/mes) y potencia de cada uno."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: EdistribucionApiClient,
        entry_id: str,
        update_interval_minutes: int = DEFAULT_SCAN_INTERVAL_MINUTES,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="edistribucion",
            update_interval=timedelta(minutes=update_interval_minutes),
        )
        self.client = client
        self.entry_id = entry_id
        self.last_success_time: datetime | None = None

    async def _async_update_data(self) -> dict:
        try:
            supply_points = await self.client.async_get_supply_points()
            data: dict[str, dict] = {}
            for sp in supply_points:
                cont_id = sp["contId"]
                cups_id = sp["cupsId"]
                bundle: dict = {"supply_point": sp, "consumption": None, "week": None, "month": None, "max_power_demand": None}

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
                    bundle["max_power_demand"] = await self.client.async_get_max_power_demand(cups_id)
                except EdistribucionApiError as err:
                    _LOGGER.debug("Sin potencia máxima para %s (normal si no tiene telegestión): %s", sp.get("cups"), err)

                data[cont_id] = bundle
            self.last_success_time = dt_util.utcnow()
            return data
        except EdistribucionApiError as err:
            raise UpdateFailed(f"Error hablando con el add-on de e-distribución: {err}") from err
