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


def cost_breakdown(consumption: dict | None, prices: dict[str, float], holiday_region: str | None = None) -> dict[str, float] | None:
    """kWh importados y coste, desglosados por periodo, para un consumo con `hourlyByDate`.
    None si no hay datos horarios. `holiday_region` (CCAA) es opcional — sin ella, los festivos
    cuentan como día laborable normal (ver limitación conocida más arriba)."""
    if not consumption or not consumption.get("hourlyByDate"):
        return None

    holiday_calendar = _get_holiday_calendar(holiday_region)
    kwh_by_period = {PUNTA: 0.0, LLANO: 0.0, VALLE: 0.0}
    for date_str, hours in consumption["hourlyByDate"].items():
        for h in hours:
            period = hour_period(date_str, h.get("hour", ""), holiday_calendar)
            kwh_by_period[period] += h.get("importedKwh") or 0

    cost_by_period = {period: round(kwh * prices.get(period, 0), 4) for period, kwh in kwh_by_period.items()}
    return {
        "kwh_punta": round(kwh_by_period[PUNTA], 3),
        "kwh_llano": round(kwh_by_period[LLANO], 3),
        "kwh_valle": round(kwh_by_period[VALLE], 3),
        "coste_punta": cost_by_period[PUNTA],
        "coste_llano": cost_by_period[LLANO],
        "coste_valle": cost_by_period[VALLE],
        "total": round(sum(cost_by_period.values()), 4),
    }


def pvpc_cost_breakdown(consumption: dict | None, pvpc_prices: dict[str, float] | None) -> dict | None:
    """Coste real hora a hora usando precios PVPC de ESIOS. `pvpc_prices` es un dict
    {"DD/MM/YYYY H": precio_eur_kwh} (ver esios.py). Las horas sin precio disponible (todavía no
    publicado, o clave sin configurar) se ignoran y quedan reflejadas en `horas_sin_precio`."""
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
    return {
        "kwh_con_precio": round(total_kwh, 3),
        "horas_sin_precio": missing_hours,
        "total": round(total, 4),
    }


def estimate_energy_cost(
    sp_opts: dict,
    imported_kwh: float | None,
    hourly_source: dict | None,
    pvpc_prices_by_zone: dict[str, dict[str, float]] | None = None,
) -> dict | None:
    """Despacha según `sp_opts["tariff_type"]` (fija/tramos/pvpc, tramos por defecto). Para "pvpc",
    `pvpc_prices_by_zone` es {zona: {"DD/MM/YYYY H": precio}} (ver coordinator.py) y se usa la zona
    configurada en `sp_opts["pvpc_zone"]` de ESTE CUPS."""
    tariff_type = sp_opts.get("tariff_type") or TARIFF_TRAMOS

    if tariff_type == TARIFF_FIJA:
        price = sp_opts.get("fixed_price") or 0
        if not price or imported_kwh is None:
            return None
        return {"tariff_type": TARIFF_FIJA, "precio_eur_kwh": price, "total": round(imported_kwh * price, 4)}

    if tariff_type == TARIFF_PVPC:
        zone = sp_opts.get("pvpc_zone") or DEFAULT_PVPC_ZONE
        zone_prices = (pvpc_prices_by_zone or {}).get(zone)
        breakdown = pvpc_cost_breakdown(hourly_source, zone_prices)
        if breakdown:
            breakdown["tariff_type"] = TARIFF_PVPC
        return breakdown

    # tramos (por defecto)
    prices = {
        PUNTA: sp_opts.get("price_punta") or 0,
        LLANO: sp_opts.get("price_llano") or 0,
        VALLE: sp_opts.get("price_valle") or 0,
    }
    breakdown = cost_breakdown(hourly_source, prices, sp_opts.get("holiday_region"))
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
    """Término de potencia de ESTE CUPS: kW contratados (punta/valle) × precio €/kW/día — se
    factura siempre, sea cual sea la tarifa de energía elegida (fija/tramos/pvpc)."""
    punta_kw = sp_opts.get("contracted_power_punta_kw") or 0
    valle_kw = sp_opts.get("contracted_power_valle_kw") or 0
    price_punta = sp_opts.get("price_power_punta") or 0
    price_valle = sp_opts.get("price_power_valle") or 0
    return round(punta_kw * price_punta + valle_kw * price_valle, 4)


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
    compensación de excedentes con un precio configurado."""
    if not sp_opts.get("surplus_compensation"):
        return None
    price = sp_opts.get("surplus_price") or 0
    if not price or exported_kwh is None:
        return None
    return round(exported_kwh * price, 4)


def max_power_reported(max_power_demand: dict | None) -> float | None:
    """Máximo real reportado por e-distribución en todo el periodo devuelto (no solo el último
    punto, ver `maxValue`) — si el add-on no trae `maxValue`, se calcula como el mayor `valueKw` de
    todos los `points`."""
    if not max_power_demand:
        return None
    max_value = max_power_demand.get("maxValue")
    if max_value is not None:
        return max_value
    points = max_power_demand.get("points") or []
    values = [p.get("valueKw") for p in points if p.get("valueKw") is not None]
    return max(values) if values else None


def power_excess_detected(max_power_demand: dict | None, contract: dict | None) -> bool | None:
    """¿La potencia máxima real demandada ha superado la potencia contratada? None si no hay datos
    suficientes (sin telegestión, o sin lectura de contrato) para saberlo.

    Si `periods` distingue con claridad punta/valle (ver `max_power_by_period`), se compara CADA
    periodo contra su propio límite contratado — comparar solo contra el mayor de los dos, cuando
    punta y valle contratados difieren, daría un falso negativo si el exceso ocurrió justo en el
    periodo de menor potencia contratada. Si no se puede distinguir, cae al máximo global contra el
    mayor de punta/valle contratados (comportamiento anterior)."""
    if not contract:
        return None

    by_period = max_power_by_period(max_power_demand)
    punta_label = next((label for label in by_period if "punta" in label.lower()), None)
    valle_label = next((label for label in by_period if "valle" in label.lower()), None)
    punta_limit = contract.get("contractedPowerPuntaKw") or 0
    valle_limit = contract.get("contractedPowerValleKw") or 0

    if punta_label and valle_label and (punta_limit or valle_limit):
        exceeded_punta = bool(punta_limit) and by_period[punta_label] > punta_limit
        exceeded_valle = bool(valle_limit) and by_period[valle_label] > valle_limit
        return exceeded_punta or exceeded_valle

    reported = max_power_reported(max_power_demand)
    if reported is None:
        return None
    limit = max(punta_limit, valle_limit)
    if not limit:
        return None
    return reported > limit


def _extract_period_value(period_entry) -> tuple[str, float] | tuple[None, None]:
    """Intenta sacar (nombre_periodo, valor_kw) de una entrada de "periods" sin asumir un nombre de
    clave concreto (el add-on no documenta el formato exacto) — usa la primera clave con valor
    numérico como el valor, y la primera clave de texto como la etiqueta."""
    if not isinstance(period_entry, dict):
        return None, None
    label = None
    value = None
    for key, val in period_entry.items():
        if isinstance(val, (int, float)) and value is None:
            value = float(val)
        elif isinstance(val, str) and label is None:
            label = val
    return (label, value) if label is not None and value is not None else (None, None)


def max_power_by_period(max_power_demand: dict | None) -> dict[str, float]:
    """Máximo por periodo (punta/valle...) si `periods` lo distingue, agregando el mayor valor de
    CADA periodo a lo largo de todos los puntos — no se asume una forma concreta de `periods` (el
    add-on no la documenta): admite tanto un dict {periodo: valor} como una lista de entradas con
    alguna clave numérica y otra de texto. Vacío si no hay nada reconocible."""
    if not max_power_demand:
        return {}
    result: dict[str, float] = {}
    for point in max_power_demand.get("points") or []:
        periods = point.get("periods")
        if isinstance(periods, dict):
            for label, val in periods.items():
                if isinstance(val, (int, float)):
                    result[label] = max(result.get(label, 0.0), float(val))
        elif isinstance(periods, list):
            for entry in periods:
                label, val = _extract_period_value(entry)
                if label is not None:
                    result[label] = max(result.get(label, 0.0), val)
    return result
