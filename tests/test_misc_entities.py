"""Tests de los módulos más pequeños: device.py, diagnostics.py, calendar.py (función pura de
eventos), binary_sensor.py y button.py (incluido el botón condicional de PVPC). Necesita objetos
reales de Home Assistant — se verifica vía CI, no en el sandbox de desarrollo local (sin pip)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.edistribucion.const import CONF_SUPPLY_POINTS, DOMAIN
from custom_components.edistribucion.device import hub_device_info

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
    aioclient_mock.get("http://localhost:8099/max-power-demand/cupsA", json={"maxValue": 3.5, "points": []})
    aioclient_mock.get(
        "http://localhost:8099/contracted-power/contA",
        json={"contractedPowerPuntaKw": 3.5, "contractedPowerValleKw": 3.5},
    )
    return aioclient_mock


def test_hub_device_info_identifiers_and_name():
    info = hub_device_info("entry123")
    assert info["identifiers"] == {(DOMAIN, "entry123")}
    assert info["name"] == "e-distribución (add-on)"


class TestDiagnostics:
    async def test_redacts_address_but_keeps_rest(self, hass, mock_add_on):
        from custom_components.edistribucion.diagnostics import async_get_config_entry_diagnostics

        entry = MockConfigEntry(domain=DOMAIN, data={"host": "localhost", "port": 8099}, options={})
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["entry"] == {"host": "localhost", "port": 8099}
        supply = result["supply_points"]["contA"]
        assert "address" not in supply["supply_point"]
        assert supply["supply_point"]["cups"] == "ES0031500160526001DS0F"
        assert result["last_update_success"] is True


class TestCalendarDayToEvent:
    def test_basic_event_summary(self):
        from custom_components.edistribucion.calendar import _day_to_event

        event = _day_to_event({"date": "30/07/2026", "importedKwh": 5.2, "exportedKwh": 0})
        assert event.start == date(2026, 7, 30)
        assert event.end == date(2026, 7, 31)
        assert "5.20 kWh importados" in event.summary
        assert "exportados" not in event.summary

    def test_includes_exported_when_present(self):
        from custom_components.edistribucion.calendar import _day_to_event

        event = _day_to_event({"date": "30/07/2026", "importedKwh": 1.0, "exportedKwh": 2.5})
        assert "2.50 kWh exportados" in event.summary

    def test_malformed_date_returns_none(self):
        from custom_components.edistribucion.calendar import _day_to_event

        assert _day_to_event({"date": "no-es-fecha", "importedKwh": 1.0}) is None

    def test_missing_date_key_returns_none(self):
        from custom_components.edistribucion.calendar import _day_to_event

        assert _day_to_event({"importedKwh": 1.0}) is None


class TestBinarySensor:
    async def test_reflects_coordinator_success(self, hass, mock_add_on):
        from homeassistant.helpers import entity_registry as er

        entry = MockConfigEntry(domain=DOMAIN, data={"host": "localhost", "port": 8099}, options={})
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        reg = er.async_get(hass)
        entity_id = reg.async_get_entity_id("binary_sensor", DOMAIN, f"{entry.entry_id}_connected")
        assert entity_id is not None
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state == "on"


class TestButtons:
    async def test_pvpc_refresh_button_absent_without_pvpc_tariff(self, hass, mock_add_on):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={"host": "localhost", "port": 8099},
            options={CONF_SUPPLY_POINTS: {"contA": {"tariff_type": "fija", "fixed_price": 0.2}}},
        )
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        from homeassistant.helpers import entity_registry as er

        reg = er.async_get(hass)
        entity_id = reg.async_get_entity_id("button", DOMAIN, f"{entry.entry_id}_refresh_pvpc")
        assert entity_id is None

    async def test_pvpc_refresh_button_present_with_pvpc_tariff(self, hass, mock_add_on):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={"host": "localhost", "port": 8099},
            options={CONF_SUPPLY_POINTS: {"contA": {"tariff_type": "pvpc"}}},
        )
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        from homeassistant.helpers import entity_registry as er

        reg = er.async_get(hass)
        entity_id = reg.async_get_entity_id("button", DOMAIN, f"{entry.entry_id}_refresh_pvpc")
        assert entity_id is not None

    async def test_pressing_refresh_pvpc_button_calls_coordinator(self, hass, mock_add_on):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={"host": "localhost", "port": 8099},
            options={CONF_SUPPLY_POINTS: {"contA": {"tariff_type": "pvpc"}}},
        )
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hass.data[DOMAIN][entry.entry_id]
        called = {"n": 0}

        async def fake_force_refresh():
            called["n"] += 1

        coordinator.async_force_refresh_pvpc_prices = fake_force_refresh

        from homeassistant.helpers import entity_registry as er

        reg = er.async_get(hass)
        entity_id = reg.async_get_entity_id("button", DOMAIN, f"{entry.entry_id}_refresh_pvpc")
        await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)

        assert called["n"] == 1
