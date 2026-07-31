"""Tests de api.py — cliente HTTP hacia el add-on. No depende de Home Assistant (solo aiohttp), así
que se ejecuta con un servidor aiohttp real de pruebas (aiohttp.test_utils), no con mocks a ciegas."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from custom_components.edistribucion.api import EdistribucionApiClient, EdistribucionApiError, InvalidCredentialsError

# Monta un aiohttp.test_utils.TestServer real (socket local de verdad) — en CI,
# pytest-homeassistant-custom-component trae pytest-socket, que bloquea sockets por defecto para
# evitar llamadas de red reales durante los tests de HA. Aquí el socket es local (127.0.0.1), así
# que se permite explícitamente para todo el módulo.
pytestmark = pytest.mark.enable_socket


def _json_handler(payload, status: int = 200):
    async def handler(request):
        return web.json_response(payload, status=status)

    return handler


@asynccontextmanager
async def _make_client(app):
    """Arranca el servidor de pruebas y lo cierra al salir, todo dentro de la misma tarea que
    corre el test — NO se usa el fixture `aiohttp_client` de pytest-aiohttp: en CI convive en la
    misma sesión con pytest-homeassistant-custom-component, que gestiona su propio event loop para
    el fixture `hass`, y mezclar ambos rompía `asyncio.timeout()` con
    "Timeout context manager should be used inside a task"."""
    async with TestClient(TestServer(app)) as test_client:
        api_client = EdistribucionApiClient(test_client.session, "localhost", 0)
        api_client._base_url = str(test_client.make_url(""))  # apuntar al servidor de pruebas real
        yield api_client, test_client


async def test_health_ok():
    app = web.Application()
    app.router.add_get("/health", _json_handler({"ok": True}))
    async with _make_client(app) as (client, _):
        assert await client.async_health() is True


async def test_health_false():
    app = web.Application()
    app.router.add_get("/health", _json_handler({"ok": False}))
    async with _make_client(app) as (client, _):
        assert await client.async_health() is False


async def test_get_info():
    app = web.Application()
    app.router.add_get("/info", _json_handler({"name": "Agustín", "visId": "abc"}))
    async with _make_client(app) as (client, _):
        info = await client.async_get_info()
        assert info == {"name": "Agustín", "visId": "abc"}


async def test_get_supply_points():
    app = web.Application()
    app.router.add_get("/supply-points", _json_handler([{"cups": "ES123"}]))
    async with _make_client(app) as (client, _):
        points = await client.async_get_supply_points()
        assert points == [{"cups": "ES123"}]


async def test_get_consumption_no_params():
    captured = {}

    async def handler(request):
        captured["query"] = dict(request.query)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get("/consumption/{cont_id}", handler)
    async with _make_client(app) as (client, _):
        await client.async_get_consumption("c1")
        assert captured["query"] == {}


async def test_get_consumption_with_range_and_date():
    captured = {}

    async def handler(request):
        captured["raw"] = request.query_string
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get("/consumption/{cont_id}", handler)
    async with _make_client(app) as (client, _):
        await client.async_get_consumption("c1", range_type="3", date="2026-07-01")
        assert captured["raw"] == "range=3&date=2026-07-01"


async def test_get_max_power_demand():
    app = web.Application()
    app.router.add_get("/max-power-demand/{cups_id}", _json_handler({"maxValue": 3.5}))
    async with _make_client(app) as (client, _):
        result = await client.async_get_max_power_demand("cups1")
        assert result == {"maxValue": 3.5}


async def test_get_contracted_power():
    app = web.Application()
    app.router.add_get("/contracted-power/{cont_id}", _json_handler({"contractedPowerPuntaKw": 3.5}))
    async with _make_client(app) as (client, _):
        result = await client.async_get_contracted_power("c1")
        assert result == {"contractedPowerPuntaKw": 3.5}


async def test_relogin_is_post():
    captured = {}

    async def handler(request):
        captured["method"] = request.method
        return web.json_response({"name": "Agustín"})

    app = web.Application()
    app.router.add_post("/relogin", handler)
    async with _make_client(app) as (client, _):
        await client.async_relogin()
        assert captured["method"] == "POST"


async def test_invalid_credentials_raises_specific_error():
    async def handler(request):
        return web.json_response({"error": "bad creds", "code": "invalid_credentials"}, status=401)

    app = web.Application()
    app.router.add_get("/info", handler)
    async with _make_client(app) as (client, _):
        with pytest.raises(InvalidCredentialsError):
            await client.async_get_info()


async def test_401_without_invalid_credentials_code_is_generic_error():
    async def handler(request):
        return web.json_response({"error": "otra cosa"}, status=401)

    app = web.Application()
    app.router.add_get("/info", handler)
    async with _make_client(app) as (client, _):
        with pytest.raises(EdistribucionApiError) as exc_info:
            await client.async_get_info()
        assert not isinstance(exc_info.value, InvalidCredentialsError)


async def test_generic_http_error_raises():
    async def handler(request):
        return web.Response(status=502, text="bad gateway")

    app = web.Application()
    app.router.add_get("/info", handler)
    async with _make_client(app) as (client, _):
        with pytest.raises(EdistribucionApiError, match="502"):
            await client.async_get_info()


async def test_malformed_error_body_still_raises_generic_error():
    """Si el cuerpo del error no es JSON válido, no debe petar — debe caer al error genérico."""

    async def handler(request):
        return web.Response(status=500, text="<html>oops</html>")

    app = web.Application()
    app.router.add_get("/info", handler)
    async with _make_client(app) as (client, _):
        with pytest.raises(EdistribucionApiError, match="500"):
            await client.async_get_info()


async def test_network_error_retries_and_then_raises(monkeypatch):
    """Un fallo de red persistente debe reintentar (RETRY_DELAYS_S) y acabar lanzando
    EdistribucionApiError, no colgarse ni propagar el ClientError crudo."""
    from aiohttp import ClientConnectionError

    app = web.Application()
    async with _make_client(app) as (client, _):
        attempts = {"count": 0}

        async def failing_request(method, url):
            attempts["count"] += 1
            raise ClientConnectionError("conexión rechazada")

        monkeypatch.setattr(client._session, "request", failing_request)
        # Acelerar el test: no queremos esperar de verdad los 1s+2s de reintento.
        monkeypatch.setattr("custom_components.edistribucion.api.asyncio.sleep", lambda _: _fast_sleep())

        with pytest.raises(EdistribucionApiError):
            await client.async_get_info()
        assert attempts["count"] == 3  # los 3 intentos de RETRY_DELAYS_S


async def _fast_sleep():
    return None


async def test_health_response_not_json_bubbles_as_client_error_path():
    """Una respuesta 200 pero no-JSON debería propagar el fallo de parseo — documenta el
    comportamiento actual (no hay manejo especial para 200 no-JSON, a diferencia de los errores)."""

    async def handler(request):
        return web.Response(status=200, text="no soy json", content_type="text/plain")

    app = web.Application()
    app.router.add_get("/health", handler)
    async with _make_client(app) as (client, _):
        with pytest.raises(Exception):
            await client.async_health()
