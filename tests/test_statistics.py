"""Tests de statistics.py — relleno de histórico del Dashboard de Energía. La parte pura (parseo de
fechas/horas, construcción de puntos hora a hora o día a día) se prueba aquí sin necesitar el
recorder de verdad. La escritura real vía `async_add_external_statistics` no se prueba (requiere el
recorder real de Home Assistant) — se confía en esa plumbing, se prueba nuestra propia lógica."""

from __future__ import annotations

from datetime import datetime, timezone

from custom_components.edistribucion.statistics import (
    _carry_over_sum,
    _daily_points,
    _hourly_points,
    _leading_hour,
    _parse_day,
    _parse_hour,
    async_backfill_energy_statistics,
    months_back,
)


class TestLeadingHour:
    def test_parses_range_label(self):
        assert _leading_hour("13 - 14 h") == 13

    def test_parses_bare_number(self):
        assert _leading_hour("7") == 7

    def test_none_on_empty(self):
        assert _leading_hour("") is None

    def test_none_on_malformed(self):
        assert _leading_hour("no es una hora") is None


class TestParseDay:
    def test_midnight_of_that_day(self):
        result = _parse_day("30/07/2026")
        assert result.tzinfo is not None
        assert (result.year, result.month, result.day, result.hour) == (2026, 7, 30, 0)


class TestParseHour:
    def test_sets_the_given_hour(self):
        result = _parse_hour("30/07/2026", 13)
        assert result.tzinfo is not None
        assert (result.year, result.month, result.day, result.hour) == (2026, 7, 30, 13)


class TestMonthsBack:
    def test_single_month_is_just_base(self):
        assert months_back(datetime(2026, 7, 30), 1) == [datetime(2026, 7, 1)]

    def test_chronological_order_oldest_first(self):
        result = months_back(datetime(2026, 7, 30), 3)
        assert result == [datetime(2026, 5, 1), datetime(2026, 6, 1), datetime(2026, 7, 1)]

    def test_wraps_across_year_boundary(self):
        result = months_back(datetime(2026, 2, 15), 3)
        assert result == [datetime(2025, 12, 1), datetime(2026, 1, 1), datetime(2026, 2, 1)]

    def test_twelve_months_back_from_january(self):
        result = months_back(datetime(2026, 1, 10), 12)
        assert result[0] == datetime(2025, 2, 1)
        assert result[-1] == datetime(2026, 1, 1)
        assert len(result) == 12


class TestHourlyPoints:
    def test_none_without_hourly_by_date(self):
        assert _hourly_points({}, "importedKwh") is None
        assert _hourly_points({"hourlyByDate": {}}, "importedKwh") is None

    def test_one_point_per_hour(self):
        month_data = {
            "hourlyByDate": {
                "30/07/2026": [
                    {"hour": "0 - 1 h", "importedKwh": 1.0},
                    {"hour": "1 - 2 h", "importedKwh": 2.0},
                ]
            }
        }
        points = _hourly_points(month_data, "importedKwh")
        assert len(points) == 2
        assert points[0][1] == 1.0
        assert points[1][1] == 2.0
        assert points[0][0].hour == 0
        assert points[1][0].hour == 1

    def test_missing_field_defaults_to_zero(self):
        month_data = {"hourlyByDate": {"30/07/2026": [{"hour": "0 - 1 h"}]}}
        points = _hourly_points(month_data, "importedKwh")
        assert points == [(points[0][0], 0.0)]

    def test_malformed_hour_label_is_skipped(self):
        month_data = {
            "hourlyByDate": {
                "30/07/2026": [
                    {"hour": "no es una hora", "importedKwh": 1.0},
                    {"hour": "5 - 6 h", "importedKwh": 2.0},
                ]
            }
        }
        points = _hourly_points(month_data, "importedKwh")
        assert len(points) == 1
        assert points[0][1] == 2.0


class TestDailyPoints:
    def test_none_without_daily_totals(self):
        assert _daily_points({}, "importedKwh") is None
        assert _daily_points({"dailyTotals": []}, "importedKwh") is None

    def test_one_point_per_day(self):
        month_data = {"dailyTotals": [{"date": "30/07/2026", "importedKwh": 5.0}, {"date": "31/07/2026", "importedKwh": 3.0}]}
        points = _daily_points(month_data, "importedKwh")
        assert [p[1] for p in points] == [5.0, 3.0]

    def test_missing_field_defaults_to_zero(self):
        month_data = {"dailyTotals": [{"date": "30/07/2026"}]}
        points = _daily_points(month_data, "importedKwh")
        assert points == [(points[0][0], 0.0)]


class TestCarryOverSum:
    """El bug real: `month_data` (ver coordinator.py) es siempre el mes EN CURSO — al cambiar de
    mes, `points[0]` pasa a ser el día/hora 1 del mes nuevo. Sin anclar al último sum guardado, el
    running_total volvería a arrancar en 0 aunque el recorder ya tuviera un sum grande acumulado
    del mes anterior, provocando una caída que rompe la monotonía de las long-term statistics."""

    def test_no_previous_data_starts_at_zero(self):
        """Primera vez que se crea el statistic_id — 0.0 sigue siendo correcto."""
        first_point = datetime(2026, 8, 1, tzinfo=timezone.utc)
        assert _carry_over_sum(None, first_point) == 0.0

    def test_anchors_to_previous_month_close_at_month_boundary(self):
        """El caso que estaba roto: lo último guardado es del 31/07 (mes anterior al que se va a
        escribir ahora, que empieza el 01/08) -> debe usarse como base, no reiniciar a 0."""
        last_saved = (datetime(2026, 7, 31, tzinfo=timezone.utc), 38.7)
        first_point = datetime(2026, 8, 1, tzinfo=timezone.utc)
        assert _carry_over_sum(last_saved, first_point) == 38.7

    def test_does_not_double_count_within_the_same_month(self):
        """Re-ejecución dentro del MISMO mes (lo último guardado es un día de este mismo mes, que
        `month_data` va a reescribir de todas formas): no hay que sumarlo aparte, o se contaría
        dos veces — sigue arrancando en 0, como siempre."""
        last_saved = (datetime(2026, 8, 15, tzinfo=timezone.utc), 20.0)
        first_point = datetime(2026, 8, 1, tzinfo=timezone.utc)
        assert _carry_over_sum(last_saved, first_point) == 0.0

    def test_equal_timestamp_does_not_anchor(self):
        """Si coincide exactamente con el primer punto (se va a reescribir), tampoco debe sumarse
        aparte — el límite es estrictamente "anterior", no "anterior o igual"."""
        last_saved = (datetime(2026, 8, 1, tzinfo=timezone.utc), 5.0)
        first_point = datetime(2026, 8, 1, tzinfo=timezone.utc)
        assert _carry_over_sum(last_saved, first_point) == 0.0


class _FakeConfig:
    def __init__(self, components):
        self.components = components


class _FakeHass:
    def __init__(self, components=()):
        self.config = _FakeConfig(set(components))


class TestAsyncBackfillEnergyStatistics:
    async def test_noop_without_month_data(self):
        # No debe ni mirar hass.config si no hay nada que rellenar.
        await async_backfill_energy_statistics(_FakeHass(), "ES123", None)
        await async_backfill_energy_statistics(_FakeHass(), "ES123", {"dailyTotals": [], "hourlyByDate": {}})

    async def test_noop_without_recorder_component(self):
        hass = _FakeHass(components=set())
        month_data = {"dailyTotals": [{"date": "30/07/2026", "importedKwh": 5.0}]}
        await async_backfill_energy_statistics(hass, "ES123", month_data)  # no debe lanzar

    async def test_graceful_when_recorder_api_not_importable(self):
        """Con "recorder" en components pero sin el paquete real instalado (este sandbox), debe
        degradar con un aviso, no lanzar — documenta el manejo de errores generoso descrito en el
        docstring del módulo."""
        hass = _FakeHass(components={"recorder"})
        month_data = {"dailyTotals": [{"date": "30/07/2026", "importedKwh": 5.0}]}
        await async_backfill_energy_statistics(hass, "ES123", month_data)  # no debe lanzar
