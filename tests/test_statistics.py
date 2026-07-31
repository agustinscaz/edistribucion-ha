"""Tests de statistics.py — relleno de histórico del Dashboard de Energía. La parte pura (parseo de
fechas/horas, construcción de puntos hora a hora o día a día) se prueba aquí sin necesitar el
recorder de verdad. La escritura real vía `async_add_external_statistics` no se prueba (requiere el
recorder real de Home Assistant) — se confía en esa plumbing, se prueba nuestra propia lógica."""

from __future__ import annotations

from datetime import datetime, timezone

from custom_components.edistribucion.statistics import (
    _daily_points,
    _hourly_points,
    _leading_hour,
    _parse_day,
    _parse_hour,
    async_backfill_energy_statistics,
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
