"""Tests de sensor.py — funciones auxiliares puras, y qué sensores se crean según cómo esté
configurado cada CUPS (coste solo si hay precio, simulador solo si hay datos para comparar,
autosuficiencia solo si ha exportado algo...). Necesita objetos reales de Home Assistant — se
verifica vía CI, no en el sandbox de desarrollo local (sin pip)."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.edistribucion.const import CONF_SUPPLY_POINTS, DOMAIN
from custom_components.edistribucion.sensor import (
    _energy_cost_configured,
    _latest_day_hourly,
    _latest_daily_total,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


class TestLatestDailyTotal:
    def test_none_consumption(self):
        assert _latest_daily_total(None) is None

    def test_no_daily_totals(self):
        assert _latest_daily_total({}) is None

    def test_picks_most_recent_date(self):
        consumption = {
            "dailyTotals": [
                {"date": "01/07/2026", "importedKwh": 1.0},
                {"date": "15/07/2026", "importedKwh": 3.0},
                {"date": "10/07/2026", "importedKwh": 2.0},
            ]
        }
        result = _latest_daily_total(consumption)
        assert result["date"] == "15/07/2026"


class TestLatestDayHourly:
    def test_none_consumption(self):
        assert _latest_day_hourly(None) is None

    def test_no_hourly_data(self):
        assert _latest_day_hourly({}) is None

    def test_trims_to_most_recent_day_only(self):
        consumption = {
            "hourlyByDate": {
                "01/07/2026": [{"hour": "0 - 1 h", "importedKwh": 1.0}],
                "15/07/2026": [{"hour": "0 - 1 h", "importedKwh": 3.0}],
            }
        }
        result = _latest_day_hourly(consumption)
        assert set(result["hourlyByDate"]) == {"15/07/2026"}


class TestEnergyCostConfigured:
    def test_fija_with_price(self):
        assert _energy_cost_configured({"tariff_type": "fija", "fixed_price": 0.2}) is True

    def test_fija_without_price(self):
        assert _energy_cost_configured({"tariff_type": "fija", "fixed_price": 0}) is False

    def test_pvpc_always_true(self):
        assert _energy_cost_configured({"tariff_type": "pvpc"}) is True

    def test_tramos_with_any_price(self):
        assert _energy_cost_configured({"tariff_type": "tramos", "price_valle": 0.1}) is True

    def test_tramos_without_any_price(self):
        assert _energy_cost_configured({"tariff_type": "tramos"}) is False


class FakeCoordinator:
    """Coordinador mínimo — evita tener que montar el add-on real solo para probar qué entidades
    se registran según los datos/opciones de cada CUPS."""

    def __init__(self, data, entry_id="entry1"):
        self.data = data
        self.entry_id = entry_id
        self.pvpc_prices: dict[str, dict[str, float]] = {}
        self.last_success_time = None
        self._year_to_date: dict[str, dict[str, float]] = {}
        self._last_value_change: dict[str, dict[str, object]] = {}
        from datetime import timedelta

        self.update_interval = timedelta(minutes=15)

    def async_add_listener(self, *args, **kwargs):
        return lambda: None

    def year_to_date_completed_months(self, cont_id):
        return self._year_to_date.get(cont_id, {"imported_kwh": 0.0, "exported_kwh": 0.0, "cost": 0.0})

    def last_value_change(self, cont_id, flow):
        return self._last_value_change.get(cont_id, {}).get(flow)


def _bundle(sp_overrides=None, has_export=False):
    # contracted_power_*_kw se pone en `sp` porque el coordinador real lo copia ahí desde
    # bundle["contract"] (ver coordinator._async_update_data) — sin esto, power_cost() siempre
    # daría 0 pase lo que pase con price_power_punta/valle.
    sp = {
        "cups": "ES123",
        "cupsId": "cupsA",
        "contId": "contA",
        "tariff_type": "tramos",
        "contracted_power_punta_kw": 3.5,
        "contracted_power_valle_kw": 3.5,
    }
    sp.update(sp_overrides or {})
    month = {"totalImportedKwh": 10.0, "totalExportedKwh": 3.0 if has_export else 0.0, "hourlyByDate": {}}
    return {
        "supply_point": sp,
        "consumption": {"dailyTotals": [], "hourlyByDate": {}},
        "week": None,
        "month": month,
        "month_last_year": None,
        "contract": {"contractedPowerPuntaKw": 3.5, "contractedPowerValleKw": 3.5},
    }


async def _setup_with_fake_coordinator(hass, bundles: dict):
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "localhost", "port": 8099}, options={})
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = FakeCoordinator(bundles, entry.entry_id)
    from homeassistant.helpers import entity_platform

    from custom_components.edistribucion import sensor as sensor_module

    added = []

    def async_add_entities(new_entities, update_before_add=False):
        # AddEntitiesCallback es una función síncrona en HA real (sensor.py la llama sin await) —
        # si aquí fuera `async def`, la llamada sin await solo crearía una corrutina sin ejecutar,
        # y `added` se quedaría vacío en silencio.
        added.extend(new_entities)

    await sensor_module.async_setup_entry(hass, entry, async_add_entities)
    return added


def _unique_ids(entities):
    return {e._attr_unique_id for e in entities if hasattr(e, "_attr_unique_id")}


async def test_no_cost_sensors_without_price_configured(hass):
    bundles = {"contA": _bundle({"tariff_type": "tramos"})}  # sin ningún precio de tramos puesto
    entities = await _setup_with_fake_coordinator(hass, bundles)
    ids = _unique_ids(entities)
    assert "contA_estimated_cost_today" not in ids
    assert "contA_average_price_month" not in ids
    assert "contA_year_to_date_cost" not in ids


async def test_cost_sensors_created_when_price_configured(hass):
    bundles = {"contA": _bundle({"tariff_type": "tramos", "price_punta": 0.25})}
    entities = await _setup_with_fake_coordinator(hass, bundles)
    ids = _unique_ids(entities)
    assert "contA_estimated_cost_today" in ids
    assert "contA_estimated_cost_month" in ids
    assert "contA_average_price_month" in ids
    assert "contA_year_to_date_cost" in ids


async def test_no_tramo_sensors_without_price_configured(hass):
    bundles = {"contA": _bundle({"tariff_type": "tramos"})}  # sin ningún precio de tramos puesto
    entities = await _setup_with_fake_coordinator(hass, bundles)
    ids = _unique_ids(entities)
    assert "contA_punta_kwh_today" not in ids
    assert "contA_punta_cost_month" not in ids


async def test_tramo_sensors_only_created_for_tramos_tariff(hass):
    bundles_tramos = {"contA": _bundle({"tariff_type": "tramos", "price_punta": 0.25})}
    entities_tramos = await _setup_with_fake_coordinator(hass, bundles_tramos)
    ids_tramos = _unique_ids(entities_tramos)
    for tramo in ("punta", "llano", "valle"):
        for kind in ("kwh", "cost"):
            for period in ("today", "month"):
                assert f"contA_{tramo}_{kind}_{period}" in ids_tramos

    hass.data[DOMAIN].clear()
    bundles_fija = {"contA": _bundle({"tariff_type": "fija", "fixed_price": 0.2})}
    entities_fija = await _setup_with_fake_coordinator(hass, bundles_fija)
    assert "contA_punta_kwh_today" not in _unique_ids(entities_fija)

    hass.data[DOMAIN].clear()
    bundles_pvpc = {"contA": _bundle({"tariff_type": "pvpc"})}
    entities_pvpc = await _setup_with_fake_coordinator(hass, bundles_pvpc)
    assert "contA_punta_kwh_today" not in _unique_ids(entities_pvpc)


async def test_tramo_sensor_values_match_cost_breakdown(hass):
    """27/07/2026 es lunes (ver TestHourPeriod en test_costs.py): 10-11h -> punta, 8-9h -> llano,
    0-1h -> valle. Mismo hourlyByDate en "hoy" y en "mes" para poder comparar ambos periodos."""
    bundle = _bundle({"tariff_type": "tramos", "price_punta": 0.25, "price_llano": 0.15, "price_valle": 0.05})
    hourly = {
        "27/07/2026": [
            {"hour": "10 - 11 h", "importedKwh": 2.0},
            {"hour": "8 - 9 h", "importedKwh": 3.0},
            {"hour": "0 - 1 h", "importedKwh": 1.0},
        ]
    }
    bundle["consumption"] = {"dailyTotals": [], "hourlyByDate": hourly}
    bundle["month"] = {"totalImportedKwh": 6.0, "totalExportedKwh": 0.0, "hourlyByDate": hourly}
    entities = await _setup_with_fake_coordinator(hass, {"contA": bundle})
    by_id = {e._attr_unique_id: e for e in entities if hasattr(e, "_attr_unique_id")}

    assert by_id["contA_punta_kwh_today"].native_value == 2.0
    assert by_id["contA_punta_cost_today"].native_value == 0.5
    assert by_id["contA_llano_kwh_month"].native_value == 3.0
    assert by_id["contA_llano_cost_month"].native_value == 0.45
    assert by_id["contA_valle_kwh_today"].native_value == 1.0
    assert by_id["contA_valle_cost_today"].native_value == 0.05


async def test_tramo_cost_sensor_applies_configured_iva(hass):
    """Mismo escenario que test_tramo_sensor_values_match_cost_breakdown, pero con iva_percent=21 —
    solo coste_punta/llano/valle deben verse afectados, no los kWh."""
    bundle = _bundle({"tariff_type": "tramos", "price_punta": 0.25, "price_llano": 0.15, "price_valle": 0.05, "iva_percent": 21})
    hourly = {"27/07/2026": [{"hour": "10 - 11 h", "importedKwh": 2.0}]}
    bundle["consumption"] = {"dailyTotals": [], "hourlyByDate": hourly}
    entities = await _setup_with_fake_coordinator(hass, {"contA": bundle})
    by_id = {e._attr_unique_id: e for e in entities if hasattr(e, "_attr_unique_id")}

    assert by_id["contA_punta_kwh_today"].native_value == 2.0
    assert by_id["contA_punta_cost_today"].native_value == pytest.approx(0.5 * 1.21)


async def test_current_pvpc_price_sensor_only_for_pvpc_tariff(hass):
    bundles_pvpc = {"contA": _bundle({"tariff_type": "pvpc"})}
    bundles_fija = {"contA": _bundle({"tariff_type": "fija", "fixed_price": 0.2})}

    entities_pvpc = await _setup_with_fake_coordinator(hass, bundles_pvpc)
    assert "contA_current_pvpc_price" in _unique_ids(entities_pvpc)

    hass.data[DOMAIN].clear()
    entities_fija = await _setup_with_fake_coordinator(hass, bundles_fija)
    assert "contA_current_pvpc_price" not in _unique_ids(entities_fija)


async def test_current_pvpc_price_sensor_applies_configured_iva(hass, monkeypatch):
    from datetime import datetime, timezone

    from custom_components.edistribucion.esios import DEFAULT_PVPC_ZONE

    monkeypatch.setattr(
        "custom_components.edistribucion.sensor.dt_util.now",
        lambda: datetime(2026, 7, 27, 10, 30, tzinfo=timezone.utc),
    )
    bundle = _bundle({"tariff_type": "pvpc", "iva_percent": 21})
    entities = await _setup_with_fake_coordinator(hass, {"contA": bundle})
    coordinator = next(iter(hass.data[DOMAIN].values()))
    coordinator.pvpc_prices = {DEFAULT_PVPC_ZONE: {"27/07/2026 10": 0.10}}

    by_id = {e._attr_unique_id: e for e in entities if hasattr(e, "_attr_unique_id")}
    sensor = by_id["contA_current_pvpc_price"]
    assert sensor.native_value == pytest.approx(0.121)
    assert sensor.extra_state_attributes["precio_sin_iva"] == 0.10
    assert sensor.extra_state_attributes["precios_hoy"]["10h"] == pytest.approx(0.121)


async def test_simulator_sensors_skip_active_tariff_and_require_data(hass):
    """Con tarifa fija activa: se simula pvpc (siempre posible) y tramos (si hay algún precio de
    tramos puesto), pero NUNCA se duplica un simulador de "fija" (ya es la tarifa activa)."""
    bundles = {"contA": _bundle({"tariff_type": "fija", "fixed_price": 0.2, "price_punta": 0.3})}
    entities = await _setup_with_fake_coordinator(hass, bundles)
    ids = _unique_ids(entities)
    assert "contA_simulated_cost_fija_month" not in ids
    assert "contA_simulated_cost_pvpc_month" in ids
    assert "contA_simulated_cost_tramos_month" in ids


async def test_simulator_tramos_skipped_without_any_tramos_price(hass):
    bundles = {"contA": _bundle({"tariff_type": "fija", "fixed_price": 0.2})}  # sin precios de tramos
    entities = await _setup_with_fake_coordinator(hass, bundles)
    ids = _unique_ids(entities)
    assert "contA_simulated_cost_tramos_month" not in ids
    assert "contA_simulated_cost_pvpc_month" in ids  # pvpc siempre se puede simular


async def test_self_consumption_sensors_only_if_exported_something(hass):
    bundles_with_export = {"contA": _bundle(has_export=True)}
    bundles_without_export = {"contA": _bundle(has_export=False)}

    entities_with = await _setup_with_fake_coordinator(hass, bundles_with_export)
    assert "contA_self_consumption_month" in _unique_ids(entities_with)

    hass.data[DOMAIN].clear()
    entities_without = await _setup_with_fake_coordinator(hass, bundles_without_export)
    assert "contA_self_consumption_month" not in _unique_ids(entities_without)


async def test_power_cost_sensors_only_if_power_term_configured(hass):
    bundles_with_price = {"contA": _bundle({"price_power_punta": 0.08})}
    bundles_without_price = {"contA": _bundle()}

    entities_with = await _setup_with_fake_coordinator(hass, bundles_with_price)
    assert "contA_power_cost_today" in _unique_ids(entities_with)

    hass.data[DOMAIN].clear()
    entities_without = await _setup_with_fake_coordinator(hass, bundles_without_price)
    assert "contA_power_cost_today" not in _unique_ids(entities_without)


async def test_power_cost_sensor_applies_configured_iva(hass):
    bundle = _bundle({"price_power_punta": 0.08, "price_power_valle": 0.02, "iva_percent": 21})
    entities = await _setup_with_fake_coordinator(hass, {"contA": bundle})
    by_id = {e._attr_unique_id: e for e in entities if hasattr(e, "_attr_unique_id")}
    sensor = by_id["contA_power_cost_today"]
    # 3.5*0.08 + 3.5*0.02 = 0.35 sin IVA (ver contracted_power_*_kw en _bundle) -> 0.4235 con 21%.
    assert sensor.native_value == pytest.approx(0.4235)
    assert sensor.extra_state_attributes["iva_percent"] == 21


async def test_cost_with_power_sensors_require_both_energy_price_and_power_term(hass):
    """Solo tiene sentido "coste + potencia" si hay AMBAS cosas configuradas — con solo una de las
    dos, el sensor de coste normal o el de potencia ya cubren ese caso por separado."""
    bundles_both = {"contA": _bundle({"price_punta": 0.25, "price_power_punta": 0.08})}
    entities_both = await _setup_with_fake_coordinator(hass, bundles_both)
    ids_both = _unique_ids(entities_both)
    assert "contA_estimated_cost_today_with_power" in ids_both
    assert "contA_estimated_cost_month_with_power" in ids_both

    hass.data[DOMAIN].clear()
    bundles_only_power = {"contA": _bundle({"price_power_punta": 0.08})}  # sin precio de energía
    entities_only_power = await _setup_with_fake_coordinator(hass, bundles_only_power)
    assert "contA_estimated_cost_today_with_power" not in _unique_ids(entities_only_power)

    hass.data[DOMAIN].clear()
    bundles_only_energy = {"contA": _bundle({"price_punta": 0.25})}  # sin término de potencia
    entities_only_energy = await _setup_with_fake_coordinator(hass, bundles_only_energy)
    assert "contA_estimated_cost_today_with_power" not in _unique_ids(entities_only_energy)


async def test_cost_with_power_month_value_sums_energy_cost_and_power_term(hass):
    """Mismo escenario que test_tramo_sensor_values_match_cost_breakdown: mes con 6 kWh repartidos
    en punta/llano/valle -> coste de energía 0.5+0.45+0.05=1.0 EUR. Potencia: 3.5+3.5 kW * 0.08+0.02
    EUR/kW/día * 0 días facturados (dailyTotals vacío en el bundle de test) -> 0 EUR de potencia, así
    que el total con potencia debe coincidir con el coste de energía solo."""
    bundle = _bundle(
        {
            "price_punta": 0.25,
            "price_llano": 0.15,
            "price_valle": 0.05,
            "price_power_punta": 0.08,
            "price_power_valle": 0.02,
        }
    )
    hourly = {
        "27/07/2026": [
            {"hour": "10 - 11 h", "importedKwh": 2.0},
            {"hour": "8 - 9 h", "importedKwh": 3.0},
            {"hour": "0 - 1 h", "importedKwh": 1.0},
        ]
    }
    bundle["month"] = {"totalImportedKwh": 6.0, "totalExportedKwh": 0.0, "hourlyByDate": hourly, "dailyTotals": [{"date": "27/07/2026"}]}
    entities = await _setup_with_fake_coordinator(hass, {"contA": bundle})
    by_id = {e._attr_unique_id: e for e in entities if hasattr(e, "_attr_unique_id")}

    month_sensor = by_id["contA_estimated_cost_month_with_power"]
    # 1 día facturado * (3.5*0.08 + 3.5*0.02) = 0.35 EUR de potencia, + 1.0 EUR de energía.
    assert month_sensor.native_value == 1.35
    assert month_sensor.extra_state_attributes["coste_energia"] == 1.0
    assert month_sensor.extra_state_attributes["termino_potencia"] == 0.35


async def test_surplus_compensation_sensors_only_if_enabled_with_price(hass):
    bundles_enabled = {"contA": _bundle({"surplus_compensation": True, "surplus_price": 0.05})}
    bundles_disabled = {"contA": _bundle({"surplus_compensation": False})}

    entities_enabled = await _setup_with_fake_coordinator(hass, bundles_enabled)
    ids_enabled = _unique_ids(entities_enabled)
    assert "contA_surplus_compensation_today" in ids_enabled
    assert "contA_surplus_compensation_week" in ids_enabled
    assert "contA_surplus_compensation_month" in ids_enabled

    hass.data[DOMAIN].clear()
    entities_disabled = await _setup_with_fake_coordinator(hass, bundles_disabled)
    ids_disabled = _unique_ids(entities_disabled)
    assert "contA_surplus_compensation_today" not in ids_disabled
    assert "contA_surplus_compensation_week" not in ids_disabled


async def test_surplus_compensation_week_value_is_exported_kwh_times_price(hass):
    bundle = _bundle({"surplus_compensation": True, "surplus_price": 0.05})
    bundle["week"] = {"totalExportedKwh": 12.0}
    entities = await _setup_with_fake_coordinator(hass, {"contA": bundle})
    by_id = {e._attr_unique_id: e for e in entities if hasattr(e, "_attr_unique_id")}
    assert by_id["contA_surplus_compensation_week"].native_value == 0.6


async def test_surplus_compensation_week_none_without_week_data(hass):
    bundle = _bundle({"surplus_compensation": True, "surplus_price": 0.05})  # bundle["week"] es None
    entities = await _setup_with_fake_coordinator(hass, {"contA": bundle})
    by_id = {e._attr_unique_id: e for e in entities if hasattr(e, "_attr_unique_id")}
    assert by_id["contA_surplus_compensation_week"].native_value is None


async def test_no_export_tramo_sensors_without_surplus_compensation(hass):
    bundles = {"contA": _bundle({"surplus_compensation": False})}
    entities = await _setup_with_fake_coordinator(hass, bundles)
    ids = _unique_ids(entities)
    assert "contA_punta_exported_kwh_today" not in ids
    assert "contA_punta_exported_compensation_month" not in ids


async def test_export_tramo_sensors_created_with_surplus_compensation(hass):
    bundles = {"contA": _bundle({"surplus_compensation": True, "surplus_price": 0.06})}
    entities = await _setup_with_fake_coordinator(hass, bundles)
    ids = _unique_ids(entities)
    for tramo in ("punta", "llano", "valle"):
        for kind in ("kwh", "compensation"):
            for period in ("today", "month"):
                assert f"contA_{tramo}_exported_{kind}_{period}" in ids


async def test_export_tramo_sensor_values_use_flat_price(hass):
    """El precio de compensación de excedentes es PLANO (tarifa "niba Solar"): mismo €/kWh en los
    tres tramos, a diferencia del consumo importado. 27/07/2026 es lunes (ver TestHourPeriod en
    test_costs.py): 10-11h -> punta, 8-9h -> llano, 0-1h -> valle."""
    bundle = _bundle({"surplus_compensation": True, "surplus_price": 0.06})
    hourly = {
        "27/07/2026": [
            {"hour": "10 - 11 h", "importedKwh": 0.0, "exportedKwh": 2.0},
            {"hour": "8 - 9 h", "importedKwh": 0.0, "exportedKwh": 3.0},
            {"hour": "0 - 1 h", "importedKwh": 0.0, "exportedKwh": 1.0},
        ]
    }
    bundle["consumption"] = {"dailyTotals": [], "hourlyByDate": hourly}
    bundle["month"] = {"totalImportedKwh": 0.0, "totalExportedKwh": 6.0, "hourlyByDate": hourly}
    entities = await _setup_with_fake_coordinator(hass, {"contA": bundle})
    by_id = {e._attr_unique_id: e for e in entities if hasattr(e, "_attr_unique_id")}

    assert by_id["contA_punta_exported_kwh_today"].native_value == 2.0
    assert by_id["contA_punta_exported_compensation_today"].native_value == 0.12
    assert by_id["contA_llano_exported_kwh_month"].native_value == 3.0
    assert by_id["contA_llano_exported_compensation_month"].native_value == 0.18
    assert by_id["contA_valle_exported_kwh_today"].native_value == 1.0
    assert by_id["contA_valle_exported_compensation_today"].native_value == 0.06


async def test_estimated_cost_today_exposes_freshness_alongside_breakdown(hass):
    """El coste estimado de hoy depende del importado "de hoy" — sin frescura, no hay forma de
    saber si el número viene de una hora vieja sin cruzarlo con otro sensor (ver
    coordinator.last_value_change / sensor._freshness_attributes)."""
    from datetime import datetime, timezone

    bundle = _bundle({"tariff_type": "fija", "fixed_price": 0.2})
    bundle["consumption"]["dailyTotals"] = [{"date": "01/08/2026", "importedKwh": 10.0, "exportedKwh": 0.0}]
    entities = await _setup_with_fake_coordinator(hass, {"contA": bundle})
    coordinator = next(iter(hass.data[DOMAIN].values()))
    changed_at = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)
    coordinator._last_value_change["contA"] = {"imported": changed_at}

    by_id = {e._attr_unique_id: e for e in entities if hasattr(e, "_attr_unique_id")}
    attrs = by_id["contA_estimated_cost_today"].extra_state_attributes
    assert attrs["ultimo_cambio"] == changed_at.isoformat()
    assert "minutos_sin_cambiar" in attrs
    assert "total" in attrs  # el desglose de costes (heredado de _EdistribucionEstimatedCostSensor) sigue ahí


async def test_estimated_cost_today_freshness_absent_without_tracked_change(hass):
    bundles = {"contA": _bundle({"tariff_type": "fija", "fixed_price": 0.2})}
    entities = await _setup_with_fake_coordinator(hass, bundles)
    by_id = {e._attr_unique_id: e for e in entities if hasattr(e, "_attr_unique_id")}
    attrs = by_id["contA_estimated_cost_today"].extra_state_attributes
    assert "ultimo_cambio" not in attrs


async def test_surplus_compensation_today_exposes_freshness(hass):
    from datetime import datetime, timezone

    bundles = {"contA": _bundle({"surplus_compensation": True, "surplus_price": 0.05})}
    entities = await _setup_with_fake_coordinator(hass, bundles)
    coordinator = next(iter(hass.data[DOMAIN].values()))
    changed_at = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)
    coordinator._last_value_change["contA"] = {"exported": changed_at}

    by_id = {e._attr_unique_id: e for e in entities if hasattr(e, "_attr_unique_id")}
    attrs = by_id["contA_surplus_compensation_today"].extra_state_attributes
    assert attrs["ultimo_cambio"] == changed_at.isoformat()
    assert "minutos_sin_cambiar" in attrs


async def test_year_to_date_cost_adds_completed_months_and_current_month(hass):
    bundles = {"contA": _bundle({"tariff_type": "fija", "fixed_price": 0.2})}  # mes en curso: 10 kWh x 0.2 = 2.0
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "localhost", "port": 8099}, options={})
    entry.add_to_hass(hass)
    coordinator = FakeCoordinator(bundles, entry.entry_id)
    coordinator._year_to_date["contA"] = {"imported_kwh": 50.0, "exported_kwh": 0.0, "cost": 10.0}
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    from custom_components.edistribucion import sensor as sensor_module

    added = []
    await sensor_module.async_setup_entry(hass, entry, lambda new, update_before_add=False: added.extend(new))

    sensor = next(e for e in added if getattr(e, "_attr_unique_id", None) == "contA_year_to_date_cost")
    assert sensor.native_value == pytest.approx(12.0)  # 10.0 (meses completados) + 2.0 (mes en curso)


async def test_always_created_sensors_present(hass):
    bundles = {"contA": _bundle()}
    entities = await _setup_with_fake_coordinator(hass, bundles)
    ids = _unique_ids(entities)
    assert "contA_imported_energy_today" in ids
    assert "contA_exported_energy_today" in ids
    assert "contA_contracted_power" in ids
    assert "contA_month_vs_last_year" in ids
