"""Tests de costs.py — cálculo de coste de energía (fija/tramos/pvpc), festivos, potencia,
autosuficiencia y comparador de tarifas. No depende de Home Assistant."""

from __future__ import annotations

import pytest

from custom_components.edistribucion.costs import (  # noqa: E402
    LLANO,
    PUNTA,
    VALLE,
    average_price_per_kwh,
    cost_breakdown,
    estimate_cost_as_tariff,
    estimate_energy_cost,
    hour_period,
    latest_hour_flow_kwh,
    max_power_by_period,
    max_power_reported,
    monthly_summary_csv,
    power_cost,
    power_excess_detected,
    pvpc_cost_breakdown,
    self_consumption_ratio,
    surplus_compensation_value,
)


def _hourly(date_str: str, entries: list[tuple[str, float]]) -> dict:
    """entries: lista de (hour_label, importedKwh)."""
    return {"hourlyByDate": {date_str: [{"hour": h, "importedKwh": kwh} for h, kwh in entries]}}


class TestHourPeriod:
    @pytest.mark.parametrize(
        "hour_label,expected",
        [
            ("10 - 11 h", PUNTA),
            ("13 - 14 h", PUNTA),
            ("18 - 19 h", PUNTA),
            ("21 - 22 h", PUNTA),
            ("8 - 9 h", LLANO),
            ("14 - 15 h", LLANO),
            ("22 - 23 h", LLANO),
            ("23 - 24 h", LLANO),
            ("0 - 1 h", VALLE),
            ("7 - 8 h", VALLE),
        ],
    )
    def test_weekday_hours(self, hour_label, expected):
        # 27/07/2026 es lunes
        assert hour_period("27/07/2026", hour_label) == expected

    def test_saturday_is_always_valle(self):
        # 25/07/2026 es sábado
        assert hour_period("25/07/2026", "11 - 12 h") == VALLE

    def test_sunday_is_always_valle(self):
        # 26/07/2026 es domingo
        assert hour_period("26/07/2026", "11 - 12 h") == VALLE

    def test_malformed_date_defaults_to_llano(self):
        assert hour_period("no-es-una-fecha", "11 - 12 h") == LLANO

    def test_missing_hour_label_defaults_to_llano(self):
        assert hour_period("27/07/2026", "") == LLANO

    def test_holiday_weekday_without_region_counts_as_normal(self):
        # 6 de enero de 2026 (Reyes) es martes -> sin región de festivos, cuenta como laborable normal
        assert hour_period("06/01/2026", "11 - 12 h") == PUNTA

    def test_holiday_weekday_with_region_counts_as_valle(self):
        from custom_components.edistribucion.costs import _get_holiday_calendar

        calendar = _get_holiday_calendar("IB")
        assert hour_period("06/01/2026", "11 - 12 h", calendar) == VALLE

    def test_holiday_calendar_none_region_is_noop(self):
        from custom_components.edistribucion.costs import _get_holiday_calendar

        assert _get_holiday_calendar(None) is None
        assert _get_holiday_calendar("none") is None

    def test_holiday_calendar_different_regions_differ(self):
        """11 de septiembre de 2026 (Diada, viernes) es festivo en Cataluña pero no en Baleares."""
        from custom_components.edistribucion.costs import _get_holiday_calendar

        import datetime

        assert datetime.datetime.strptime("11/09/2026", "%d/%m/%Y").weekday() < 5  # es viernes

        ct = _get_holiday_calendar("CT")
        ib = _get_holiday_calendar("IB")
        assert hour_period("11/09/2026", "11 - 12 h", ct) == VALLE
        assert hour_period("11/09/2026", "11 - 12 h", ib) == PUNTA


class TestCostBreakdown:
    def test_none_consumption_returns_none(self):
        assert cost_breakdown(None, {}) is None

    def test_consumption_without_hourly_data_returns_none(self):
        assert cost_breakdown({}, {}) is None
        assert cost_breakdown({"hourlyByDate": {}}, {}) is None

    def test_basic_breakdown_by_period(self):
        consumption = _hourly("27/07/2026", [("11 - 12 h", 2.0), ("0 - 1 h", 3.0), ("9 - 10 h", 1.0)])
        prices = {PUNTA: 0.30, LLANO: 0.20, VALLE: 0.10}
        result = cost_breakdown(consumption, prices)
        assert result["kwh_punta"] == 2.0
        assert result["kwh_llano"] == 1.0
        assert result["kwh_valle"] == 3.0
        assert result["coste_punta"] == 0.6
        assert result["coste_llano"] == 0.2
        assert result["coste_valle"] == 0.3
        assert result["total"] == pytest.approx(1.1)

    def test_missing_price_for_period_defaults_to_zero(self):
        consumption = _hourly("27/07/2026", [("11 - 12 h", 2.0)])
        result = cost_breakdown(consumption, {})
        assert result["coste_punta"] == 0.0
        assert result["total"] == 0.0

    def test_multiple_days_accumulate(self):
        consumption = {
            "hourlyByDate": {
                "27/07/2026": [{"hour": "11 - 12 h", "importedKwh": 1.0}],
                "28/07/2026": [{"hour": "11 - 12 h", "importedKwh": 2.0}],
            }
        }
        result = cost_breakdown(consumption, {PUNTA: 1.0, LLANO: 0, VALLE: 0})
        assert result["kwh_punta"] == 3.0

    def test_holiday_region_reclassifies_holiday_as_valle(self):
        consumption = _hourly("06/01/2026", [("11 - 12 h", 5.0)])  # Reyes, martes
        without_region = cost_breakdown(consumption, {PUNTA: 1, VALLE: 1}, holiday_region=None)
        with_region = cost_breakdown(consumption, {PUNTA: 1, VALLE: 1}, holiday_region="IB")
        assert without_region["kwh_punta"] == 5.0
        assert with_region["kwh_valle"] == 5.0
        assert with_region["kwh_punta"] == 0.0

    def test_zero_importedkwh_hour_does_not_crash(self):
        consumption = _hourly("27/07/2026", [("11 - 12 h", 0.0)])
        result = cost_breakdown(consumption, {PUNTA: 1.0, LLANO: 0, VALLE: 0})
        assert result["kwh_punta"] == 0.0

    def test_none_importedkwh_treated_as_zero(self):
        consumption = {"hourlyByDate": {"27/07/2026": [{"hour": "11 - 12 h", "importedKwh": None}]}}
        result = cost_breakdown(consumption, {PUNTA: 1.0, LLANO: 0, VALLE: 0})
        assert result["kwh_punta"] == 0.0


class TestPvpcCostBreakdown:
    def test_none_consumption_returns_none(self):
        assert pvpc_cost_breakdown(None, {"27/07/2026 0": 0.1}) is None

    def test_no_prices_returns_none(self):
        consumption = _hourly("27/07/2026", [("0 - 1 h", 2.0)])
        assert pvpc_cost_breakdown(consumption, None) is None
        assert pvpc_cost_breakdown(consumption, {}) is None

    def test_basic_cost(self):
        consumption = _hourly("27/07/2026", [("0 - 1 h", 2.0), ("1 - 2 h", 3.0)])
        prices = {"27/07/2026 0": 0.10, "27/07/2026 1": 0.20}
        result = pvpc_cost_breakdown(consumption, prices)
        assert result["kwh_con_precio"] == 5.0
        assert result["horas_sin_precio"] == 0
        assert result["total"] == pytest.approx(2 * 0.10 + 3 * 0.20)

    def test_missing_price_hour_counted_but_not_charged(self):
        consumption = _hourly("27/07/2026", [("0 - 1 h", 2.0), ("1 - 2 h", 3.0)])
        prices = {"27/07/2026 0": 0.10}  # falta la hora 1 (aun no publicada)
        result = pvpc_cost_breakdown(consumption, prices)
        assert result["horas_sin_precio"] == 1
        assert result["kwh_con_precio"] == 2.0
        assert result["total"] == pytest.approx(0.2)

    def test_missing_price_with_zero_kwh_not_counted_as_missing(self):
        consumption = _hourly("27/07/2026", [("0 - 1 h", 0.0)])
        result = pvpc_cost_breakdown(consumption, {})
        assert result is None  # sin precios en absoluto -> None desde el principio

    def test_all_hours_missing_and_zero_kwh_returns_none(self):
        consumption = _hourly("27/07/2026", [("0 - 1 h", 0.0)])
        prices = {"28/07/2026 0": 0.1}  # ninguna hora del consumo tiene precio
        result = pvpc_cost_breakdown(consumption, prices)
        assert result is None

    def test_unparseable_hour_label_is_skipped(self):
        consumption = _hourly("27/07/2026", [("", 2.0), ("0 - 1 h", 3.0)])
        prices = {"27/07/2026 0": 0.10}
        result = pvpc_cost_breakdown(consumption, prices)
        assert result["kwh_con_precio"] == 3.0  # la hora sin etiqueta parseable no cuenta ni suma


class TestEstimateEnergyCost:
    def test_fija_basic(self):
        sp = {"tariff_type": "fija", "fixed_price": 0.15}
        result = estimate_energy_cost(sp, 10.0, None)
        assert result == {"tariff_type": "fija", "precio_eur_kwh": 0.15, "total": 1.5}

    def test_fija_without_price_returns_none(self):
        sp = {"tariff_type": "fija"}
        assert estimate_energy_cost(sp, 10.0, None) is None

    def test_fija_without_imported_kwh_returns_none(self):
        sp = {"tariff_type": "fija", "fixed_price": 0.15}
        assert estimate_energy_cost(sp, None, None) is None

    def test_tramos_dispatches_to_cost_breakdown(self):
        sp = {"tariff_type": "tramos", "price_punta": 0.3, "price_llano": 0.2, "price_valle": 0.1}
        hourly = _hourly("27/07/2026", [("11 - 12 h", 2.0)])
        result = estimate_energy_cost(sp, 2.0, hourly)
        assert result["tariff_type"] == "tramos"
        assert result["total"] == pytest.approx(0.6)

    def test_tramos_uses_holiday_region_from_sp_opts(self):
        sp = {"tariff_type": "tramos", "price_punta": 1.0, "price_valle": 1.0, "holiday_region": "IB"}
        hourly = _hourly("06/01/2026", [("11 - 12 h", 5.0)])
        result = estimate_energy_cost(sp, 5.0, hourly)
        assert result["kwh_valle"] == 5.0

    def test_default_tariff_is_tramos(self):
        sp = {"price_punta": 1.0, "price_llano": 0, "price_valle": 0}  # sin tariff_type
        hourly = _hourly("27/07/2026", [("11 - 12 h", 2.0)])
        result = estimate_energy_cost(sp, 2.0, hourly)
        assert result["tariff_type"] == "tramos"

    def test_pvpc_uses_configured_zone(self):
        sp = {"tariff_type": "pvpc", "pvpc_zone": "CYM"}
        hourly = _hourly("27/07/2026", [("0 - 1 h", 2.0)])
        prices_by_zone = {"PCB": {"27/07/2026 0": 0.10}, "CYM": {"27/07/2026 0": 0.50}}
        result = estimate_energy_cost(sp, 2.0, hourly, prices_by_zone)
        assert result["total"] == pytest.approx(1.0)  # usa CYM (0.50), no PCB

    def test_pvpc_without_zone_uses_default(self):
        sp = {"tariff_type": "pvpc"}
        hourly = _hourly("27/07/2026", [("0 - 1 h", 2.0)])
        prices_by_zone = {"PCB": {"27/07/2026 0": 0.10}}
        result = estimate_energy_cost(sp, 2.0, hourly, prices_by_zone)
        assert result["total"] == pytest.approx(0.2)

    def test_pvpc_without_any_prices_returns_none(self):
        sp = {"tariff_type": "pvpc"}
        hourly = _hourly("27/07/2026", [("0 - 1 h", 2.0)])
        assert estimate_energy_cost(sp, 2.0, hourly, None) is None


class TestEstimateCostAsTariff:
    def test_simulates_different_tariff_without_mutating_input(self):
        sp = {"tariff_type": "pvpc", "fixed_price": 0.20, "pvpc_zone": "PCB"}
        result = estimate_cost_as_tariff(sp, "fija", 10.0, None)
        assert result["tariff_type"] == "fija"
        assert result["total"] == 2.0
        assert sp["tariff_type"] == "pvpc"  # el dict original no se ha tocado

    def test_simulates_pvpc_even_when_active_tariff_is_fija(self):
        sp = {"tariff_type": "fija", "fixed_price": 0.20}
        hourly = _hourly("27/07/2026", [("0 - 1 h", 2.0)])
        prices_by_zone = {"PCB": {"27/07/2026 0": 0.10}}
        result = estimate_cost_as_tariff(sp, "pvpc", 2.0, hourly, prices_by_zone)
        assert result["tariff_type"] == "pvpc"
        assert result["total"] == pytest.approx(0.2)


class TestAveragePricePerKwh:
    def test_basic(self):
        assert average_price_per_kwh(1.5, 10.0) == 0.15

    def test_none_cost_returns_none(self):
        assert average_price_per_kwh(None, 10.0) is None

    def test_none_imported_returns_none(self):
        assert average_price_per_kwh(1.5, None) is None

    def test_zero_imported_returns_none(self):
        assert average_price_per_kwh(1.5, 0) is None

    def test_rounding(self):
        assert average_price_per_kwh(1, 3) == round(1 / 3, 5)


class TestPowerCost:
    def test_basic(self):
        sp = {
            "contracted_power_punta_kw": 4.6,
            "contracted_power_valle_kw": 4.6,
            "price_power_punta": 0.10,
            "price_power_valle": 0.05,
        }
        assert power_cost(sp) == pytest.approx(4.6 * 0.10 + 4.6 * 0.05)

    def test_missing_fields_default_to_zero(self):
        assert power_cost({}) == 0.0

    def test_only_punta_configured(self):
        sp = {"contracted_power_punta_kw": 5.0, "price_power_punta": 0.08}
        assert power_cost(sp) == pytest.approx(0.4)


class TestSelfConsumptionRatio:
    def test_no_grid_import_is_100_percent(self):
        """Caso reportado: 0 kWh importados de la red -> 100% de autosuficiencia."""
        assert self_consumption_ratio(0.0, 4.5) == 100.0

    def test_no_export_ever_is_0_percent(self):
        assert self_consumption_ratio(10.0, 0.0) == 0.0

    def test_intermediate_case(self):
        assert self_consumption_ratio(2.0, 3.0) == 60.0

    def test_none_imported_returns_none(self):
        assert self_consumption_ratio(None, 3.0) is None

    def test_none_exported_returns_none(self):
        assert self_consumption_ratio(2.0, None) is None

    def test_both_zero_returns_none(self):
        assert self_consumption_ratio(0.0, 0.0) is None


class TestSurplusCompensationValue:
    def test_disabled_returns_none(self):
        assert surplus_compensation_value({"surplus_compensation": False, "surplus_price": 0.05}, 10.0) is None

    def test_enabled_without_price_returns_none(self):
        assert surplus_compensation_value({"surplus_compensation": True, "surplus_price": 0}, 10.0) is None

    def test_enabled_without_exported_kwh_returns_none(self):
        assert surplus_compensation_value({"surplus_compensation": True, "surplus_price": 0.05}, None) is None

    def test_basic_value(self):
        sp = {"surplus_compensation": True, "surplus_price": 0.05}
        assert surplus_compensation_value(sp, 10.0) == 0.5


class TestMaxPowerReported:
    def test_none_without_data(self):
        assert max_power_reported(None) is None
        assert max_power_reported({}) is None

    def test_uses_max_value_when_present(self):
        assert max_power_reported({"maxValue": 3.8, "points": [{"valueKw": 1.0}]}) == 3.8

    def test_falls_back_to_max_of_points_without_max_value(self):
        power = {"points": [{"valueKw": 2.0}, {"valueKw": 4.2}, {"valueKw": 1.0}]}
        assert max_power_reported(power) == 4.2

    def test_none_without_max_value_or_points(self):
        assert max_power_reported({"points": []}) is None


class TestPowerExcessDetected:
    def test_none_without_max_power_demand(self):
        assert power_excess_detected(None, {"contractedPowerPuntaKw": 3.5}) is None

    def test_none_without_contract(self):
        assert power_excess_detected({"maxValue": 4.0}, None) is None

    def test_none_without_contracted_limit(self):
        assert power_excess_detected({"maxValue": 4.0}, {"contractedPowerPuntaKw": 0}) is None

    def test_true_when_above_punta_limit(self):
        assert power_excess_detected({"maxValue": 4.0}, {"contractedPowerPuntaKw": 3.5, "contractedPowerValleKw": 3.5}) is True

    def test_false_when_within_limit(self):
        assert power_excess_detected({"maxValue": 3.0}, {"contractedPowerPuntaKw": 3.5, "contractedPowerValleKw": 3.5}) is False

    def test_compares_against_the_higher_of_punta_and_valle(self):
        assert power_excess_detected({"maxValue": 4.0}, {"contractedPowerPuntaKw": 3.5, "contractedPowerValleKw": 5.0}) is False

    def test_per_period_catches_excess_in_the_lower_contracted_period(self):
        """Caso del falso negativo: valle contratado (2.0kW) es menor que punta (5.0kW). Un pico de
        3.0kW en valle no supera el máximo global (5.0kW) pero SÍ supera lo contratado en valle."""
        power = {"points": [{"periods": {"punta": 4.0, "valle": 3.0}}]}
        contract = {"contractedPowerPuntaKw": 5.0, "contractedPowerValleKw": 2.0}
        assert power_excess_detected(power, contract) is True

    def test_per_period_false_when_both_within_their_own_limits(self):
        power = {"points": [{"periods": {"punta": 4.0, "valle": 1.5}}]}
        contract = {"contractedPowerPuntaKw": 5.0, "contractedPowerValleKw": 2.0}
        assert power_excess_detected(power, contract) is False

    def test_per_period_true_when_punta_exceeds_its_own_limit(self):
        power = {"points": [{"periods": {"punta": 6.0, "valle": 1.0}}]}
        contract = {"contractedPowerPuntaKw": 5.0, "contractedPowerValleKw": 2.0}
        assert power_excess_detected(power, contract) is True

    def test_falls_back_to_global_max_without_recognizable_period_labels(self):
        """Sin "punta"/"valle" reconocibles en los labels, no se puede comparar por periodo — cae
        al comportamiento global (aquí el pico de 4.0 SÍ supera el mayor contratado, 3.5)."""
        power = {"maxValue": 4.0, "points": [{"periods": {"P1": 4.0, "P2": 1.0}}]}
        contract = {"contractedPowerPuntaKw": 3.5, "contractedPowerValleKw": 3.5}
        assert power_excess_detected(power, contract) is True


class TestMaxPowerByPeriod:
    def test_empty_without_data(self):
        assert max_power_by_period(None) == {}
        assert max_power_by_period({"points": []}) == {}

    def test_dict_shaped_periods(self):
        power = {
            "points": [
                {"periods": {"punta": 3.0, "valle": 1.0}},
                {"periods": {"punta": 4.5, "valle": 0.8}},
            ]
        }
        assert max_power_by_period(power) == {"punta": 4.5, "valle": 1.0}

    def test_list_shaped_periods(self):
        power = {
            "points": [
                {"periods": [{"tipo": "punta", "valueKw": 3.0}, {"tipo": "valle", "valueKw": 1.0}]},
                {"periods": [{"tipo": "punta", "valueKw": 4.5}]},
            ]
        }
        assert max_power_by_period(power) == {"punta": 4.5, "valle": 1.0}

    def test_ignores_points_without_periods(self):
        power = {"points": [{"date": "30/07/2026"}, {"periods": {"punta": 2.0}}]}
        assert max_power_by_period(power) == {"punta": 2.0}

    def test_unrecognizable_entry_is_skipped(self):
        power = {"points": [{"periods": [{"onlytext": "x"}, {"onlynum": 5}]}]}
        assert max_power_by_period(power) == {}


class TestLatestHourFlowKwh:
    def test_none_without_hourly_by_date(self):
        assert latest_hour_flow_kwh(None, "exportedKwh") is None
        assert latest_hour_flow_kwh({}, "exportedKwh") is None
        assert latest_hour_flow_kwh({"hourlyByDate": {}}, "exportedKwh") is None

    def test_picks_the_most_recent_hour_across_days(self):
        consumption = {
            "hourlyByDate": {
                "29/07/2026": [{"hour": "23 - 24 h", "exportedKwh": 1.0}],
                "30/07/2026": [{"hour": "0 - 1 h", "exportedKwh": 2.5}, {"hour": "1 - 2 h", "exportedKwh": 0.0}],
            }
        }
        assert latest_hour_flow_kwh(consumption, "exportedKwh") == ("30/07/2026 1", 0.0)

    def test_missing_field_defaults_to_zero(self):
        consumption = {"hourlyByDate": {"30/07/2026": [{"hour": "0 - 1 h"}]}}
        assert latest_hour_flow_kwh(consumption, "exportedKwh") == ("30/07/2026 0", 0.0)

    def test_malformed_date_is_skipped(self):
        consumption = {"hourlyByDate": {"no-es-fecha": [{"hour": "0 - 1 h", "exportedKwh": 5.0}]}}
        assert latest_hour_flow_kwh(consumption, "exportedKwh") is None


class TestMonthlySummaryCsv:
    def test_fija_tariff(self):
        sp = {"tariff_type": "fija", "fixed_price": 0.2}
        month = {"totalImportedKwh": 100.0, "totalExportedKwh": 0.0}
        csv = monthly_summary_csv(sp, month)
        assert "tarifa,fija" in csv
        assert "kwh_importados,100.0" in csv
        assert "coste_energia,20.0" in csv
        assert "total_estimado,20.0" in csv

    def test_tramos_tariff_includes_period_breakdown(self):
        sp = {"tariff_type": "tramos", "price_punta": 0.3, "price_llano": 0.2, "price_valle": 0.1}
        month = {
            "totalImportedKwh": 10.0,
            "hourlyByDate": {"30/07/2026": [{"hour": "10 - 11 h", "importedKwh": 10.0}]},  # jueves, punta
        }
        csv = monthly_summary_csv(sp, month)
        assert "kwh_punta,10.0" in csv
        assert "coste_punta,3.0" in csv
        assert "coste_energia,3.0" in csv

    def test_includes_power_term_and_surplus_compensation(self):
        sp = {
            "tariff_type": "fija",
            "fixed_price": 0.2,
            "contracted_power_punta_kw": 5.0,
            "price_power_punta": 0.1,
            "surplus_compensation": True,
            "surplus_price": 0.05,
        }
        month = {"totalImportedKwh": 10.0, "totalExportedKwh": 4.0}
        csv = monthly_summary_csv(sp, month)
        assert "termino_potencia,0.5" in csv
        assert "compensacion_excedentes,0.2" in csv
        # 10*0.2 (energía) + 0.5 (potencia) - 0.2 (excedentes) = 2.3
        assert "total_estimado,2.3" in csv

    def test_without_month_data_still_returns_header_and_zeros(self):
        sp = {"tariff_type": "fija", "fixed_price": 0.2}
        csv = monthly_summary_csv(sp, None)
        assert "coste_energia,0" in csv
        assert "total_estimado,0" in csv
