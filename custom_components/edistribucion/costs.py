"""Coste estimado de energía, según el tipo de tarifa configurado por CUPS (fija/tramos/pvpc), y
compensación por excedentes exportados.

Horario de tramos usado (2.0TD peninsular, el más común en residencial):
- Punta: 10-14h y 18-22h, de lunes a viernes.
- Llano: 8-10h, 14-18h y 22-24h, de lunes a viernes.
- Valle: 0-8h todos los días, y todo el fin de semana.

OJO — limitaciones conocidas:
- "tramos" cuenta un festivo entre semana como día laborable normal, SALVO que configures una
  región (CCAA) de festivos para el CUPS — en ese caso, un festivo cuenta como valle todo el día
  (igual que fin de semana), usando la librería `holidays` (paquete del manifest).
- "pvpc" usa precios reales hora a hora del archivo público de PVPC de ESIOS/REE (ver esios.py) —
  no hace falta ninguna clave, solo la zona (Península/Baleares/Canarias o Ceuta/Melilla)
  configurada en el propio CUPS. Sin precio para alguna hora concreta (p.ej. si aún no se ha
  publicado), esa hora queda sin coste.
- Dos impuestos configurables por CUPS, aplicados EN ORDEN sobre el coste de energía (los tres
  tipos de tarifa) Y el término de potencia — igual que en una factura real, no son un recargo plano
  ni se pueden fusionar en un único porcentaje sin perder precisión si el Estado cambia uno u otro
  por separado (ver issue #3):
    1. `iee_percent` (Impuesto Especial sobre la Electricidad) — base imponible del IVA, NO un
       recargo aditivo aparte.
    2. `iva_percent` — aplicado sobre el resultado de aplicar el IEE, no sobre el precio base.
  `total = base * (1 + iee_percent/100) * (1 + iva_percent/100)`. Sigue faltando el alquiler de
  equipos de medida, así que sigue siendo una estimación, no una factura real. Instalaciones sin
  `iee_percent`/`iva_percent` guardados los reciben con el valor sugerido en el primer arranque tras
  actualizar (ver migration.async_apply_default_tax_percentages) — solo calculan al 0% si el propio
  usuario lo puso así a propósito.
- La compensación por excedentes (`surplus_compensation_value`) NO lleva impuestos aplicados a
  propósito: es una bonificación que se resta de la factura, no un consumo que se grave igual que
  la energía importada.
"""

from __future__ import annotations

import re
from datetime import datetime

import holidays

from .const import TARIFF_FIJA, TARIFF_PVPC, TARIFF_TRAMOS
from .esios import DEFAULT_PVPC_ZONE

PUNTA = "punta"
LLANO = "llano"
VALLE = "valle"

_PUNTA_HOURS = {10, 11, 12, 13, 18, 19, 20, 21}
_LLANO_HOURS = {8, 9, 14, 15, 16, 17, 22, 23}

# Un `holidays.Spain(subdiv=...)` por región, reutilizado entre llamadas — construirlo es barato
# (calcula los años sobre la marcha), pero no hace falta rehacerlo en cada refresco de sensor.
_holiday_calendars: dict[str, holidays.HolidayBase] = {}


def _get_holiday_calendar(region: str | None) -> holidays.HolidayBase | None:
    if not region or region == "none":
        return None
    if region not in _holiday_calendars:
        _holiday_calendars[region] = holidays.Spain(subdiv=region)
    return _holiday_calendars[region]


def hour_period(date_str: str, hour_label: str, holiday_calendar: holidays.HolidayBase | None = None) -> str:
    """`date_str` en formato DD/MM/YYYY, `hour_label` tipo '13 - 14 h' (el que usa el add-on)."""
    try:
        day = datetime.strptime(date_str, "%d/%m/%Y")
    except ValueError:
        return LLANO
    if day.weekday() >= 5 or (holiday_calendar is not None and day.date() in holiday_calendar):  # sáb/dom o festivo
        return VALLE
    match = re.match(r"(\d+)", hour_label or "")
    if not match:
        return LLANO
    hour = int(match.group(1))
    if hour in _PUNTA_HOURS:
        return PUNTA
    if hour in _LLANO_HOURS:
        return LLANO
    return VALLE


def _leading_hour(hour_label: str) -> int | None:
    match = re.match(r"(\d+)", hour_label or "")
    return int(match.group(1)) if match else None


def apply_iee(amount: float, iee_percent: float | None) -> float:
    """`amount` con el Impuesto Especial sobre la Electricidad (IEE) aplicado — 0/None deja el
    importe tal cual. El IEE se aplica ANTES del IVA (es base imponible del IVA, no un recargo
    aparte que se sume al final) — ver `apply_iva` y la cabecera del módulo."""
    return round(amount * (1 + (iee_percent or 0) / 100), 4)


def apply_iva(amount: float, iva_percent: float | None) -> float:
    """`amount` con el IVA (%) aplicado — 0/None deja el importe tal cual (instalaciones sin
    `iva_percent` guardado, ver limitación en la cabecera del módulo). Para aplicar IEE + IVA como
    en una factura real, aplica primero `apply_iee` y pasa ESE resultado aquí, no el importe base."""
    return round(amount * (1 + (iva_percent or 0) / 100), 4)


def cost_breakdown(
    consumption: dict | None,
    prices: dict[str, float],
    holiday_region: str | None = None,
    field: str = "importedKwh",
    iee_percent: float | None = 0,
    iva_percent: float | None = 0,
) -> dict[str, float] | None:
    """kWh y coste, desglosados por periodo, para un consumo con `hourlyByDate`. `field` es la
    clave de cada punto horario a bucketear — "importedKwh" (consumo, por defecto) o "exportedKwh"
    (excedentes: mismo bucketing horario/festivo, con el precio plano de compensación repetido en
    los tres periodos si aplica). None si no hay datos horarios. `holiday_region` (CCAA) es
    opcional — sin ella, los festivos cuentan como día laborable normal (ver limitación conocida
    más arriba). `iee_percent`/`iva_percent` se aplican EN ESE ORDEN a cada coste de periodo (y por
    tanto al total, ver `apply_iee`/`apply_iva`) — déjalos a 0 para excedentes/compensación, que no
    llevan impuestos (ver cabecera)."""
    if not consumption or not consumption.get("hourlyByDate"):
        return None

    holiday_calendar = _get_holiday_calendar(holiday_region)
    kwh_by_period = {PUNTA: 0.0, LLANO: 0.0, VALLE: 0.0}
    for date_str, hours in consumption["hourlyByDate"].items():
        for h in hours:
            period = hour_period(date_str, h.get("hour", ""), holiday_calendar)
            kwh_by_period[period] += h.get(field) or 0

    cost_by_period_sin_impuestos = {period: round(kwh * prices.get(period, 0), 4) for period, kwh in kwh_by_period.items()}
    cost_by_period_con_iee = {period: apply_iee(cost, iee_percent) for period, cost in cost_by_period_sin_impuestos.items()}
    cost_by_period = {period: apply_iva(cost, iva_percent) for period, cost in cost_by_period_con_iee.items()}
    return {
        "kwh_punta": round(kwh_by_period[PUNTA], 3),
        "kwh_llano": round(kwh_by_period[LLANO], 3),
        "kwh_valle": round(kwh_by_period[VALLE], 3),
        "coste_punta": cost_by_period[PUNTA],
        "coste_llano": cost_by_period[LLANO],
        "coste_valle": cost_by_period[VALLE],
        "iee_percent": iee_percent or 0,
        "iva_percent": iva_percent or 0,
        "total_sin_impuestos": round(sum(cost_by_period_sin_impuestos.values()), 4),
        "total_con_iee": round(sum(cost_by_period_con_iee.values()), 4),
        "total": round(sum(cost_by_period.values()), 4),
    }


def pvpc_cost_breakdown(
    consumption: dict | None,
    pvpc_prices: dict[str, float] | None,
    iee_percent: float | None = 0,
    iva_percent: float | None = 0,
) -> dict | None:
    """Coste real hora a hora usando precios PVPC de ESIOS. `pvpc_prices` es un dict
    {"DD/MM/YYYY H": precio_eur_kwh} (ver esios.py). Las horas sin precio disponible (todavía no
    publicado, o clave sin configurar) se ignoran y quedan reflejadas en `horas_sin_precio`.
    `iee_percent`/`iva_percent` se aplican EN ESE ORDEN al total (ver `apply_iee`/`apply_iva`)."""
    if not consumption or not consumption.get("hourlyByDate") or not pvpc_prices:
        return None

    total = 0.0
    total_kwh = 0.0
    missing_hours = 0
    for date_str, hours in consumption["hourlyByDate"].items():
        for h in hours:
            hour = _leading_hour(h.get("hour", ""))
            if hour is None:
                continue
            kwh = h.get("importedKwh") or 0
            price = pvpc_prices.get(f"{date_str} {hour}")
            if price is None:
                if kwh:
                    missing_hours += 1
                continue
            total += kwh * price
            total_kwh += kwh

    if total_kwh == 0 and missing_hours == 0:
        return None
    total_sin_impuestos = round(total, 4)
    total_con_iee = apply_iee(total_sin_impuestos, iee_percent)
    return {
        "kwh_con_precio": round(total_kwh, 3),
        "horas_sin_precio": missing_hours,
        "iee_percent": iee_percent or 0,
        "iva_percent": iva_percent or 0,
        "total_sin_impuestos": total_sin_impuestos,
        "total_con_iee": total_con_iee,
        "total": apply_iva(total_con_iee, iva_percent),
    }


def estimate_energy_cost(
    sp_opts: dict,
    imported_kwh: float | None,
    hourly_source: dict | None,
    pvpc_prices_by_zone: dict[str, dict[str, float]] | None = None,
) -> dict | None:
    """Despacha según `sp_opts["tariff_type"]` (fija/tramos/pvpc, tramos por defecto). Para "pvpc",
    `pvpc_prices_by_zone` es {zona: {"DD/MM/YYYY H": precio}} (ver coordinator.py) y se usa la zona
    configurada en `sp_opts["pvpc_zone"]` de ESTE CUPS. El IEE y el IVA (`sp_opts["iee_percent"]`/
    `["iva_percent"]`) se aplican EN ESE ORDEN en los tres casos — ver `apply_iee`/`apply_iva` y la
    limitación de la cabecera del módulo."""
    tariff_type = sp_opts.get("tariff_type") or TARIFF_TRAMOS
    iee_percent = sp_opts.get("iee_percent") or 0
    iva_percent = sp_opts.get("iva_percent") or 0

    if tariff_type == TARIFF_FIJA:
        price = sp_opts.get("fixed_price") or 0
        if not price or imported_kwh is None:
            return None
        total_sin_impuestos = round(imported_kwh * price, 4)
        total_con_iee = apply_iee(total_sin_impuestos, iee_percent)
        return {
            "tariff_type": TARIFF_FIJA,
            "precio_eur_kwh": price,
            "iee_percent": iee_percent,
            "iva_percent": iva_percent,
            "total_sin_impuestos": total_sin_impuestos,
            "total_con_iee": total_con_iee,
            "total": apply_iva(total_con_iee, iva_percent),
        }

    if tariff_type == TARIFF_PVPC:
        zone = sp_opts.get("pvpc_zone") or DEFAULT_PVPC_ZONE
        zone_prices = (pvpc_prices_by_zone or {}).get(zone)
        breakdown = pvpc_cost_breakdown(hourly_source, zone_prices, iee_percent, iva_percent)
        if breakdown:
            breakdown["tariff_type"] = TARIFF_PVPC
        return breakdown

    # tramos (por defecto)
    prices = {
        PUNTA: sp_opts.get("price_punta") or 0,
        LLANO: sp_opts.get("price_llano") or 0,
        VALLE: sp_opts.get("price_valle") or 0,
    }
    breakdown = cost_breakdown(
        hourly_source, prices, sp_opts.get("holiday_region"), iee_percent=iee_percent, iva_percent=iva_percent
    )
    if breakdown:
        breakdown["tariff_type"] = TARIFF_TRAMOS
    return breakdown


def estimate_cost_as_tariff(
    sp_opts: dict,
    simulated_tariff: str,
    imported_kwh: float | None,
    hourly_source: dict | None,
    pvpc_prices_by_zone: dict[str, dict[str, float]] | None = None,
) -> dict | None:
    """Simula el coste que habría dado ESTE CUPS con una tarifa DISTINTA a la configurada, sobre el
    mismo consumo real — para comparar sin cambiar de tarifa de verdad. Reutiliza los precios ya
    guardados para ese CUPS (fija/tramos siempre están en `sp_opts`, aunque no sean la tarifa
    activa) y, para "pvpc", la zona configurada (o la de por defecto si no se ha elegido ninguna)."""
    return estimate_energy_cost({**sp_opts, "tariff_type": simulated_tariff}, imported_kwh, hourly_source, pvpc_prices_by_zone)


def average_price_per_kwh(cost_total: float | None, imported_kwh: float | None) -> float | None:
    """Precio medio real pagado por kWh (coste total ÷ kWh importados) — útil para comparar contra
    otras ofertas del mercado sin tener que calcularlo a mano."""
    if cost_total is None or not imported_kwh:
        return None
    return round(cost_total / imported_kwh, 5)


def power_cost(sp_opts: dict) -> float:
    """Término de potencia de ESTE CUPS, CON IEE + IVA: kW contratados (punta/valle) × precio
    €/kW/día, con el IEE y el IVA de `sp_opts` aplicados encima EN ESE ORDEN (ver
    `apply_iee`/`apply_iva`) — se factura siempre, sea cual sea la tarifa de energía elegida
    (fija/tramos/pvpc)."""
    punta_kw = sp_opts.get("contracted_power_punta_kw") or 0
    valle_kw = sp_opts.get("contracted_power_valle_kw") or 0
    price_punta = sp_opts.get("price_power_punta") or 0
    price_valle = sp_opts.get("price_power_valle") or 0
    total_sin_impuestos = punta_kw * price_punta + valle_kw * price_valle
    total_con_iee = apply_iee(total_sin_impuestos, sp_opts.get("iee_percent") or 0)
    return apply_iva(total_con_iee, sp_opts.get("iva_percent") or 0)


def self_consumption_ratio(imported_kwh: float | None, exported_kwh: float | None) -> float | None:
    """Grado de AUTOSUFICIENCIA aproximado (%), calculado solo con importado/exportado del
    contador de e-distribución — no con generación solar real (e-distribución no la reporta, solo
    ve el intercambio con la red). Es una aproximación, pero con los casos límite correctos: si no
    importas nada de la red (imported=0), sale 100% (autosuficiente del todo, sin depender de la
    red); si nunca exportas nada (sin placas/batería), sale 0%. Los valores intermedios son solo
    una estimación (no equivalen exactamente a "% de tu generación autoconsumida", que requeriría
    el dato de generación real, no solo el del contador)."""
    if imported_kwh is None or exported_kwh is None:
        return None
    total = imported_kwh + exported_kwh
    if total <= 0:
        return None
    return round(100 * exported_kwh / total, 1)


def surplus_compensation_value(sp_opts: dict, exported_kwh: float | None) -> float | None:
    """Cuánto se compensaría por los kWh exportados, si el suministro tiene activada la
    compensación de excedentes con un precio configurado. Sin IEE ni IVA a propósito — ver cabecera
    del módulo."""
    if not sp_opts.get("surplus_compensation"):
        return None
    price = sp_opts.get("surplus_price") or 0
    if not price or exported_kwh is None:
        return None
    return round(exported_kwh * price, 4)


def monthly_summary_csv(sp_opts: dict, month_data: dict | None, pvpc_prices_by_zone: dict[str, dict[str, float]] | None = None) -> str:
    """Resumen del mes en texto CSV (concepto,valor): coste de energía (desglosado por periodo si
    la tarifa lo permite), término de potencia, compensación de excedentes si aplica, y un total
    estimado — para descargar/analizar fuera de Home Assistant. Es una ESTIMACIÓN hecha con los
    precios que tengas configurados AHORA (no una factura real, ver limitaciones en la cabecera del
    módulo). `coste_energia`/`termino_potencia`/`total_estimado` llevan el IEE y el IVA configurados
    aplicados EN ESE ORDEN (0% cada uno si no se han configurado); `coste_energia_sin_impuestos` y
    `coste_energia_con_iee` se incluyen aparte para quien quiera el desglose completo."""
    imported_kwh = (month_data or {}).get("totalImportedKwh")
    exported_kwh = (month_data or {}).get("totalExportedKwh")
    breakdown = estimate_energy_cost(sp_opts, imported_kwh, month_data, pvpc_prices_by_zone) or {}
    power = power_cost(sp_opts)
    surplus = surplus_compensation_value(sp_opts, exported_kwh)

    rows = ["concepto,valor"]
    rows.append(f"tarifa,{sp_opts.get('tariff_type') or TARIFF_TRAMOS}")
    rows.append(f"iee_percent,{sp_opts.get('iee_percent') or 0}")
    rows.append(f"iva_percent,{sp_opts.get('iva_percent') or 0}")
    rows.append(f"kwh_importados,{imported_kwh if imported_kwh is not None else ''}")
    rows.append(f"kwh_exportados,{exported_kwh if exported_kwh is not None else ''}")
    for key in ("kwh_punta", "kwh_llano", "kwh_valle", "coste_punta", "coste_llano", "coste_valle", "kwh_con_precio", "horas_sin_precio"):
        if key in breakdown:
            rows.append(f"{key},{breakdown[key]}")
    rows.append(f"coste_energia_sin_impuestos,{breakdown.get('total_sin_impuestos', 0)}")
    rows.append(f"coste_energia_con_iee,{breakdown.get('total_con_iee', 0)}")
    rows.append(f"coste_energia,{breakdown.get('total', 0)}")
    rows.append(f"termino_potencia,{power}")
    if surplus is not None:
        rows.append(f"compensacion_excedentes,{surplus}")
    total = round((breakdown.get("total") or 0) + power - (surplus or 0), 4)
    rows.append(f"total_estimado,{total}")
    return "\n".join(rows)
