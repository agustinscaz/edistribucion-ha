"""Cliente HTTP ligero hacia el add-on `edistribucion` (API local, sin navegador desde aquí)."""

from __future__ import annotations

import asyncio
import logging

from aiohttp import ClientSession, ClientError

_LOGGER = logging.getLogger(__name__)

TIMEOUT = 30
RETRY_DELAYS_S = (0, 1, 2)  # 3 intentos en total: inmediato, +1s, +2s


class EdistribucionApiError(Exception):
    """Error genérico al hablar con el add-on (red, sesión, etc.)."""


class InvalidCredentialsError(EdistribucionApiError):
    """El add-on ha rechazado el login por credenciales incorrectas — reintentar no sirve de nada,
    hace falta corregir dni/password en la configuración del add-on."""


class EdistribucionApiClient:
    """Envuelve las llamadas HTTP al add-on `edistribucion-ha-addon`."""

    def __init__(self, session: ClientSession, host: str, port: int) -> None:
        self._session = session
        self._base_url = f"http://{host}:{port}"

    async def _request(self, method: str, path: str) -> dict | list:
        last_err: Exception | None = None
        for attempt, delay in enumerate(RETRY_DELAYS_S):
            if delay:
                await asyncio.sleep(delay)
            try:
                async with asyncio.timeout(TIMEOUT):
                    resp = await self._session.request(method, f"{self._base_url}{path}")
                    if resp.status >= 400:
                        return await self._raise_for_status(resp, path)
                    return await resp.json()
            except (ClientError, TimeoutError) as err:
                last_err = err
                if attempt < len(RETRY_DELAYS_S) - 1:
                    _LOGGER.debug("Fallo de red hablando con el add-on (%s), reintentando: %s", path, err)
        raise EdistribucionApiError(f"No se pudo conectar con el add-on ({path}) tras {len(RETRY_DELAYS_S)} intentos: {last_err}") from last_err

    @staticmethod
    async def _raise_for_status(resp, path: str):
        body_text = await resp.text()
        code = None
        try:
            import json

            code = json.loads(body_text).get("code")
        except (ValueError, AttributeError):
            pass
        if resp.status == 401 and code == "invalid_credentials":
            raise InvalidCredentialsError("El add-on rechazó el login: credenciales incorrectas")
        raise EdistribucionApiError(f"{path} -> HTTP {resp.status}: {body_text[:300]}")

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

    async def async_get_contracted_power(self, cont_id: str) -> dict:
        """Potencia contratada real (punta/valle, kW) + metadatos del contrato, sacados
        directamente de e-distribución — no hace falta que el usuario los teclee."""
        return await self._get(f"/contracted-power/{cont_id}")

    async def async_relogin(self) -> dict:
        """Fuerza un login fresco en el add-on (botón 'Forzar reconexión')."""
        return await self._request("POST", "/relogin")
