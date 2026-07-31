"""Tests de config_flow.py — flujo de configuración inicial (host/puerto, zeroconf) y el asistente
de opciones multi-paso (uno por CUPS). Necesita objetos reales de Home Assistant
(pytest-homeassistant-custom-component) — no se ejecuta en el sandbox de desarrollo local (sin
pip), se verifica vía CI. Ver requirements_test.txt."""

from __future__ import annotations

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
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
    },
    {
        "cups": "ES0031500115281004WC0F",
        "cupsId": "cupsB",
        "contId": "contB",
        "address": "CL HOLANDA 18",
        "tariff": "2.0TD",
        "active": False,
        "startDate": "2020-01-01",
        "endDate": "2022-01-01",
    },
]


@pytest.fixture
def mock_add_on(aioclient_mock):
    aioclient_mock.get("http://localhost:8099/info", json={"name": "e-distribución"})
    aioclient_mock.get("http://localhost:8099/supply-points", json=_SUPPLY_POINTS)
    return aioclient_mock


async def test_user_flow_success(hass, mock_add_on):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "localhost", "port": 8099}
    )
    assert result2["type"] == FlowResultType.CREATE_ENTRY
    assert result2["data"] == {"host": "localhost", "port": 8099}


async def test_user_flow_cannot_connect_shows_error(hass, aioclient_mock):
    aioclient_mock.get("http://localhost:8099/info", exc=Exception("boom"))
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "localhost", "port": 8099}
    )
    assert result2["type"] == FlowResultType.FORM
    assert result2["errors"] == {"base": "cannot_connect"}


async def test_user_flow_duplicate_host_port_aborts(hass, mock_add_on):
    MockConfigEntry(domain=DOMAIN, unique_id="localhost:8099", data={"host": "localhost", "port": 8099}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "localhost", "port": 8099}
    )
    assert result2["type"] == FlowResultType.ABORT
    assert result2["reason"] == "already_configured"


async def test_zeroconf_discovery_confirms_and_creates_entry(hass, mock_add_on):
    discovery_info = ZeroconfServiceInfo(
        ip_address="127.0.0.1",
        ip_addresses=["127.0.0.1"],
        hostname="localhost.local.",
        name="e-distribucion._edistribucion._tcp.local.",
        port=8099,
        type="_edistribucion._tcp.local.",
        properties={},
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=discovery_info
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"

    result2 = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result2["type"] == FlowResultType.CREATE_ENTRY
    assert result2["data"]["port"] == 8099


async def test_zeroconf_discovery_cannot_connect_aborts(hass, aioclient_mock):
    aioclient_mock.get("http://127.0.0.1:8099/info", exc=Exception("boom"))
    discovery_info = ZeroconfServiceInfo(
        ip_address="127.0.0.1",
        ip_addresses=["127.0.0.1"],
        hostname="localhost.local.",
        name="e-distribucion._edistribucion._tcp.local.",
        port=8099,
        type="_edistribucion._tcp.local.",
        properties={},
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=discovery_info
    )
    result2 = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result2["type"] == FlowResultType.ABORT
    assert result2["reason"] == "cannot_connect"


@pytest.fixture
def config_entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "localhost", "port": 8099}, options={})
    entry.add_to_hass(hass)
    return entry


class TestOptionsFlow:
    async def test_full_walkthrough_two_supply_points(self, hass, mock_add_on, config_entry):
        result = await hass.config_entries.options.async_init(config_entry.entry_id)
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "init"
        assert result["description_placeholders"]["num_supplies"] == "2"

        result = await hass.config_entries.options.async_configure(result["flow_id"], {"scan_interval": 20})
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "supply_point"
        assert result["description_placeholders"]["cups"] == "ES0031500160526001DS0F"
        assert result["description_placeholders"]["position"] == "1"
        assert result["description_placeholders"]["total"] == "2"

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "track": True,
                "alias": "Casa",
                "tariff_type": "tramos",
                "price_power_punta": 0.08,
                "price_power_valle": 0.03,
                "fixed_price": 0,
                "price_punta": 0.25,
                "price_llano": 0.18,
                "price_valle": 0.10,
                "holiday_region": "none",
                "surplus_compensation": False,
                "surplus_price": 0,
                "pvpc_zone": "PCB",
            },
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "supply_point"
        assert result["description_placeholders"]["position"] == "2"
        assert result["description_placeholders"]["estado"] in ("histórico", "historical")

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "track": False,
                "alias": "",
                "tariff_type": "fija",
                "price_power_punta": 0,
                "price_power_valle": 0,
                "fixed_price": 0.20,
                "price_punta": 0,
                "price_llano": 0,
                "price_valle": 0,
                "holiday_region": "none",
                "surplus_compensation": False,
                "surplus_price": 0,
                "pvpc_zone": "PCB",
            },
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        data = result["data"]
        assert data["scan_interval"] == 20
        supply_points = data[CONF_SUPPLY_POINTS]
        assert supply_points["contA"]["alias"] == "Casa"
        assert supply_points["contA"]["price_punta"] == 0.25
        assert supply_points["contB"]["track"] is False
        assert supply_points["contB"]["fixed_price"] == 0.20

    async def test_no_supply_points_finishes_immediately(self, hass, config_entry, aioclient_mock):
        aioclient_mock.get("http://localhost:8099/supply-points", json=[])
        result = await hass.config_entries.options.async_init(config_entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"scan_interval": 15})
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_SUPPLY_POINTS] == {}

    async def test_existing_options_prefill_defaults(self, hass, mock_add_on):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={"host": "localhost", "port": 8099},
            options={CONF_SUPPLY_POINTS: {"contA": {"alias": "Mi alias guardado", "tariff_type": "pvpc"}}},
        )
        entry.add_to_hass(hass)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"scan_interval": 15})
        assert result["step_id"] == "supply_point"

        # El formulario del primer CUPS debe precargar lo ya guardado, no los valores por defecto.
        schema_dict = result["data_schema"].schema
        alias_marker = next(k for k in schema_dict if str(k) == "alias")
        tariff_marker = next(k for k in schema_dict if str(k) == "tariff_type")
        assert alias_marker.default() == "Mi alias guardado"
        assert tariff_marker.default() == "pvpc"
