"""Precio PVPC hora a hora para la tarifa "pvpc", sacado del archivo público de PVPC de ESIOS/REE
(archivo nº 70) — el mismo JSON que usa la propia web de REE para publicar el precio, así que NO
hace falta pedir ninguna clave/API key: es un endpoint público, un día por petición.

Los precios de mañana se publican sobre las 20:15h (hora peninsular) del día anterior — el
coordinator solo pide datos nuevos una vez al día (no cada 15 min), ya que el precio no cambia más
a menudo y conviene no pedir de más a una API pública sin necesidad.

OJO: ESIOS devuelve 403 si detecta un user-agent "de librería" (p.ej. "aiohttp/..."), así que se
manda un user-agent de navegador normal — es un comportamiento conocido y documentado en otras
integraciones que usan este mismo endpoint (p.ej. aiopvpc, la librería tras la integración oficial
`pvpc_hourly_pricing` de Home Assistant).

Referencia: https://api.esios.ree.es/archives/70/download_json?locale=es&date=YYYY-MM-DD
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date

from aiohttp import ClientError, ClientSession

_LOGGER = logging.getLogger(__name__)

ARCHIVE_URL = "https://api.esios.ree.es/archives/70/download_json"
TIMEOUT = 20

_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# El PVPC es el mismo para Península/Baleares/Canarias desde la reforma 2.0TD — solo Ceuta y
# Melilla tienen un precio distinto (coste extra de generación/interconexión).
ZONE_PENINSULA_BALEARES_CANARIAS = "PCB"
ZONE_CEUTA_MELILLA = "CYM"
PVPC_ZONES = {
    ZONE_PENINSULA_BALEARES_CANARIAS: "Península / Baleares / Canarias",
    ZONE_CEUTA_MELILLA: "Ceuta y Melilla",
}
DEFAULT_PVPC_ZONE = ZONE_PENINSULA_BALEARES_CANARIAS


class EsiosError(Exception):
    """Error hablando con el archivo público de ESIOS, o baneo por user-agent."""


async def async_get_pvpc_prices_for_day(session: ClientSession, zone: str, day: date) -> dict[str, float]:
    """Precios PVPC (€/kWh) de UN día, para la zona `zone` ("PCB" o "CYM").

    Devuelve un dict {"DD/MM/YYYY H": precio}, con H la hora SIN ceros a la izquierda (0-23), para
    cruzar directamente con las horas que trae `hourlyByDate` del add-on de e-distribución.
    """
    headers = {"Accept": "application/json", "User-Agent": _USER_AGENT}
    params = {"locale": "es", "date": day.strftime("%Y-%m-%d")}
    try:
        async with asyncio.timeout(TIMEOUT):
            resp = await session.get(ARCHIVE_URL, headers=headers, params=params)
            if resp.status >= 400:
                body = await resp.text()
                raise EsiosError(f"HTTP {resp.status} de ESIOS: {body[:300]}")
            data = await resp.json(content_type=None)
    except (ClientError, TimeoutError) as err:
        raise EsiosError(f"No se pudo conectar con ESIOS: {err}") from err

    prices: dict[str, float] = {}
    for row in data.get("PVPC") or []:
        date_str = row.get("Dia")
        hour_label = row.get("Hora")  # tipo "13-14"
        raw_value = row.get(zone)
        if not date_str or not hour_label or raw_value is None:
            continue
        try:
            hour = int(hour_label.split("-")[0])
            price = round(float(str(raw_value).replace(",", ".")) / 1000.0, 5)  # €/MWh -> €/kWh
        except (ValueError, IndexError):
            continue
        prices[f"{date_str} {hour}"] = price

    return prices
