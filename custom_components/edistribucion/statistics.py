"""Relleno de histórico en el Dashboard de Energía usando la Statistics API del recorder.

Se ejecuta al configurar cada suministro Y una vez al día en cada ciclo del coordinator (ver
coordinator._async_backfill_statistics_if_needed), usando el consumo mensual ya disponible, para
que el Dashboard de Energía no empiece con el gráfico vacío y los meses nuevos se rellenen solos
aunque Home Assistant lleve semanas sin reiniciarse. Es idempotente (mismo statistic_id + misma
fecha se sobrescribe, no se duplica), así que es seguro llamarlo tantas veces como haga falta.

Esta es la parte más "avanzada" de toda la integración (la Statistics API del recorder no es
trivial y ha cambiado de forma sutil entre versiones de Home Assistant) — por eso todo aquí está
envuelto en manejo de errores generoso: si falla, se registra un aviso y la integración sigue
funcionando con normalidad (solo te quedas sin el relleno retroactivo, no sin los sensores).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _parse_day(date_str: str) -> datetime:
    """'DD/MM/YYYY' -> medianoche UTC de ese día (los external statistics quieren tz-aware)."""
    naive_midnight = datetime.strptime(date_str, "%d/%m/%Y").replace(hour=0, minute=0, second=0, microsecond=0)
    return dt_util.as_utc(dt_util.as_local(naive_midnight))


def _leading_hour(hour_label: str) -> int | None:
    """'13 - 14 h' -> 13 (el formato de hora que usa el add-on en hourlyByDate)."""
    match = re.match(r"(\d+)", hour_label or "")
    return int(match.group(1)) if match else None


def _parse_hour(date_str: str, hour: int) -> datetime:
    """'DD/MM/YYYY' + hora (0-23) -> ese instante en UTC, tz-aware."""
    naive = datetime.strptime(date_str, "%d/%m/%Y").replace(hour=hour, minute=0, second=0, microsecond=0)
    return dt_util.as_utc(dt_util.as_local(naive))


def _hourly_points(month_data: dict, field: str) -> list[tuple[datetime, float]] | None:
    """Un punto por HORA a partir de `hourlyByDate` — más granular que un punto por día. None si
    no hay datos horarios (para que el llamador haga fallback a `_daily_points`)."""
    hourly = month_data.get("hourlyByDate")
    if not hourly:
        return None
    points: list[tuple[datetime, float]] = []
    for date_str, hours in hourly.items():
        for h in hours:
            hour = _leading_hour(h.get("hour", ""))
            if hour is None:
                continue
            points.append((_parse_hour(date_str, hour), h.get(field) or 0.0))
    return points or None


def _daily_points(month_data: dict, field: str) -> list[tuple[datetime, float]] | None:
    """Un punto por DÍA a partir de `dailyTotals` — usado si no hay `hourlyByDate` disponible."""
    days = month_data.get("dailyTotals")
    if not days:
        return None
    return [(_parse_day(day["date"]), day.get(field) or 0.0) for day in days]


async def async_backfill_energy_statistics(hass: HomeAssistant, cups: str, month_data: dict | None) -> None:
    if not month_data or not (month_data.get("hourlyByDate") or month_data.get("dailyTotals")):
        return
    if "recorder" not in hass.config.components:
        return

    try:
        from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
        from homeassistant.components.recorder.statistics import async_add_external_statistics
    except ImportError as err:  # el recorder no está disponible en esta instalación
        _LOGGER.debug("Recorder/Statistics API no disponible, sin relleno de histórico: %s", err)
        return

    for flow, field, label in (
        ("imported", "importedKwh", "importada"),
        ("exported", "exportedKwh", "exportada"),
    ):
        try:
            points = _hourly_points(month_data, field) or _daily_points(month_data, field)
            if not points:
                continue
            points.sort(key=lambda p: p[0])
            running_total = 0.0
            stats: list[StatisticData] = []
            for start, value in points:
                running_total += value
                stats.append(StatisticData(start=start, sum=running_total, state=value))

            statistic_id = f"{DOMAIN}:{cups.lower()}_{flow}_energy"
            metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                name=f"e-distribución {cups} — energía {label}",
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            )
            async_add_external_statistics(hass, metadata, stats)
        except Exception as err:  # noqa: BLE001 — un fallo aquí no debe romper el arranque de la integración
            _LOGGER.warning("No se pudo rellenar el histórico de %s (%s): %s", cups, flow, err)
