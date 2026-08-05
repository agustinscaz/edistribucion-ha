"""Tests de costs.py — cálculo de coste de energía (fija/tramos/pvpc), festivos, potencia,
autosuficiencia y comparador de tarifas. No depende de Home Assistant."""

from __future__ import annotations

from datetime import datetime

import pytest

from custom_components.edistribucion.costs import (  # noqa: E402
    LLANO,
    PUNTA,
    VALLE,
    apply_iee,
    apply_iva,
    average_price_per_kwh,
    cost_breakdown,
    current_period,
    estimate_cost_as_tariff,
    estimate_energy_cost,
    hour_period,
    monthly_summary_csv,
    next_period_change,
    power_cost,
    pvpc_cost_breakdown,
    self_consumption_ratio,
    surplus_compensation_value,
    tramo_prices_today,
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

    def test_weekday_hours_default_zone_matches_pcb(self):
        """zone=None (instalaciones sin pvpc_zone guardado) usa el horario PCB, igual que zone="PCB"."""
        assert hour_period("27/07/2026", "11 - 12 h", zone=None) == PUNTA
        assert hour_period("27/07/2026", "11 - 12 h", zone="PCB") == PUNTA

    @pytest.mark.parametrize(
        "hour_label,expected",
        [
            # Ver issue #5: mismo horario que PCB desplazado +1h (11-15h/19-23h punta,
            # 9-11h/15-19h/23-01h llano, resto valle).
            ("11 - 12 h", PUNTA),
            ("14 - 15 h", PUNTA),
            ("19 - 20 h", PUNTA),
            ("22 - 23 h", PUNTA),
            ("9 - 10 h", LLANO),
            ("10 - 11 h", LLANO),  # PUNTA en PCB, LLANO en CYM — el caso que reportaba el bug
            ("15 - 16 h", LLANO),
            ("18 - 19 h", LLANO),  # PUNTA en PCB, LLANO en CYM
            ("23 - 24 h", LLANO),
            ("0 - 1 h", LLANO),  # LLANO en CYM (23-01h), VALLE en PCB
            ("1 - 2 h", VALLE),
            ("8 - 9 h", VALLE),  # LLANO en PCB, VALLE en CYM
        ],
    )
    def test_weekday_hours_cym_zone_shifted_one_hour(self, hour_label, expected):
        # 27/07/2026 es lunes
        assert hour_period("27/07/2026", hour_label, zone="CYM") == expected

    def test_saturday_is_always_valle(self):
        # 25/07/2026 es sábado
        assert hour_period("25/07/2026", "11 - 12 h") == VALLE

    def test_saturday_is_always_valle_in_cym_too(self):
        assert hour_period("25/07/2026", "11 - 12 h", zone="CYM") == VALLE

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

    def test_field_parameter_buckets_exported_kwh_with_flat_price(self):
        """La compensación de excedentes es un precio PLANO (mismo €/kWh en los tres tramos) — a
        diferencia de importado, no hay precios distintos por tramo que aplicar."""
        consumption = {
            "hourlyByDate": {
                "27/07/2026": [
                    {"hour": "11 - 12 h", "importedKwh": 99.0, "exportedKwh": 2.0},
                    {"hour": "0 - 1 h", "importedKwh": 99.0, "exportedKwh": 3.0},
                ]
            }
        }
        flat_price = {PUNTA: 0.06, LLANO: 0.06, VALLE: 0.06}
        result = cost_breakdown(consumption, flat_price, field="exportedKwh")
        assert result["kwh_punta"] == 2.0
        assert result["kwh_valle"] == 3.0
        assert result["coste_punta"] == pytest.approx(0.12)
        assert result["coste_valle"] == pytest.approx(0.18)

    def test_zone_cym_reclassifies_hour_that_would_be_punta_in_pcb(self):
        """Ver issue #5: 10-11h es PUNTA en PCB pero LLANO en CYM — sin `zone`, este kWh se
        clasificaría (mal) como punta para un CUPS de Ceuta/Melilla."""
        consumption = _hourly("27/07/2026", [("10 - 11 h", 2.0)])  # lunes
        prices = {PUNTA: 0.30, LLANO: 0.20, VALLE: 0.10}
        result_pcb = cost_breakdown(consumption, prices, zone="PCB")
        result_cym = cost_breakdown(consumption, prices, zone="CYM")
        assert result_pcb["kwh_punta"] == 2.0
        assert result_cym["kwh_punta"] == 0.0
        assert result_cym["kwh_llano"] == 2.0

    def test_iva_percent_applied_to_each_period_and_total(self):
        consumption = _hourly("27/07/2026", [("11 - 12 h", 2.0), ("0 - 1 h", 3.0)])
        prices = {PUNTA: 0.30, VALLE: 0.10}
        result = cost_breakdown(consumption, prices, iva_percent=21)
        assert result["total_sin_impuestos"] == pytest.approx(0.9)
        assert result["coste_punta"] == pytest.approx(0.6 * 1.21)
        assert result["total"] == pytest.approx(0.9 * 1.21)

    def test_iee_applied_before_iva_to_each_period_and_total(self):
        """Cada periodo redondea a 4 decimales por separado (IEE y luego IVA) antes de sumar — el
        total puede diferir en el último decimal de "sumar sin impuestos y aplicar impuestos al
        final" (ver apply_iee/apply_iva), así que el valor esperado se construye con las mismas
        funciones en vez de a mano."""
        consumption = _hourly("27/07/2026", [("11 - 12 h", 2.0), ("0 - 1 h", 3.0)])
        prices = {PUNTA: 0.30, VALLE: 0.10}
        result = cost_breakdown(consumption, prices, iee_percent=5.11269632, iva_percent=21)
        coste_punta_esperado = apply_iva(apply_iee(0.6, 5.11269632), 21)
        coste_valle_esperado = apply_iva(apply_iee(0.3, 5.11269632), 21)
        assert result["total_sin_impuestos"] == pytest.approx(0.9)
        assert result["total_con_iee"] == pytest.approx(apply_iee(0.6, 5.11269632) + apply_iee(0.3, 5.11269632))
        assert result["coste_punta"] == pytest.approx(coste_punta_esperado)
        assert result["total"] == pytest.approx(coste_punta_esperado + coste_valle_esperado)

    def test_taxes_default_to_zero_when_not_passed(self):
        consumption = _hourly("27/07/2026", [("11 - 12 h", 2.0)])
        result = cost_breakdown(consumption, {PUNTA: 0.30})
        assert result["total"] == result["total_sin_impuestos"] == result["total_con_iee"]
        assert result["iee_percent"] == 0
        assert result["iva_percent"] == 0


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

    def test_iva_percent_applied_to_total(self):
        consumption = _hourly("27/07/2026", [("0 - 1 h", 2.0)])
        prices = {"27/07/2026 0": 0.10}
        result = pvpc_cost_breakdown(consumption, prices, iva_percent=21)
        assert result["total_sin_impuestos"] == pytest.approx(0.2)
        assert result["total"] == pytest.approx(0.242)

    def test_iee_applied_before_iva_to_total(self):
        consumption = _hourly("27/07/2026", [("0 - 1 h", 2.0)])
        prices = {"27/07/2026 0": 0.10}
        result = pvpc_cost_breakdown(consumption, prices, iee_percent=5.11269632, iva_percent=21)
        assert result["total_sin_impuestos"] == pytest.approx(0.2)
        assert result["total_con_iee"] == pytest.approx(apply_iee(0.2, 5.11269632))
        assert result["total"] == pytest.approx(apply_iva(apply_iee(0.2, 5.11269632), 21))


class TestApplyIva:
    def test_basic(self):
        assert apply_iva(100.0, 21) == 121.0

    def test_zero_percent_is_noop(self):
        assert apply_iva(100.0, 0) == 100.0

    def test_none_percent_treated_as_zero(self):
        assert apply_iva(100.0, None) == 100.0

    def test_rounds_to_4_decimals(self):
        assert apply_iva(1 / 3, 21) == round((1 / 3) * 1.21, 4)


class TestApplyIee:
    def test_basic(self):
        assert apply_iee(100.0, 5.11269632) == pytest.approx(105.11269632, rel=1e-6)

    def test_zero_percent_is_noop(self):
        assert apply_iee(100.0, 0) == 100.0

    def test_none_percent_treated_as_zero(self):
        assert apply_iee(100.0, None) == 100.0

    def test_stacked_with_iva_matches_real_invoice_growth(self):
        """Ver issue #3: en factura real, IEE se aplica ANTES del IVA (es base imponible del IVA,
        no un recargo aditivo aparte) — con IEE=5.11269632% e IVA=21%, el crecimiento observado en
        producción fue ~27,0-27,2%, no un 21% ni un 26,11% (IEE+IVA sumados sin componer)."""
        base = 100.0
        con_iee = apply_iee(base, 5.11269632)
        total = apply_iva(con_iee, 21)
        assert total == pytest.approx(127.186, abs=0.001)


class TestEstimateEnergyCost:
    def test_fija_basic(self):
        sp = {"tariff_type": "fija", "fixed_price": 0.15}
        result = estimate_energy_cost(sp, 10.0, None)
        assert result == {
            "tariff_type": "fija",
            "precio_eur_kwh": 0.15,
            "iee_percent": 0,
            "iva_percent": 0,
            "total_sin_impuestos": 1.5,
            "total_con_iee": 1.5,
            "total": 1.5,
        }

    def test_fija_applies_configured_iva(self):
        sp = {"tariff_type": "fija", "fixed_price": 0.15, "iva_percent": 21}
        result = estimate_energy_cost(sp, 10.0, None)
        assert result["total_sin_impuestos"] == 1.5
        assert result["total"] == pytest.approx(1.815)

    def test_fija_applies_iee_before_iva(self):
        """Ver issue #3: el IEE es base imponible del IVA, no se suma aparte."""
        sp = {"tariff_type": "fija", "fixed_price": 0.15, "iee_percent": 5.11269632, "iva_percent": 21}
        result = estimate_energy_cost(sp, 10.0, None)
        assert result["total_sin_impuestos"] == 1.5
        assert result["total_con_iee"] == pytest.approx(apply_iee(1.5, 5.11269632))
        assert result["total"] == pytest.approx(apply_iva(apply_iee(1.5, 5.11269632), 21))

    def test_tramos_applies_configured_iva(self):
        sp = {"tariff_type": "tramos", "price_punta": 0.3, "price_llano": 0.2, "price_valle": 0.1, "iva_percent": 10}
        hourly = _hourly("27/07/2026", [("11 - 12 h", 2.0)])
        result = estimate_energy_cost(sp, 2.0, hourly)
        assert result["total_sin_impuestos"] == pytest.approx(0.6)
        assert result["total"] == pytest.approx(0.66)

    def test_tramos_applies_iee_and_iva(self):
        sp = {
            "tariff_type": "tramos",
            "price_punta": 0.3,
            "price_llano": 0.2,
            "price_valle": 0.1,
            "iee_percent": 5.11269632,
            "iva_percent": 21,
        }
        hourly = _hourly("27/07/2026", [("11 - 12 h", 2.0)])
        result = estimate_energy_cost(sp, 2.0, hourly)
        assert result["total_sin_impuestos"] == pytest.approx(0.6)
        assert result["total"] == pytest.approx(apply_iva(apply_iee(0.6, 5.11269632), 21))

    def test_pvpc_applies_configured_iva(self):
        sp = {"tariff_type": "pvpc", "iva_percent": 21}
        hourly = _hourly("27/07/2026", [("0 - 1 h", 2.0)])
        prices_by_zone = {"PCB": {"27/07/2026 0": 0.10}}
        result = estimate_energy_cost(sp, 2.0, hourly, prices_by_zone)
        assert result["total_sin_impuestos"] == pytest.approx(0.2)
        assert result["total"] == pytest.approx(0.242)

    def test_pvpc_applies_iee_and_iva(self):
        sp = {"tariff_type": "pvpc", "iee_percent": 5.11269632, "iva_percent": 21}
        hourly = _hourly("27/07/2026", [("0 - 1 h", 2.0)])
        prices_by_zone = {"PCB": {"27/07/2026 0": 0.10}}
        result = estimate_energy_cost(sp, 2.0, hourly, prices_by_zone)
        assert result["total_sin_impuestos"] == pytest.approx(0.2)
        assert result["total"] == pytest.approx(apply_iva(apply_iee(0.2, 5.11269632), 21))

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

    def test_tramos_uses_pvpc_zone_from_sp_opts_for_hour_classification(self):
        """Ver issue #5: sp_opts["pvpc_zone"]="CYM" también debe cambiar el horario de tramos, no
        solo el precio pvpc — 10-11h es LLANO en CYM (PUNTA en PCB, la zona por defecto)."""
        sp = {"tariff_type": "tramos", "price_punta": 1.0, "price_llano": 1.0, "pvpc_zone": "CYM"}
        hourly = _hourly("27/07/2026", [("10 - 11 h", 5.0)])  # lunes
        result = estimate_energy_cost(sp, 5.0, hourly)
        assert result["kwh_punta"] == 0.0
        assert result["kwh_llano"] == 5.0

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

    def test_iva_percent_applied(self):
        sp = {"contracted_power_punta_kw": 5.0, "price_power_punta": 0.08, "iva_percent": 21}
        assert power_cost(sp) == pytest.approx(0.4 * 1.21)

    def test_iee_applied_before_iva(self):
        sp = {
            "contracted_power_punta_kw": 5.0,
            "price_power_punta": 0.08,
            "iee_percent": 5.11269632,
            "iva_percent": 21,
        }
        assert power_cost(sp) == pytest.approx(apply_iva(apply_iee(0.4, 5.11269632), 21))


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

    def test_iva_percent_applied_to_energy_and_power_not_surplus(self):
        sp = {
            "tariff_type": "fija",
            "fixed_price": 0.2,
            "iva_percent": 21,
            "contracted_power_punta_kw": 5.0,
            "price_power_punta": 0.1,
            "surplus_compensation": True,
            "surplus_price": 0.05,
        }
        month = {"totalImportedKwh": 10.0, "totalExportedKwh": 4.0}
        csv = monthly_summary_csv(sp, month)
        assert "iee_percent,0" in csv
        assert "iva_percent,21" in csv
        assert "coste_energia_sin_impuestos,2.0" in csv
        assert "coste_energia,2.42" in csv  # 10*0.2 * 1.21
        assert "termino_potencia,0.605" in csv  # 5*0.1 * 1.21
        assert "compensacion_excedentes,0.2" in csv  # sin impuestos, ver TestSurplusCompensationValue
        # 2.42 (energía) + 0.605 (potencia) - 0.2 (excedentes) = 2.825
        assert "total_estimado,2.825" in csv

    def test_iee_and_iva_applied_to_energy_and_power_not_surplus(self):
        """Ver issue #3: fórmula real de factura, IEE aplicado antes del IVA."""
        sp = {
            "tariff_type": "fija",
            "fixed_price": 0.2,
            "iee_percent": 5.11269632,
            "iva_percent": 21,
            "contracted_power_punta_kw": 5.0,
            "price_power_punta": 0.1,
            "surplus_compensation": True,
            "surplus_price": 0.05,
        }
        month = {"totalImportedKwh": 10.0, "totalExportedKwh": 4.0}
        csv = monthly_summary_csv(sp, month)
        energia_con_iee = apply_iee(2.0, 5.11269632)
        energia = apply_iva(energia_con_iee, 21)
        potencia = apply_iva(apply_iee(0.5, 5.11269632), 21)
        assert f"coste_energia_con_iee,{energia_con_iee}" in csv
        assert f"coste_energia,{energia}" in csv
        assert f"termino_potencia,{potencia}" in csv
        total = round(energia + potencia - 0.2, 4)
        assert f"total_estimado,{total}" in csv


class TestCurrentPeriod:
    """Ver issue #4 — usadas por el sensor de precio actual con impuestos para tarifa 'tramos'."""

    def test_pcb_punta_hour(self):
        # 27/07/2026 es lunes, 10h -> punta en PCB
        assert current_period(datetime(2026, 7, 27, 10, 30)) == PUNTA

    def test_pcb_llano_hour(self):
        assert current_period(datetime(2026, 7, 27, 9, 0)) == LLANO

    def test_cym_zone_reclassifies_hour_10_as_llano(self):
        """Mismo instante que test_pcb_punta_hour, pero con zone="CYM" -> llano, no punta (issue #5)."""
        assert current_period(datetime(2026, 7, 27, 10, 30), zone="CYM") == LLANO

    def test_weekend_is_always_valle(self):
        # 25/07/2026 es sábado
        assert current_period(datetime(2026, 7, 25, 12, 0)) == VALLE

    def test_holiday_with_region_is_valle(self):
        # 6 de enero de 2026 (Reyes) es martes
        assert current_period(datetime(2026, 1, 6, 11, 0), holiday_region="IB") == VALLE

    def test_holiday_without_region_counts_as_normal(self):
        assert current_period(datetime(2026, 1, 6, 11, 0)) == PUNTA


class TestNextPeriodChange:
    def test_change_within_same_day(self):
        # Lunes 09:30 (llano) -> el próximo cambio es a las 10:00 (empieza punta)
        result = next_period_change(datetime(2026, 7, 27, 9, 30))
        assert result == datetime(2026, 7, 27, 10, 0)

    def test_result_is_always_on_the_hour(self):
        result = next_period_change(datetime(2026, 7, 27, 9, 45))
        assert result.minute == 0
        assert result.second == 0

    def test_crosses_weekend_into_monday_llano(self):
        """Domingo (valle todo el día) -> el próximo cambio es el lunes a las 8h, cuando PCB pasa
        de valle a llano — cruza más de un día, no solo una hora."""
        result = next_period_change(datetime(2026, 7, 26, 15, 0))
        assert result == datetime(2026, 7, 27, 8, 0)

    def test_cym_zone_changes_at_different_hour_than_pcb(self):
        # Lunes 10:30: en CYM es llano (empezó a las 9h) y sigue siéndolo hasta las 11h -> punta.
        result = next_period_change(datetime(2026, 7, 27, 10, 30), zone="CYM")
        assert result == datetime(2026, 7, 27, 11, 0)


class TestTramoPricesToday:
    def test_maps_each_hour_to_its_period_price(self):
        sp = {"price_punta": 0.30, "price_llano": 0.20, "price_valle": 0.10}
        prices = tramo_prices_today(datetime(2026, 7, 27, 12, 0), sp)  # lunes
        assert prices[10] == pytest.approx(0.30)  # punta
        assert prices[9] == pytest.approx(0.20)  # llano
        assert prices[0] == pytest.approx(0.10)  # valle
        assert len(prices) == 24

    def test_weekend_is_all_valle_price(self):
        sp = {"price_punta": 0.30, "price_llano": 0.20, "price_valle": 0.10}
        prices = tramo_prices_today(datetime(2026, 7, 25, 12, 0), sp)  # sábado
        assert all(p == pytest.approx(0.10) for p in prices.values())

    def test_applies_configured_taxes(self):
        sp = {"price_punta": 0.30, "price_llano": 0.20, "price_valle": 0.10, "iee_percent": 5.11269632, "iva_percent": 21}
        prices = tramo_prices_today(datetime(2026, 7, 27, 12, 0), sp)  # lunes
        assert prices[10] == pytest.approx(apply_iva(apply_iee(0.30, 5.11269632), 21))

    def test_uses_cym_zone_for_hour_classification(self):
        sp = {"price_punta": 0.30, "price_llano": 0.20, "pvpc_zone": "CYM"}
        prices = tramo_prices_today(datetime(2026, 7, 27, 12, 0), sp)  # lunes
        assert prices[10] == pytest.approx(0.20)  # llano en CYM, no punta
