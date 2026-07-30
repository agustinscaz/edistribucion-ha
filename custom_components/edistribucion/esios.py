"""Cliente para la API de ESIOS/REE (indicador 1001 = PVPC), para el tipo de tarifa "pvpc".

Los precios de mañana se publican sobre las 20:15h (hora peninsular) del día anterior — el
coordinator solo pide datos nuevos una vez al día (no cada 15 min), ya que el precio no cambia más
a menudo y ESIOS es una API pública que conviene no machacar sin necesidad.

Referencia: https://api.esios.ree.es/indicators/1001 (necesita una clave gratuita, solicitada por
email a REE — no hay alta instantánea como en Datadis).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from aiohttp import ClientError, ClientSession
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

INDICATOR_PVPC = 1001
BASE_URL = "https://api.esios.ree.es"
TIMEOUT = 20


class EsiosError(Exception):
    """Error hablando con la API de ESIOS."""


def _hourly_key(date_str: str, hour: int) -> str:
    """Misma forma de clave que usa `costs.hour_period`: 'DD/MM/YYYY' + hora, para poder cruzar
    directamente con el `hourlyByDate` que devuelve el add-on."""
    return f"{date_str} {hour}"


async def async_get_pvpc_prices(
    session: ClientSession, api_key: str, geo_id: str, start: datetime, end: datetime
) -> dict[str, float]:
    """Precios PVPC (€/kWh) hora a hora entre `start` y `end`, para la región `geo_id`.

    Devuelve un dict {"DD/MM/YYYY H": precio}, con H la hora SIN ceros a la izquierda (0-23), para
    cruzar directamente con las horas que trae `hourlyByDate` del add-on de e-distribución.
    """
    headers = {
        "Accept": "application/json; application/vnd.esios-api-v2+json",
        "Content-Type": "application/json",
        "Authorization": f'Token token="{api_key}"',
    }
    params = {
        "start_date": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "end_date": end.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    try:
        async with asyncio.timeout(TIMEOUT):
            resp = await session.get(f"{BASE_URL}/indicators/{INDICATOR_PVPC}", headers=headers, params=params)
            if resp.status >= 400:
                body = await resp.text()
                raise EsiosError(f"HTTP {resp.status} de ESIOS: {body[:300]}")
            data = await resp.json()
    except (ClientError, TimeoutError) as err:
        raise EsiosError(f"No se pudo conectar con ESIOS: {err}") from err

    prices: dict[str, float] = {}
    for entry in data.get("indicator", {}).get("values", []):
        if str(entry.get("geo_id")) != str(geo_id):
            continue
        raw_dt = entry.get("datetime")
        value = entry.get("value")
        if raw_dt is None or value is None:
            continue
        try:
            parsed = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
        except ValueError:
            continue
        local_dt = dt_util.as_local(parsed) if parsed.tzinfo else parsed
        prices[_hourly_key(local_dt.strftime("%d/%m/%Y"), local_dt.hour)] = float(value)

    return prices
