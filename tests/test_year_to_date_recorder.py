"""Test de integración del acumulado del año (issue #27) contra el recorder REAL de Home Assistant.

Antes de implementar esto se verificó (leyendo `compile_statistics` en
`homeassistant/components/sensor/recorder.py`) que las estadísticas AUTOMÁTICAS de un sensor
`state_class=TOTAL` sin `last_reset` (nuestros sensores "_hoy") NO compensan el reseteo diario a
medianoche: el `sum` que compila Home Assistant solo no detecta un reset automáticamente más que la
PRIMERA vez que se compilan estadísticas para esa entidad — a partir de ahí, `sum` telescopa a
`estado_actual - primer_estado_histórico`, inútil como acumulado real. Por eso el acumulado del año
lee estadísticas EXTERNAS propias (`edistribucion:<cups>_...`, ver statistics.py), que esta
integración arrastra explícitamente día a día — igual que ya hacía para energía desde antes de
este issue.

Este test verifica el otro extremo de la cadena: que `recorder.statistics_during_period(period=
"month", types={"change"})` sobre esas estadísticas EXTERNAS da de verdad el total mensual
correcto. La lógica pura de agregación (qué meses cuentan como completados, el defecto en enero,
el detalle mes a mes) ya tiene cobertura sin recorder en test_coordinator.py.

Necesita el recorder real — se verifica vía CI (`recorder_mock`/`async_wait_recording_done`), no en
el sandbox de desarrollo local (sin pip)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.edistribucion.const import CONF_SUPPLY_POINTS, DOMAIN
from custom_components.edistribucion.coordinator import EdistribucionCoordinator
from custom_components.edistribucion.statistics import async_backfill_cost_statistics, async_backfill_energy_statistics

# Sin enable_custom_integrations a propósito — mismo motivo que test_statistics_recorder.py: ese
# fixture depende de `hass`, así que lo instanciaría ANTES de que `recorder_mock` pueda montar la
# base de datos falsa (pytest-homeassistant-custom-component exige recorder_mock antes que hass).

_CUPS = "ES123"
_SP = {
    "cont_id": "cont1",
    "cups": _CUPS,
    "tariff_type": "fija",
    "fixed_price": 0.2,
    "contracted_power_punta_kw": 3.5,
    "contracted_power_valle_kw": 3.5,
    "price_power_punta": 0.1,
    "price_power_valle": 0.05,
    "surplus_compensation": True,
    "surplus_price": 0.05,
}
_DAILY_POWER_COST = 3.5 * 0.1 + 3.5 * 0.05  # 0.525 €/día


def _month_data(day_points: list[tuple[str, float, float]]) -> dict:
    """`day_points`: [(fecha DD/MM/YYYY, importedKwh, exportedKwh)] — un único bloque horario por
    día (12-13h) es suficiente para que `_cost_days` pueda construir el desglose."""
    return {"hourlyByDate": {date: [{"hour": "12 - 13 h", "importedKwh": imp, "exportedKwh": exp}] for date, imp, exp in day_points}}


async def _seed_month(hass, month_data: dict) -> None:
    await async_backfill_energy_statistics(hass, _CUPS, month_data)
    await async_backfill_cost_statistics(hass, _CUPS, _SP, month_data, pvpc_prices={})
    await async_wait_recording_done(hass)


def _make_coordinator(hass) -> EdistribucionCoordinator:
    client = AsyncMock()
    client.async_get_supply_points.return_value = [
        {"contId": "cont1", "cupsId": "cups1", "cups": _CUPS, "address": "x", "tariff": "2.0TD", "active": True, "startDate": "2026-01-01", "endDate": None}
    ]
    client.async_get_consumption.return_value = {"totalImportedKwh": 0.0, "totalExportedKwh": 0.0, "hourlyByDate": {}, "dailyTotals": []}
    client.async_get_contracted_power.return_value = {"contractedPowerPuntaKw": 3.5, "contractedPowerValleKw": 3.5}
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "localhost", "port": 8099}, options={CONF_SUPPLY_POINTS: {"cont1": _SP}})
    entry.add_to_hass(hass)
    return EdistribucionCoordinator(hass, client, entry)


async def test_completed_months_sum_from_real_recorder_statistics(recorder_mock, hass, monkeypatch):
    await _seed_month(hass, _month_data([("15/01/2026", 10.0, 4.0), ("16/01/2026", 5.0, 0.0)]))
    await _seed_month(hass, _month_data([("10/02/2026", 8.0, 2.0)]))

    coordinator = _make_coordinator(hass)
    monkeypatch.setattr(
        "custom_components.edistribucion.coordinator.dt_util.now", lambda: datetime(2026, 3, 15, tzinfo=timezone.utc)
    )

    await coordinator._async_update_year_to_date_if_needed({"cont1": {"supply_point": _SP, "month": None}})

    completed = coordinator.year_to_date_completed_months("cont1")
    assert completed["imported_kwh"] == pytest.approx(23.0)  # 10+5 (enero) + 8 (febrero)
    assert completed["exported_kwh"] == pytest.approx(6.0)  # 4 (enero) + 2 (febrero)
    assert completed["cost"] == pytest.approx(23.0 * 0.2)  # fija: kWh importados x 0.2 €/kWh
    assert completed["power_cost"] == pytest.approx(_DAILY_POWER_COST * 3)  # 3 días con dato en total
    assert completed["surplus_compensation"] == pytest.approx(6.0 * 0.05)

    details = coordinator.year_to_date_month_details("cont1")
    assert set(details) == {1, 2}
    assert details[1]["imported_kwh"] == pytest.approx(15.0)
    assert details[1]["exported_kwh"] == pytest.approx(4.0)
    assert details[2]["imported_kwh"] == pytest.approx(8.0)
    assert details[2]["exported_kwh"] == pytest.approx(2.0)


async def test_current_month_not_counted_as_completed(recorder_mock, hass, monkeypatch):
    """Solo enero está "cerrado" al mirarlo en febrero — el mes en curso no debe aparecer en el
    acumulado (lo suma en vivo cada sensor aparte, con `bundle["month"]`)."""
    await _seed_month(hass, _month_data([("15/01/2026", 10.0, 0.0)]))

    coordinator = _make_coordinator(hass)
    monkeypatch.setattr(
        "custom_components.edistribucion.coordinator.dt_util.now", lambda: datetime(2026, 2, 10, tzinfo=timezone.utc)
    )

    await coordinator._async_update_year_to_date_if_needed({"cont1": {"supply_point": _SP, "month": None}})

    completed = coordinator.year_to_date_completed_months("cont1")
    assert completed["imported_kwh"] == pytest.approx(10.0)
    assert set(coordinator.year_to_date_month_details("cont1")) == {1}


async def test_month_before_statistic_existed_counts_as_honest_zero(recorder_mock, hass, monkeypatch):
    """Issue #27, limitación aceptada: un mes sin ningún punto en la estadística (CUPS instalado a
    mitad de año, o esta métrica nueva en esta versión) cuenta como 0.0 real, no se inventa ni se
    omite en silencio como pasaba con la API de e-distribución (issue #25)."""
    await _seed_month(hass, _month_data([("10/02/2026", 8.0, 0.0)]))  # solo febrero tiene datos

    coordinator = _make_coordinator(hass)
    monkeypatch.setattr(
        "custom_components.edistribucion.coordinator.dt_util.now", lambda: datetime(2026, 3, 15, tzinfo=timezone.utc)
    )

    await coordinator._async_update_year_to_date_if_needed({"cont1": {"supply_point": _SP, "month": None}})

    details = coordinator.year_to_date_month_details("cont1")
    assert set(details) == {1, 2}
    assert details[1]["imported_kwh"] == 0.0
    assert details[2]["imported_kwh"] == pytest.approx(8.0)


async def test_price_change_after_closed_month_still_recomputes_within_addon_window(recorder_mock, hass, monkeypatch):
    """Un mes cerrado sigue estando DENTRO de la ventana que trae `month_data` de e-distribución
    (~30-40 días) hasta que cae fuera de ella: mientras siga dentro, un cambio de precio SÍ afecta
    retroactivamente su coste acumulado, igual que ya le pasa a los sensores `_mes` de HA (que
    tampoco recuerdan precios antiguos) — no es una regresión, es el mismo comportamiento ya
    aceptado, ahora también reflejado en las estadísticas externas de coste."""
    await _seed_month(hass, _month_data([("20/01/2026", 10.0, 0.0)]))

    sp_new_price = {**_SP, "fixed_price": 0.3}
    # Se re-escribe el mismo día (todavía "visible" para e-distribución) con el precio nuevo.
    await async_backfill_cost_statistics(hass, _CUPS, sp_new_price, _month_data([("20/01/2026", 10.0, 0.0)]), pvpc_prices={})
    await async_wait_recording_done(hass)

    coordinator = _make_coordinator(hass)
    monkeypatch.setattr(
        "custom_components.edistribucion.coordinator.dt_util.now", lambda: datetime(2026, 2, 10, tzinfo=timezone.utc)
    )

    await coordinator._async_update_year_to_date_if_needed({"cont1": {"supply_point": sp_new_price, "month": None}})

    completed = coordinator.year_to_date_completed_months("cont1")
    assert completed["cost"] == pytest.approx(10.0 * 0.3)
