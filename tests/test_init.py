"""Tests de __init__.py — alta/baja de la integración, migración de opciones legadas al arrancar, y
los dos servicios (consultar_consumo, exportar_precios_pvpc). Necesita objetos reales de Home
Assistant — se verifica vía CI, no en el sandbox de desarrollo local (sin pip)."""

from __future__ import annotations

import pytest
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
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
    # Los mocks CON `params` deben registrarse antes que el genérico sin params: aioclient_mock
    # compara en orden de registro y un mock sin `params` hace de comodín (coincide con cualquier
    # query) — si fuera el primero, "ganaría" también para las llamadas con range=2/range=3.
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
    aioclient_mock.get("http://localhost:8099/consumption/contA", json={"totalImportedKwh": 5.0, "hourlyByDate": {}})
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
    assert hass.services.has_service(DOMAIN, "horas_mas_baratas_pvpc")
    assert hass.services.has_service(DOMAIN, "resumen_mensual")
    assert hass.services.has_service(DOMAIN, "rellenar_historico")


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


class TestHorasMasBaratasPvpcService:
    async def test_finds_cheapest_window(self, hass, mock_add_on):
        entry = MockConfigEntry(domain=DOMAIN, data={"host": "localhost", "port": 8099}, options={})
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hass.data[DOMAIN][entry.entry_id]
        now = dt_util.now()
        today_str = now.strftime("%d/%m/%Y")
        prices = {f"{today_str} {h}": 0.30 for h in range(24)}
        prices[f"{today_str} {now.hour}"] = 0.05  # la hora actual, la más barata
        coordinator.pvpc_prices = {"PCB": prices}

        result = await hass.services.async_call(
            DOMAIN, "horas_mas_baratas_pvpc", {"horas": 1}, blocking=True, return_response=True
        )
        assert result["precio_medio_eur_kwh"] == 0.05

    async def test_raises_when_not_enough_consecutive_hours(self, hass, mock_add_on):
        entry = MockConfigEntry(domain=DOMAIN, data={"host": "localhost", "port": 8099}, options={})
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN, "horas_mas_baratas_pvpc", {"horas": 5}, blocking=True, return_response=True
            )


class TestResumenMensualService:
    async def test_returns_csv_summary(self, hass, mock_add_on):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={"host": "localhost", "port": 8099},
            options={CONF_SUPPLY_POINTS: {"contA": {"tariff_type": "fija", "fixed_price": 0.2}}},
        )
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "contA")})
        result = await hass.services.async_call(
            DOMAIN, "resumen_mensual", {"device_id": device.id}, blocking=True, return_response=True
        )
        assert "tarifa,fija" in result["resumen"]
        assert "total_estimado," in result["resumen"]

    async def test_unknown_device_raises(self, hass, mock_add_on):
        entry = MockConfigEntry(domain=DOMAIN, data={"host": "localhost", "port": 8099}, options={})
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN, "resumen_mensual", {"device_id": "no-existe"}, blocking=True, return_response=True
            )


class TestRellenarHistoricoService:
    async def test_fills_all_supply_points_by_default(self, hass, mock_add_on, monkeypatch):
        entry = MockConfigEntry(domain=DOMAIN, data={"host": "localhost", "port": 8099}, options={})
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hass.data[DOMAIN][entry.entry_id]

        async def fake_get_consumption(cont_id, range_type=None, date=None):
            return {"totalImportedKwh": 1.0, "totalExportedKwh": 0.0}

        coordinator.client.async_get_consumption = fake_get_consumption

        backfilled_cups = []

        async def fake_backfill(hass_arg, cups, month_data):
            backfilled_cups.append(cups)

        monkeypatch.setattr("custom_components.edistribucion.async_backfill_energy_statistics", fake_backfill)

        result = await hass.services.async_call(
            DOMAIN, "rellenar_historico", {"meses": 3}, blocking=True, return_response=True
        )

        assert result == {"suministros": 1, "meses_rellenados": 3}
        assert backfilled_cups == ["ES0031500160526001DS0F"] * 3

    async def test_fills_only_the_given_device_when_device_id_provided(self, hass, mock_add_on, monkeypatch):
        entry = MockConfigEntry(domain=DOMAIN, data={"host": "localhost", "port": 8099}, options={})
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hass.data[DOMAIN][entry.entry_id]

        async def fake_get_consumption(cont_id, range_type=None, date=None):
            return {"totalImportedKwh": 1.0, "totalExportedKwh": 0.0}

        coordinator.client.async_get_consumption = fake_get_consumption

        async def fake_backfill(hass_arg, cups, month_data):
            pass

        monkeypatch.setattr("custom_components.edistribucion.async_backfill_energy_statistics", fake_backfill)

        device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "contA")})
        result = await hass.services.async_call(
            DOMAIN, "rellenar_historico", {"device_id": device.id, "meses": 2}, blocking=True, return_response=True
        )
        assert result == {"suministros": 1, "meses_rellenados": 2}

    async def test_unknown_device_raises(self, hass, mock_add_on):
        entry = MockConfigEntry(domain=DOMAIN, data={"host": "localhost", "port": 8099}, options={})
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN, "rellenar_historico", {"device_id": "no-existe"}, blocking=True, return_response=True
            )

    async def test_api_error_for_one_month_does_not_stop_the_rest(self, hass, mock_add_on, monkeypatch):
        """Un mes sin datos (contrato más nuevo que ese mes, o fallo puntual) no debe frenar el
        relleno del resto de meses pedidos."""
        from custom_components.edistribucion.api import EdistribucionApiError

        entry = MockConfigEntry(domain=DOMAIN, data={"host": "localhost", "port": 8099}, options={})
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hass.data[DOMAIN][entry.entry_id]
        calls = {"n": 0}

        async def flaky_get_consumption(cont_id, range_type=None, date=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise EdistribucionApiError("sin datos ese mes")
            return {"totalImportedKwh": 1.0, "totalExportedKwh": 0.0}

        async def fake_backfill(hass_arg, cups, month_data):
            pass

        coordinator.client.async_get_consumption = flaky_get_consumption
        monkeypatch.setattr("custom_components.edistribucion.async_backfill_energy_statistics", fake_backfill)

        result = await hass.services.async_call(
            DOMAIN, "rellenar_historico", {"meses": 3}, blocking=True, return_response=True
        )
        assert result == {"suministros": 1, "meses_rellenados": 2}  # 3 pedidos, 1 falló, 2 rellenados
