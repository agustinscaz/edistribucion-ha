"""Test de integración de statistics.py contra el recorder REAL de Home Assistant.

Verifica el bug reportado en producción: el backfill ahora se repite una vez al día (ver
coordinator._async_backfill_statistics_if_needed), pero `month_data` (ver coordinator.py) es
siempre el mes EN CURSO, nunca el histórico completo. Sin anclar `running_total` al último `sum` ya
guardado, al cruzar un límite de mes el `sum` de la long-term statistic volvía a arrancar en 0,
rompiendo la monotonía (rompe cualquier vista del recorder que cruce ese límite: año, multi-mes...).

Necesita el recorder real de Home Assistant — se verifica vía CI (`recorder_mock`/
`async_wait_recording_done` de pytest-homeassistant-custom-component), no en el sandbox de
desarrollo local (sin pip). La parte pura de esta lógica (`_carry_over_sum`) ya tiene cobertura
completa y determinista en test_statistics.py sin depender del recorder."""

from __future__ import annotations

import pytest

from custom_components.edistribucion.statistics import async_backfill_energy_statistics

# Sin `enable_custom_integrations` a propósito: ese fixture depende de `hass`, así que lo
# instanciaría ANTES de que `recorder_mock` pueda configurar la base de datos falsa (pytest-
# homeassistant-custom-component exige recorder_mock antes que hass) — y aquí no hace falta, no se
# pasa por el cargador de integraciones de HA, se llama a la función directamente.


async def test_sum_stays_monotonic_across_a_month_boundary(recorder_mock, hass):
    from homeassistant.components.recorder.statistics import get_last_statistics
    from pytest_homeassistant_custom_component.common import async_wait_recording_done

    cups = "ES0031500160526001DS0F"
    statistic_id = f"edistribucion:{cups.lower()}_imported_energy"

    july_data = {
        "dailyTotals": [
            {"date": "30/07/2026", "importedKwh": 10.0},
            {"date": "31/07/2026", "importedKwh": 8.7},
        ]
    }
    await async_backfill_energy_statistics(hass, cups, july_data)
    await async_wait_recording_done(hass)

    last = get_last_statistics(hass, 1, statistic_id, True, {"sum"})
    july_last_sum = last[statistic_id][0]["sum"]
    assert july_last_sum == pytest.approx(18.7)

    # Cambio de mes: month_data ahora SOLO trae agosto, como pasaría de verdad al cruzar el mes.
    august_data = {"dailyTotals": [{"date": "01/08/2026", "importedKwh": 2.0}]}
    await async_backfill_energy_statistics(hass, cups, august_data)
    await async_wait_recording_done(hass)

    last = get_last_statistics(hass, 1, statistic_id, True, {"sum"})
    august_first_sum = last[statistic_id][0]["sum"]
    # El bug real: sin el fix, esto habría sido 2.0 (reiniciado a 0 + el delta de agosto) en vez de
    # continuar desde el sum de julio — una caída de ~18.7 a ~2.0 de un día para otro.
    assert august_first_sum == pytest.approx(july_last_sum + 2.0)
    assert august_first_sum > july_last_sum


async def test_reruns_within_the_same_month_do_not_double_count(recorder_mock, hass):
    """Repetir el backfill varias veces dentro del MISMO mes (el ciclo diario normal, sin cruzar
    ningún límite de mes) no debe ir sumando el mismo consumo una y otra vez."""
    from homeassistant.components.recorder.statistics import get_last_statistics
    from pytest_homeassistant_custom_component.common import async_wait_recording_done

    cups = "ES0031500160526001DS0F"
    statistic_id = f"edistribucion:{cups.lower()}_imported_energy"

    await async_backfill_energy_statistics(hass, cups, {"dailyTotals": [{"date": "01/08/2026", "importedKwh": 2.0}]})
    await async_wait_recording_done(hass)

    # Un día más tarde, "month_data" ya trae los dos días (mismo mes) — se re-escribe todo.
    await async_backfill_energy_statistics(
        hass,
        cups,
        {
            "dailyTotals": [
                {"date": "01/08/2026", "importedKwh": 2.0},
                {"date": "02/08/2026", "importedKwh": 3.0},
            ]
        },
    )
    await async_wait_recording_done(hass)

    last = get_last_statistics(hass, 1, statistic_id, True, {"sum"})
    assert last[statistic_id][0]["sum"] == pytest.approx(5.0)  # no 7.0 (2.0 contado dos veces)
