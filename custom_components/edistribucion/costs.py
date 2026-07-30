"""Coste estimado por franja horaria (punta/llano/valle), calculado hora a hora a partir del
consumo real (`hourlyByDate`) — no una media diaria, así que un mismo kWh cuenta distinto según a
qué hora se consumió de verdad.

Horario usado (2.0TD peninsular, el más común en residencial):
- Punta: 10-14h y 18-22h, de lunes a viernes.
- Llano: 8-10h, 14-18h y 22-24h, de lunes a viernes.
- Valle: 0-8h todos los días, y todo el fin de semana.

OJO — limitación conocida: no tiene en cuenta festivos (que cuentan como valle todo el día en la
tarifa real). Es una aproximación pensada para tener una idea, no para cuadrar con la factura.
"""

from __future__ import annotations

import re
from datetime import datetime

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
