"""Test de integración de statistics.py contra el recorder REAL de Home Assistant.

Verifica dos bugs relacionados, ambos reportados en producción:

1. (v1.20.1) El backfill se repite una vez al día, pero `month_data` (ver coordinator.py) es
   siempre el mes EN CURSO, nunca el histórico completo. Sin anclar `running_total` al cierre del
   mes anterior, al cruzar un límite de mes el `sum` volvía a arrancar en 0, rompiendo la
   monotonía que exigen las long-term statistics.
2. (seguimiento) El fix de (1), basado en `get_last_statistics` ("el último punto guardado en
   general"), reintroducía el MISMO problema un día después: a partir del segundo día del mes en
   curso, "lo último guardado en general" ya es una hora de ESTE MISMO MES (reescrita el día
   anterior), no el cierre del mes pasado — usarlo como ancla duplicaba el arrastre en cada
   re-ejecución dentro del mismo mes, corrompiendo silenciosamente (upsert, no excepción) el sum
   ya guardado del día 1. Arreglado con `_async_last_saved_stat_before`, que busca el último punto
   ESTRICTAMENTE ANTERIOR al primero que se va a (re)escribir (vía `statistics_during_period` con
   `end_time`), no "lo último guardado", sin importar qué día del mes sea el run actual.

Necesita el recorder real de Home Assistant — se verifica vía CI (`recorder_mock`/
`async_wait_recording_done` de pytest-homeassistant-custom-component), no en el sandbox de
desarrollo local (sin pip). La parte pura de esta lógica (`_carry_over_sum`) ya tiene cobertura
completa y determinista en test_statistics.py sin depender del recorder."""

from __future__ import annotations

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_last_statistics, statistics_during_period
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.edistribucion.statistics import async_backfill_energy_statistics

# Sin `enable_custom_integrations` a propósito: ese fixture depende de `hass`, así que lo
# instanciaría ANTES de que `recorder_mock` pueda configurar la base de datos falsa (pytest-
# homeassistant-custom-component exige recorder_mock antes que hass) — y aquí no hace falta, no se
# pasa por el cargador de integraciones de HA, se llama a la función directamente.

_CUPS = "ES0031500160526001DS0F"
_STATISTIC_ID = f"edistribucion:{_CUPS.lower()}_imported_energy"


async def _last_sum(hass) -> float:
    def _query():
        return get_last_statistics(hass, 1, _STATISTIC_ID, True, {"sum"})

    result = await get_instance(hass).async_add_executor_job(_query)
    rows = result.get(_STATISTIC_ID)
    return rows[0]["sum"] if rows else None


async def _sum_at(hass, day: str) -> float | None:
    """El sum guardado para el punto de un día concreto ("DD/MM/YYYY"), o None si no hay ninguno."""
    from datetime import datetime

    start = dt_util.as_utc(dt_util.as_local(datetime.strptime(day, "%d/%m/%Y")))

    def _query():
        return statistics_during_period(
            hass,
            start_time=start,
            end_time=None,
            statistic_ids={_STATISTIC_ID},
            period="hour",
            units=None,
            types={"sum"},
        )

    result = await get_instance(hass).async_add_executor_job(_query)
    rows = result.get(_STATISTIC_ID)
    if not rows:
        return None
    return rows[0]["sum"]  # start_time ya apunta exactamente a la medianoche de ese día


async def test_sum_stays_monotonic_across_a_month_boundary(recorder_mock, hass):
    july_data = {
        "dailyTotals": [
            {"date": "30/07/2026", "importedKwh": 10.0},
            {"date": "31/07/2026", "importedKwh": 8.7},
        ]
    }
    await async_backfill_energy_statistics(hass, _CUPS, july_data)
    await async_wait_recording_done(hass)

    july_last_sum = await _last_sum(hass)
    assert july_last_sum == pytest.approx(18.7)

    # Cambio de mes: month_data ahora SOLO trae agosto, como pasaría de verdad al cruzar el mes.
    august_data = {"dailyTotals": [{"date": "01/08/2026", "importedKwh": 2.0}]}
    await async_backfill_energy_statistics(hass, _CUPS, august_data)
    await async_wait_recording_done(hass)

    august_first_sum = await _last_sum(hass)
    # El bug original: sin ancla, esto habría sido 2.0 (reiniciado a 0 + el delta de agosto) en vez
    # de continuar desde el sum de julio — una caída de ~18.7 a ~2.0 de un día para otro.
    assert august_first_sum == pytest.approx(july_last_sum + 2.0)
    assert august_first_sum > july_last_sum


async def test_earlier_days_sum_unchanged_across_three_consecutive_runs(recorder_mock, hass):
    """El bug de seguimiento: con `get_last_statistics` ("lo último guardado en general") como
    ancla, el run del día 2 del mes reescribía el día 1 duplicando el arrastre de julio (porque
    "lo último guardado" ya era la propia hora del día 1, no el cierre de julio) — un fallo
    silencioso (upsert exitoso, sin excepción) que solo se ve comparando el sum del día 1 ANTES y
    DESPUÉS del run del día 2/3, no con una única comprobación día 1 -> día 2."""
    july_data = {"dailyTotals": [{"date": "31/07/2026", "importedKwh": 8.7}]}
    await async_backfill_energy_statistics(hass, _CUPS, july_data)
    await async_wait_recording_done(hass)

    # --- Día 1 del mes nuevo (01/08): month_data trae solo ese día ---
    await async_backfill_energy_statistics(hass, _CUPS, {"dailyTotals": [{"date": "01/08/2026", "importedKwh": 2.0}]})
    await async_wait_recording_done(hass)
    sum_day1_after_run1 = await _sum_at(hass, "01/08/2026")
    assert sum_day1_after_run1 == pytest.approx(10.7)  # 8.7 (julio) + 2.0

    # --- Día 2 del mes (02/08): month_data ya trae el mes completo desde el día 1 ---
    await async_backfill_energy_statistics(
        hass,
        _CUPS,
        {
            "dailyTotals": [
                {"date": "01/08/2026", "importedKwh": 2.0},
                {"date": "02/08/2026", "importedKwh": 3.0},
            ]
        },
    )
    await async_wait_recording_done(hass)
    sum_day1_after_run2 = await _sum_at(hass, "01/08/2026")
    # El bug: esto habría bajado a 2.0 (perdiendo el arrastre de julio) en vez de seguir en 10.7.
    assert sum_day1_after_run2 == pytest.approx(sum_day1_after_run1)

    # --- Día 3 del mes (03/08): un tercer run, para descartar que el problema reaparezca más tarde ---
    await async_backfill_energy_statistics(
        hass,
        _CUPS,
        {
            "dailyTotals": [
                {"date": "01/08/2026", "importedKwh": 2.0},
                {"date": "02/08/2026", "importedKwh": 3.0},
                {"date": "03/08/2026", "importedKwh": 1.5},
            ]
        },
    )
    await async_wait_recording_done(hass)
    sum_day1_after_run3 = await _sum_at(hass, "01/08/2026")
    assert sum_day1_after_run3 == pytest.approx(sum_day1_after_run1)

    # Y el sum final (03/08) debe seguir siendo la suma continua correcta, no reiniciada.
    assert await _last_sum(hass) == pytest.approx(8.7 + 2.0 + 3.0 + 1.5)


async def test_carry_over_still_works_after_a_gap_longer_than_the_recent_window(recorder_mock, hass):
    """Si el hueco entre el cierre del mes anterior y el primer punto de este mes es mayor que
    `_RECENT_WINDOW` (Home Assistant estuvo apagado varias semanas o meses seguidos), el arrastre
    debe seguir encontrando el sum correcto vía el fallback sin límite de ventana, no perderlo en
    silencio solo porque la búsqueda "reciente" no encontró nada."""
    await async_backfill_energy_statistics(hass, _CUPS, {"dailyTotals": [{"date": "01/01/2026", "importedKwh": 5.0}]})
    await async_wait_recording_done(hass)

    # ~7 meses de hueco hasta el siguiente backfill (bien por encima de _RECENT_WINDOW = 40 días).
    await async_backfill_energy_statistics(hass, _CUPS, {"dailyTotals": [{"date": "01/08/2026", "importedKwh": 2.0}]})
    await async_wait_recording_done(hass)

    assert await _last_sum(hass) == pytest.approx(5.0 + 2.0)


async def test_carry_over_logged_once_not_on_every_rerun(recorder_mock, hass, caplog):
    """El aviso de "arrastrando sum" debe verse una vez cuando de verdad aplica (confirmable desde
    el log sin tener que consultar la base de datos a mano) pero no repetirse en cada re-ejecución
    dentro del mismo mes, para no ensuciar el log a diario sin necesidad."""
    import logging

    from custom_components.edistribucion import statistics as statistics_module

    statistics_module._carry_over_logged.clear()  # aislar de otros tests de este módulo

    await async_backfill_energy_statistics(hass, _CUPS, {"dailyTotals": [{"date": "31/07/2026", "importedKwh": 8.7}]})
    await async_wait_recording_done(hass)

    with caplog.at_level(logging.INFO, logger="custom_components.edistribucion.statistics"):
        caplog.clear()
        await async_backfill_energy_statistics(hass, _CUPS, {"dailyTotals": [{"date": "01/08/2026", "importedKwh": 2.0}]})
        await async_wait_recording_done(hass)
        assert any("Arrastrando sum" in r.message for r in caplog.records)

        caplog.clear()
        await async_backfill_energy_statistics(
            hass,
            _CUPS,
            {"dailyTotals": [{"date": "01/08/2026", "importedKwh": 2.0}, {"date": "02/08/2026", "importedKwh": 3.0}]},
        )
        await async_wait_recording_done(hass)
        assert not any("Arrastrando sum" in r.message for r in caplog.records)
