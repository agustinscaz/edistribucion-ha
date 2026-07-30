"""DataUpdateCoordinator: pide datos al add-on cada X minutos, uno por suministro."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EdistribucionApiClient, EdistribucionApiError
from .const import DEFAULT_SCAN_INTERVAL_MINUTES

_LOGGER = logging.getLogger(__name__)


class EdistribucionCoordinator(DataUpdateCoordinator):
    """Mantiene: lista de suministros + consumo/potencia de cada uno."""

    def __init__(self, hass: HomeAssistant, client: EdistribucionApiClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="edistribucion",
            update_interval=timedelta(minutes=DEFAULT_SCAN_INTERVAL_MINUTES),
        )
        self.client = client

    async def _async_update_data(self) -> dict:
        try:
            supply_points = await self.client.async_get_supply_points()
            data: dict[str, dict] = {}
            for sp in supply_points:
                cont_id = sp["contId"]
                cups_id = sp["cupsId"]
                consumption = None
                power = None
                try:
                    consumption = await self.client.async_get_consumption(cont_id)
                except EdistribucionApiError as err:
                    _LOGGER.warning("No se pudo leer consumo de %s: %s", sp.get("cups"), err)
                try:
                    power = await self.client.async_get_max_power_demand(cups_id)
                except EdistribucionApiError as err:
                    _LOGGER.debug("Sin potencia máxima para %s (normal si no tiene telegestión): %s", sp.get("cups"), err)
                data[cont_id] = {"supply_point": sp, "consumption": consumption, "max_power_demand": power}
            return data
        except EdistribucionApiError as err:
            raise UpdateFailed(f"Error hablando con el add-on de e-distribución: {err}") from err
