"""Coste estimado de energía, según el tipo de tarifa configurado por CUPS (fija/tramos/pvpc), y
compensación por excedentes exportados.

Horario de tramos usado (2.0TD peninsular, el más común en residencial):
- Punta: 10-14h y 18-22h, de lunes a viernes.
- Llano: 8-10h, 14-18h y 22-24h, de lunes a viernes.
- Valle: 0-8h todos los días, y todo el fin de semana.

OJO — limitaciones conocidas:
- "tramos" no tiene en cuenta festivos (cuentan como valle todo el día en la tarifa real).
- "pvpc" usa precios reales hora a hora de la API de ESIOS/REE (ver esios.py) — necesita una clave
  de ESIOS configurada a nivel de la integración. Sin esa clave (o sin precio para alguna hora
  concreta, p.ej. si aún no se ha publicado), esa hora queda sin coste.
"""

from __future__ import annotations

import re
from datetime import datetime

from .const import TARIFF_FIJA, TARIFF_PVPC, TARIFF_TRAMOS

PUNTA = "punta"
LLANO = "llano"
VALLE = "valle"

_PUNTA_HOURS = {10, 11, 12, 13, 18, 19, 20, 21}
_LLANO_HOURS = {8, 9, 14, 15, 16, 17, 22, 23}


def hour_period(date_str: str, hour_label: str) -> str:
    """`date_str` en formato DD/MM/YYYY, `hour_label` tipo '13 - 14 h' (el que usa el add-on)."""
    try:
        day = datetime.strptime(date_str, "%d/%m/%Y")
    except ValueError:
        return LLANO
    if day.weekday() >= 5:  # sábado=5, domingo=6
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


def cost_breakdown(consumption: dict | None, prices: dict[str, float]) -> dict[str, float] | None:
    """kWh importados y coste, desglosados por periodo, para un consumo con `hourlyByDate`.
    None si no hay datos horarios."""
    if not consumption or not consumption.get("hourlyByDate"):
        return None

    kwh_by_period = {PUNTA: 0.0, LLANO: 0.0, VALLE: 0.0}
    for date_str, hours in consumption["hourlyByDate"].items():
        for h in hours:
            period = hour_period(date_str, h.get("hour", ""))
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
    sp_opts: dict, imported_kwh: float | None, hourly_source: dict | None, pvpc_prices: dict[str, float] | None = None
) -> dict | None:
    """Despacha según `sp_opts["tariff_type"]` (fija/tramos/pvpc, tramos por defecto)."""
    tariff_type = sp_opts.get("tariff_type") or TARIFF_TRAMOS

    if tariff_type == TARIFF_FIJA:
        price = sp_opts.get("fixed_price") or 0
        if not price or imported_kwh is None:
            return None
        return {"tariff_type": TARIFF_FIJA, "precio_eur_kwh": price, "total": round(imported_kwh * price, 4)}

    if tariff_type == TARIFF_PVPC:
        breakdown = pvpc_cost_breakdown(hourly_source, pvpc_prices)
        if breakdown:
            breakdown["tariff_type"] = TARIFF_PVPC
        return breakdown

    # tramos (por defecto)
    prices = {
        PUNTA: sp_opts.get("price_punta") or 0,
        LLANO: sp_opts.get("price_llano") or 0,
        VALLE: sp_opts.get("price_valle") or 0,
    }
    breakdown = cost_breakdown(hourly_source, prices)
    if breakdown:
        breakdown["tariff_type"] = TARIFF_TRAMOS
    return breakdown


def surplus_compensation_value(sp_opts: dict, exported_kwh: float | None) -> float | None:
    """Cuánto se compensaría por los kWh exportados, si el suministro tiene activada la
    compensación de excedentes con un precio configurado."""
    if not sp_opts.get("surplus_compensation"):
        return None
    price = sp_opts.get("surplus_price") or 0
    if not price or exported_kwh is None:
        return None
    return round(exported_kwh * price, 4)
