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


def _carry_over_sum(last_saved: tuple[datetime, float] | None, first_point_start: datetime) -> float:
    """Con qué `sum` arrancar el `running_total` de ESTA llamada.

    `month_data` (ver coordinator.py) es siempre el mes EN CURSO — nunca el histórico completo —
    así que reconstruir el `sum` desde 0 en cada llamada solo es correcto la primera vez que se
    crea el statistic_id, o cuando se está reescribiendo (idempotente) el mismo mes de siempre. El
    caso que NO es correcto: el backfill ahora se repite una vez al día (ver
    coordinator._async_backfill_statistics_if_needed) y, al cambiar de mes, `month_data` pasa a
    contener solo los días/horas del mes nuevo — si volviéramos a arrancar en 0 ahí, el `sum` ya
    guardado del mes anterior (que puede ser un número grande) caería en picado de un día para
    otro, rompiendo la monotonía que exige el recorder para las long-term statistics.

    Por eso: si lo último que hay guardado para este statistic_id es ANTERIOR al primer punto que
    vamos a escribir ahora, es que representa historia real que no vamos a tocar (el cierre del mes
    anterior) y hay que usarlo como base. Si en cambio cae DENTRO del rango que vamos a reescribir
    (mismo mes, ya visto en una llamada anterior de hoy), no hay que sumarlo aparte — ya está
    contado en `month_data`, así que se arranca en 0 como siempre (evita contar dos veces)."""
    if last_saved is not None and last_saved[0] < first_point_start:
        return last_saved[1]
    return 0.0


async def _async_last_saved_stat(hass: HomeAssistant, statistic_id: str) -> tuple[datetime, float] | None:
    """(inicio, sum) de la última estadística ya guardada para este statistic_id, o None si no hay
    ninguna todavía (primera vez que se crea). Es una consulta BLOQUEANTE a la base de datos del
    recorder, así que se ejecuta en su executor, nunca en el bucle de eventos."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import get_last_statistics

    def _query() -> tuple[datetime, float] | None:
        result = get_last_statistics(hass, 1, statistic_id, True, {"sum"})
        rows = result.get(statistic_id)
        if not rows:
            return None
        return rows[0]["start"], rows[0]["sum"]

    return await get_instance(hass).async_add_executor_job(_query)


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

            statistic_id = f"{DOMAIN}:{cups.lower()}_{flow}_energy"
            try:
                last_saved = await _async_last_saved_stat(hass, statistic_id)
            except Exception as err:  # noqa: BLE001 — sin poder leerlo, se asume "sin dato previo" (0.0)
                _LOGGER.warning("No se pudo leer el último sum guardado de %s (%s): %s", cups, flow, err)
                last_saved = None
            running_total = _carry_over_sum(last_saved, points[0][0])

            stats: list[StatisticData] = []
            for start, value in points:
                running_total += value
                stats.append(StatisticData(start=start, sum=running_total, state=value))

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
