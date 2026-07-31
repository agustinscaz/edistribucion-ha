"""Tests de __init__.py — alta/baja de la integración, migración de opciones legadas al arrancar, y
los dos servicios (consultar_consumo, exportar_precios_pvpc). Necesita objetos reales de Home
Assistant — se verifica vía CI, no en el sandbox de desarrollo local (sin pip)."""

from __future__ import annotations

import pytest
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.edistribucion.const import CONF_SUPPLY_POINTS, DOMAIN

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

_SUPPLY_POINTS = [
    {
        "cups": "ES0031500160526001DS0F",
        "cupsId": "cupsA",
        "contId": "contA",
        "address": "AV MALLORCA 48",
        "tariff": "2.0TD",
        "active": True,
        "startDate": "2026-01-01",
        "endDate": None,
    }
]


@pytest.fixture
def mock_add_on(aioclient_mock):
    aioclient_mock.get("http://localhost:8099/supply-points", json=_SUPPLY_POINTS)
    aioclient_mock.get("http://localhost:8099/consumption/contA", json={"totalImportedKwh": 5.0, "hourlyByDate": {}})
    aioclient_mock.get(
        "http://localhost:8099/consumption/contA",
        params={"range": "2"},
        json={"totalImportedKwh": 5.0, "hourlyByDate": {}},
    )
    aioclient_mock.get(
        "http://localhost:8099/consumption/contA",
        params={"range": "3"},
        json={"totalImportedKwh": 20.0, "hourlyByDate": {}},
    )
    aioclient_mock.get("http://localhost:8099/max-power-demand/cupsA", json={"maxValue": 3.5, "points": []})
    aioclient_mock.get(
        "http://localhost:8099/contracted-power/contA",
        json={"contractedPowerPuntaKw": 3.5, "contractedPowerValleKw": 3.5},
    )
    return aioclient_mock


async def test_setup_and_unload_entry(hass, mock_add_on):
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "localhost", "port": 8099}, options={})
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.entry_id in hass.data[DOMAIN]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.entry_id not in hass.data[DOMAIN]


async def test_legacy_options_are_migrated_on_setup(hass, mock_add_on):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"host": "localhost", "port": 8099},
        options={"price_power_punta": 0.08, CONF_SUPPLY_POINTS: {"contA": {}}},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert "price_power_punta" not in entry.options
    assert entry.options[CONF_SUPPLY_POINTS]["contA"]["price_power_punta"] == 0.08


async def test_services_are_registered_after_setup(hass, mock_add_on):
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "localhost", "port": 8099}, options={})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.services.has_service(DOMAIN, "consultar_consumo")
    assert hass.services.has_service(DOMAIN, "exportar_precios_pvpc")


class TestConsultarConsumoService:
    @pytest.fixture
    async def setup_entry(self, hass, mock_add_on):
        entry = MockConfigEntry(domain=DOMAIN, data={"host": "localhost", "port": 8099}, options={})
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return entry

    async def test_success(self, hass, setup_entry):
        device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "contA")})
        assert device is not None

        result = await hass.services.async_call(
            DOMAIN,
            "consultar_consumo",
            {"device_id": device.id, "range": "3"},
            blocking=True,
            return_response=True,
        )
        assert result["totalImportedKwh"] == 20.0

    async def test_unknown_device_raises(self, hass, setup_entry):
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN,
                "consultar_consumo",
                {"device_id": "no-existe"},
                blocking=True,
                return_response=True,
            )

    async def test_device_not_belonging_to_integration_raises(self, hass, setup_entry):
        other_entry = MockConfigEntry(domain="otro_dominio")
        other_entry.add_to_hass(hass)
        other_device = dr.async_get(hass).async_get_or_create(
            config_entry_id=other_entry.entry_id, identifiers={("otro_dominio", "algo")}
        )
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN,
                "consultar_consumo",
                {"device_id": other_device.id},
                blocking=True,
                return_response=True,
            )


class TestExportarPreciosPvpcService:
    async def test_returns_csv_from_coordinator_cache(self, hass, mock_add_on):
        entry = MockConfigEntry(domain=DOMAIN, data={"host": "localhost", "port": 8099}, options={})
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hass.data[DOMAIN][entry.entry_id]
        coordinator.pvpc_prices = {"PCB": {"30/07/2026 0": 0.15}, "CYM": {"30/07/2026 0": 0.20}}

        result = await hass.services.async_call(
            DOMAIN, "exportar_precios_pvpc", {}, blocking=True, return_response=True
        )
        assert "PCB,30/07/2026,0,0.15" in result["csv"]
        assert "CYM,30/07/2026,0,0.2" in result["csv"]

    async def test_zone_filter(self, hass, mock_add_on):
        entry = MockConfigEntry(domain=DOMAIN, data={"host": "localhost", "port": 8099}, options={})
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hass.data[DOMAIN][entry.entry_id]
        coordinator.pvpc_prices = {"PCB": {"30/07/2026 0": 0.15}, "CYM": {"30/07/2026 0": 0.20}}

        result = await hass.services.async_call(
            DOMAIN, "exportar_precios_pvpc", {"zona": "PCB"}, blocking=True, return_response=True
        )
        assert "PCB" in result["csv"]
        assert "CYM" not in result["csv"]
