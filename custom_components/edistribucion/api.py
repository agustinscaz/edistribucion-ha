"""Cliente HTTP ligero hacia el add-on `edistribucion` (API local, sin navegador desde aquí)."""

from __future__ import annotations

import asyncio

from aiohttp import ClientSession, ClientError

TIMEOUT = 30


class EdistribucionApiError(Exception):
    """Error genérico al hablar con el add-on."""


class EdistribucionApiClient:
    """Envuelve las llamadas HTTP al add-on `edistribucion-ha-addon`."""

    def __init__(self, session: ClientSession, host: str, port: int) -> None:
        self._session = session
        self._base_url = f"http://{host}:{port}"

    async def _get(self, path: str) -> dict | list:
        try:
            async with asyncio.timeout(TIMEOUT):
                resp = await self._session.get(f"{self._base_url}{path}")
                if resp.status >= 400:
                    body = await resp.text()
                    raise EdistribucionApiError(f"{path} -> HTTP {resp.status}: {body[:300]}")
                return await resp.json()
        except (ClientError, TimeoutError) as err:
            raise EdistribucionApiError(f"No se pudo conectar con el add-on ({path}): {err}") from err

    async def async_health(self) -> bool:
        data = await self._get("/health")
        return bool(data.get("ok"))

    async def async_get_info(self) -> dict:
        return await self._get("/info")

    async def async_get_supply_points(self) -> list[dict]:
        return await self._get("/supply-points")

    async def async_get_consumption(self, cont_id: str) -> dict:
        return await self._get(f"/consumption/{cont_id}")

    async def async_get_max_power_demand(self, cups_id: str) -> dict:
        return await self._get(f"/max-power-demand/{cups_id}")
