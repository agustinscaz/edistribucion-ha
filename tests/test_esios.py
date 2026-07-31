"""Tests de esios.py — cliente del archivo público de PVPC (ESIOS/REE). No depende de Home
Assistant, se ejecuta con un servidor aiohttp real de pruebas."""

from __future__ import annotations

from datetime import date

import pytest
from aiohttp import web

from custom_components.edistribucion.esios import (
    DEFAULT_PVPC_ZONE,
    ZONE_CEUTA_MELILLA,
    ZONE_PENINSULA_BALEARES_CANARIAS,
    EsiosError,
    async_get_pvpc_prices_for_day,
    pvpc_prices_to_csv,
)


async def _client_with_mock_archive(aiohttp_client, monkeypatch, app):
    """`async_get_pvpc_prices_for_day` llama a la URL ABSOLUTA `esios.ARCHIVE_URL` — el cliente de
    pruebas de aiohttp solo intercepta rutas relativas a su propio servidor, así que sin esto la
    llamada se iría de verdad a la ESIOS real en vez de al servidor de pruebas local."""
    client = await aiohttp_client(app)
    monkeypatch.setattr(
        "custom_components.edistribucion.esios.ARCHIVE_URL", str(client.make_url("/archives/70/download_json"))
    )
    return client


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


@pytest.mark.asyncio
async def test_parses_real_shaped_response(aiohttp_client, monkeypatch):
    rows = [_row("30/07/2026", f"{h}-{h + 1}", f"{100 + h},50") for h in range(24)]
    app = web.Application()

    async def handler(request):
        assert request.query["locale"] == "es"
        assert request.query["date"] == "2026-07-30"
        assert "Mozilla" in request.headers.get("User-Agent", "")  # evita el baneo de ESIOS
        return web.json_response(_pvpc_payload(rows))

    app.router.add_get("/archives/70/download_json", handler)
    client = await _client_with_mock_archive(aiohttp_client, monkeypatch, app)

    prices = await async_get_pvpc_prices_for_day(client.session, ZONE_PENINSULA_BALEARES_CANARIAS, date(2026, 7, 30))
    assert len(prices) == 24
    assert prices["30/07/2026 0"] == 0.1005
    assert prices["30/07/2026 23"] == round(123.5 / 1000, 5)


@pytest.mark.asyncio
async def test_uses_correct_zone_column(aiohttp_client, monkeypatch):
    rows = [_row("30/07/2026", "0-1", "100,0", "200,0")]
    app = web.Application()
    app.router.add_get("/archives/70/download_json", _json_handler(_pvpc_payload(rows)))
    client = await _client_with_mock_archive(aiohttp_client, monkeypatch, app)

    pcb_prices = await async_get_pvpc_prices_for_day(client.session, ZONE_PENINSULA_BALEARES_CANARIAS, date(2026, 7, 30))
    cym_prices = await async_get_pvpc_prices_for_day(client.session, ZONE_CEUTA_MELILLA, date(2026, 7, 30))
    assert pcb_prices["30/07/2026 0"] == 0.1
    assert cym_prices["30/07/2026 0"] == 0.2


@pytest.mark.asyncio
async def test_not_yet_published_day_returns_empty_dict(aiohttp_client, monkeypatch):
    """Comportamiento real observado contra ESIOS: día aún no publicado -> 200 sin clave "PVPC"."""
    app = web.Application()
    app.router.add_get("/archives/70/download_json", _json_handler({"message": "No values for specified archive"}))
    client = await _client_with_mock_archive(aiohttp_client, monkeypatch, app)

    prices = await async_get_pvpc_prices_for_day(client.session, DEFAULT_PVPC_ZONE, date(2026, 12, 31))
    assert prices == {}


@pytest.mark.asyncio
async def test_malformed_row_is_skipped_not_fatal(aiohttp_client, monkeypatch):
    rows = [
        _row("30/07/2026", "0-1", "100,0"),
        {"Dia": "30/07/2026", "Hora": "1-2", "PCB": "no-es-un-numero"},  # precio inválido
        {"Dia": "30/07/2026", "PCB": "100,0"},  # sin "Hora"
        _row("30/07/2026", "2-3", "102,0"),
    ]
    app = web.Application()
    app.router.add_get("/archives/70/download_json", _json_handler(_pvpc_payload(rows)))
    client = await _client_with_mock_archive(aiohttp_client, monkeypatch, app)

    prices = await async_get_pvpc_prices_for_day(client.session, DEFAULT_PVPC_ZONE, date(2026, 7, 30))
    assert set(prices) == {"30/07/2026 0", "30/07/2026 2"}


@pytest.mark.asyncio
async def test_http_error_raises_esios_error(aiohttp_client, monkeypatch):
    async def forbidden_handler(request):
        return web.Response(status=403, text="banned")

    app = web.Application()
    app.router.add_get("/archives/70/download_json", forbidden_handler)
    client = await _client_with_mock_archive(aiohttp_client, monkeypatch, app)

    with pytest.raises(EsiosError, match="403"):
        await async_get_pvpc_prices_for_day(client.session, DEFAULT_PVPC_ZONE, date(2026, 7, 30))


@pytest.mark.asyncio
async def test_connection_error_raises_esios_error(aiohttp_client, monkeypatch):
    from aiohttp import ClientConnectionError

    app = web.Application()
    client = await aiohttp_client(app)

    async def failing_get(*args, **kwargs):
        raise ClientConnectionError("no hay red")

    monkeypatch.setattr(client.session, "get", failing_get)
    with pytest.raises(EsiosError):
        await async_get_pvpc_prices_for_day(client.session, DEFAULT_PVPC_ZONE, date(2026, 7, 30))


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
