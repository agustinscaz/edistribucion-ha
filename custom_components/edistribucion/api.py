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

    async def _request(self, method: str, path: str) -> dict | list:
        try:
            async with asyncio.timeout(TIMEOUT):
                resp = await self._session.request(method, f"{self._base_url}{path}")
                if resp.status >= 400:
                    body = await resp.text()
                    raise EdistribucionApiError(f"{path} -> HTTP {resp.status}: {body[:300]}")
                return await resp.json()
        except (ClientError, TimeoutError) as err:
            raise EdistribucionApiError(f"No se pudo conectar con el add-on ({path}): {err}") from err

    async def _get(self, path: str) -> dict | list:
        return await self._request("GET", path)

    async def async_health(self) -> bool:
        data = await self._get("/health")
        return bool(data.get("ok"))

    async def async_get_info(self) -> dict:
        return await self._get("/info")

    async def async_get_supply_points(self) -> list[dict]:
        return await self._get("/supply-points")

    async def async_get_consumption(self, cont_id: str, range_type: str | None = None, date: str | None = None) -> dict:
        path = f"/consumption/{cont_id}"
        params = []
        if range_type:
            params.append(f"range={range_type}")
        if date:
            params.append(f"date={date}")
        if params:
            path += "?" + "&".join(params)
        return await self._get(path)

    async def async_get_max_power_demand(self, cups_id: str) -> dict:
        return await self._get(f"/max-power-demand/{cups_id}")

    async def async_relogin(self) -> dict:
        """Fuerza un login fresco en el add-on (botón 'Forzar reconexión')."""
        return await self._request("POST", "/relogin")
