"""Tests de diagnostics.py — qué se incluye (y qué se redacta) en el volcado descargable desde
la UI (Ajustes → Dispositivos y servicios → e-distribución → Descargar diagnósticos)."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.edistribucion.const import DOMAIN
from custom_components.edistribucion.diagnostics import async_get_config_entry_diagnostics

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


class FakeCoordinator:
    def __init__(self, data, year_to_date=None, year_to_date_details=None):
        self.data = data
        self.last_update_success = True
        self.last_success_time = None
        self._year_to_date = year_to_date or {}
        self._year_to_date_details = year_to_date_details or {}

    def year_to_date_completed_months(self, cont_id):
        return self._year_to_date.get(
            cont_id,
            {"imported_kwh": 0.0, "exported_kwh": 0.0, "cost": 0.0, "power_cost": 0.0, "surplus_compensation": 0.0},
        )

    def year_to_date_month_details(self, cont_id):
        return self._year_to_date_details.get(cont_id, {})


async def test_redacts_address_but_keeps_rest_of_supply_point(hass):
    bundle = {
        "supply_point": {"cups": "ES123", "address": "Calle Falsa 123", "tariff": "tramos"},
        "consumption": {"dailyTotals": []},
        "week": None,
        "month": None,
        "contract": {"contractCode": "XYZ"},
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "localhost", "port": 8099}, options={})
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = FakeCoordinator({"contA": bundle})

    result = await async_get_config_entry_diagnostics(hass, entry)

    sp = result["supply_points"]["contA"]["supply_point"]
    assert "address" not in sp
    assert sp["cups"] == "ES123"
    assert sp["tariff"] == "tramos"


async def test_exposes_year_to_date_caches(hass):
    """Issue #29: los cachés de acumulado del año del coordinator deben verse en diagnósticos, sin
    tener que activar debug logging ni forzar un reload a mano (ver issue #25)."""
    bundle = {
        "supply_point": {"cups": "ES123"},
        "consumption": {"dailyTotals": []},
        "week": None,
        "month": None,
        "contract": None,
    }
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "localhost", "port": 8099}, options={})
    entry.add_to_hass(hass)
    year_to_date = {"contA": {"imported_kwh": 120.5, "exported_kwh": 30.0, "cost": 25.4, "power_cost": 10.0, "surplus_compensation": 1.5}}
    year_to_date_details = {"contA": {1: {"imported_kwh": 0.0, "exported_kwh": 0.0}, 8: {"imported_kwh": 120.5, "exported_kwh": 30.0}}}
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = FakeCoordinator({"contA": bundle}, year_to_date, year_to_date_details)

    result = await async_get_config_entry_diagnostics(hass, entry)

    supply = result["supply_points"]["contA"]
    assert supply["year_to_date_completed_months"] == year_to_date["contA"]
    assert supply["year_to_date_month_details"] == year_to_date_details["contA"]


async def test_year_to_date_caches_default_when_missing(hass):
    """Sin nada cacheado todavía (integración recién arrancada), no debe romper con KeyError."""
    bundle = {"supply_point": {"cups": "ES123"}, "consumption": None, "week": None, "month": None, "contract": None}
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "localhost", "port": 8099}, options={})
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = FakeCoordinator({"contA": bundle})

    result = await async_get_config_entry_diagnostics(hass, entry)

    supply = result["supply_points"]["contA"]
    assert supply["year_to_date_completed_months"]["imported_kwh"] == 0.0
    assert supply["year_to_date_month_details"] == {}
