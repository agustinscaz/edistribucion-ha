"""Tests de esios.py — cliente del archivo público de PVPC (ESIOS/REE). No depende de Home
Assistant, se ejecuta con un servidor aiohttp real de pruebas."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from custom_components.edistribucion.esios import (
    DEFAULT_PVPC_ZONE,
    ZONE_CEUTA_MELILLA,
    ZONE_PENINSULA_BALEARES_CANARIAS,
    EsiosError,
    async_get_pvpc_prices_for_day,
    cheapest_window,
    pvpc_prices_to_csv,
)

# Servidor aiohttp real de pruebas (socket local) — pytest-socket (traído por
# pytest-homeassistant-custom-component en CI) bloquea sockets por defecto; se permite para todo
# el módulo, igual que en test_api.py.
pytestmark = pytest.mark.enable_socket


@asynccontextmanager
async def _client_with_mock_archive(monkeypatch, app):
    """Arranca el servidor de pruebas dentro de la propia tarea del test (ver test_api.py para el
    motivo de no usar el fixture `aiohttp_client` de pytest-aiohttp) y redirige la URL ABSOLUTA
    `esios.ARCHIVE_URL` hacia él — si no, `async_get_pvpc_prices_for_day` se iría de verdad a la
    ESIOS real en vez de al servidor de pruebas local."""
    async with TestClient(TestServer(app)) as test_client:
        monkeypatch.setattr(
            "custom_components.edistribucion.esios.ARCHIVE_URL", str(test_client.make_url("/archives/70/download_json"))
        )
        yield test_client


def _pvpc_payload(rows):
    return {"PVPC": rows}


def _json_handler(payload, status: int = 200):
    async def handler(request):
        return web.json_response(payload, status=status)

    return handler


def _row(dia: str, hora: str, pcb: str, cym: str | None = None) -> dict:
    row = {"Dia": dia, "Hora": hora, "PCB": pcb}
    if cym is not None:
        row["CYM"] = cym
    return row


async def test_parses_real_shaped_response(monkeypatch):
    rows = [_row("30/07/2026", f"{h}-{h + 1}", f"{100 + h},50") for h in range(24)]
    app = web.Application()

    async def handler(request):
        assert request.query["locale"] == "es"
        assert request.query["date"] == "2026-07-30"
        assert "Mozilla" in request.headers.get("User-Agent", "")  # evita el baneo de ESIOS
        return web.json_response(_pvpc_payload(rows))

    app.router.add_get("/archives/70/download_json", handler)
    async with _client_with_mock_archive(monkeypatch, app) as client:
        prices = await async_get_pvpc_prices_for_day(client.session, ZONE_PENINSULA_BALEARES_CANARIAS, date(2026, 7, 30))
        assert len(prices) == 24
        assert prices["30/07/2026 0"] == 0.1005
        assert prices["30/07/2026 23"] == round(123.5 / 1000, 5)


async def test_uses_correct_zone_column(monkeypatch):
    rows = [_row("30/07/2026", "0-1", "100,0", "200,0")]
    app = web.Application()
    app.router.add_get("/archives/70/download_json", _json_handler(_pvpc_payload(rows)))
    async with _client_with_mock_archive(monkeypatch, app) as client:
        pcb_prices = await async_get_pvpc_prices_for_day(client.session, ZONE_PENINSULA_BALEARES_CANARIAS, date(2026, 7, 30))
        cym_prices = await async_get_pvpc_prices_for_day(client.session, ZONE_CEUTA_MELILLA, date(2026, 7, 30))
        assert pcb_prices["30/07/2026 0"] == 0.1
        assert cym_prices["30/07/2026 0"] == 0.2


async def test_not_yet_published_day_returns_empty_dict(monkeypatch):
    """Comportamiento real observado contra ESIOS: día aún no publicado -> 200 sin clave "PVPC"."""
    app = web.Application()
    app.router.add_get("/archives/70/download_json", _json_handler({"message": "No values for specified archive"}))
    async with _client_with_mock_archive(monkeypatch, app) as client:
        prices = await async_get_pvpc_prices_for_day(client.session, DEFAULT_PVPC_ZONE, date(2026, 12, 31))
        assert prices == {}


async def test_malformed_row_is_skipped_not_fatal(monkeypatch):
    rows = [
        _row("30/07/2026", "0-1", "100,0"),
        {"Dia": "30/07/2026", "Hora": "1-2", "PCB": "no-es-un-numero"},  # precio inválido
        {"Dia": "30/07/2026", "PCB": "100,0"},  # sin "Hora"
        _row("30/07/2026", "2-3", "102,0"),
    ]
    app = web.Application()
    app.router.add_get("/archives/70/download_json", _json_handler(_pvpc_payload(rows)))
    async with _client_with_mock_archive(monkeypatch, app) as client:
        prices = await async_get_pvpc_prices_for_day(client.session, DEFAULT_PVPC_ZONE, date(2026, 7, 30))
        assert set(prices) == {"30/07/2026 0", "30/07/2026 2"}


async def test_http_error_raises_esios_error(monkeypatch):
    async def forbidden_handler(request):
        return web.Response(status=403, text="banned")

    app = web.Application()
    app.router.add_get("/archives/70/download_json", forbidden_handler)
    async with _client_with_mock_archive(monkeypatch, app) as client:
        with pytest.raises(EsiosError, match="403"):
            await async_get_pvpc_prices_for_day(client.session, DEFAULT_PVPC_ZONE, date(2026, 7, 30))


async def test_connection_error_raises_esios_error(monkeypatch):
    from aiohttp import ClientConnectionError

    app = web.Application()
    async with TestClient(TestServer(app)) as test_client:

        async def failing_get(*args, **kwargs):
            raise ClientConnectionError("no hay red")

        monkeypatch.setattr(test_client.session, "get", failing_get)
        with pytest.raises(EsiosError):
            await async_get_pvpc_prices_for_day(test_client.session, DEFAULT_PVPC_ZONE, date(2026, 7, 30))


class TestPvpcPricesToCsv:
    def test_empty_input(self):
        assert pvpc_prices_to_csv({}) == "zona,fecha,hora,precio_eur_kwh"

    def test_single_zone_sorted_chronologically(self):
        prices = {"PCB": {"30/07/2026 2": 0.12, "30/07/2026 0": 0.10, "30/07/2026 1": 0.11}}
        csv = pvpc_prices_to_csv(prices)
        lines = csv.splitlines()
        assert lines[0] == "zona,fecha,hora,precio_eur_kwh"
        assert lines[1:] == [
            "PCB,30/07/2026,0,0.1",
            "PCB,30/07/2026,1,0.11",
            "PCB,30/07/2026,2,0.12",
        ]

    def test_sorts_across_month_boundary(self):
        """31/07 debe ir antes que 01/08 pese a que "01" ordena antes que "31" como texto."""
        prices = {"PCB": {"01/08/2026 0": 0.20, "31/07/2026 23": 0.19}}
        csv = pvpc_prices_to_csv(prices)
        lines = csv.splitlines()[1:]
        assert lines == ["PCB,31/07/2026,23,0.19", "PCB,01/08/2026,0,0.2"]

    def test_multiple_zones_included_by_default(self):
        prices = {"PCB": {"30/07/2026 0": 0.10}, "CYM": {"30/07/2026 0": 0.20}}
        csv = pvpc_prices_to_csv(prices)
        assert "PCB,30/07/2026,0,0.1" in csv
        assert "CYM,30/07/2026,0,0.2" in csv

    def test_zone_filter_excludes_other_zones(self):
        prices = {"PCB": {"30/07/2026 0": 0.10}, "CYM": {"30/07/2026 0": 0.20}}
        csv = pvpc_prices_to_csv(prices, zone_filter="PCB")
        assert "PCB" in csv
        assert "CYM" not in csv

    def test_zone_filter_matching_nothing_returns_only_header(self):
        prices = {"PCB": {"30/07/2026 0": 0.10}}
        csv = pvpc_prices_to_csv(prices, zone_filter="CYM")
        assert csv == "zona,fecha,hora,precio_eur_kwh"


class TestCheapestWindow:
    def _prices(self, day: str, values: list[float]) -> dict[str, float]:
        return {f"{day} {h}": v for h, v in enumerate(values)}

    def test_none_without_prices(self):
        assert cheapest_window({}, 2, datetime(2026, 7, 30, 0)) is None

    def test_none_with_invalid_window(self):
        prices = self._prices("30/07/2026", [0.1] * 24)
        assert cheapest_window(prices, 0, datetime(2026, 7, 30, 0)) is None

    def test_none_without_enough_upcoming_hours(self):
        prices = self._prices("30/07/2026", [0.1] * 24)
        # A las 22h del día solo quedan 2 horas por delante (22, 23) — pedir 3 no puede cumplirse.
        assert cheapest_window(prices, 3, datetime(2026, 7, 30, 22)) is None

    def test_finds_the_cheapest_consecutive_window(self):
        # Precios de un día: caros salvo un valle claro en la madrugada (horas 2-3).
        values = [0.20, 0.20, 0.05, 0.05, 0.20, 0.20, 0.20, 0.20, 0.20, 0.20, 0.20, 0.20, 0.20, 0.20, 0.20, 0.20, 0.20, 0.20, 0.20, 0.20, 0.20, 0.20, 0.20, 0.20]
        prices = self._prices("30/07/2026", values)
        result = cheapest_window(prices, 2, datetime(2026, 7, 30, 0))
        assert result == {"inicio": "30/07/2026 2h", "horas": 2, "precio_medio_eur_kwh": 0.05}

    def test_only_considers_hours_from_now_onwards(self):
        """Aunque la hora más barata del día ya haya pasado, no debe elegirla — solo cuenta desde
        `now` en adelante."""
        values = [0.05] + [0.20] * 23  # la más barata es la 0h, que ya pasó a las 10h
        prices = self._prices("30/07/2026", values)
        result = cheapest_window(prices, 1, datetime(2026, 7, 30, 10))
        assert result["inicio"] == "30/07/2026 10h"

    def test_skips_windows_with_gaps(self):
        """Si falta una hora en medio (p.ej. sin precio publicado todavía), esa ventana no cuenta
        como "consecutiva", aunque las horas que sí están sean muy baratas."""
        prices = {"30/07/2026 0": 0.01, "30/07/2026 2": 0.01}  # falta la hora 1 -> no son consecutivas
        assert cheapest_window(prices, 2, datetime(2026, 7, 30, 0)) is None

    def test_window_spans_midnight_correctly(self):
        """Una ventana que cruza medianoche (23h de un día + 0h del siguiente) sí debe contar como
        consecutiva."""
        prices = {
            "30/07/2026 23": 0.05,
            "31/07/2026 0": 0.05,
            "31/07/2026 1": 0.20,
        }
        result = cheapest_window(prices, 2, datetime(2026, 7, 30, 23))
        assert result == {"inicio": "30/07/2026 23h", "horas": 2, "precio_medio_eur_kwh": 0.05}
