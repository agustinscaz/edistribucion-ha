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
from datetime import datetime, timedelta

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


def months_back(base: datetime, n: int) -> list[datetime]:
    """Los `n` meses hasta `base` (incluido su propio mes), como el día 1 de cada uno, en orden
    CRONOLÓGICO (el más antiguo primero) — usado por el servicio de relleno de histórico completo
    (ver __init__.py). El orden importa: para que el arrastre de sum entre meses (ver
    `_carry_over_sum`) quede bien encadenado, hay que rellenar los meses de más antiguo a más
    reciente, nunca al revés ni salteados."""
    results = []
    year, month = base.year, base.month
    for _ in range(n):
        results.append(base.replace(year=year, month=month, day=1))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    results.reverse()
    return results


def _carry_over_sum(last_saved: tuple[datetime, float] | None, first_point_start: datetime) -> float:
    """Con qué `sum` arrancar el `running_total` de ESTA llamada.

    `month_data` (ver coordinator.py) es siempre el mes EN CURSO — nunca el histórico completo —
    así que reconstruir el `sum` desde 0 en cada llamada solo es correcto la primera vez que se
    crea el statistic_id. El resto de veces hay que arrastrar el `sum` acumulado hasta el cierre
    del mes anterior (`last_saved`, ver `_async_last_saved_stat_before` — busca el último punto
    ESTRICTAMENTE ANTERIOR al primero que se va a (re)escribir ahora, no "lo último guardado en
    general": a partir del segundo día del mes en curso, "lo último guardado en general" ya sería
    una hora de este mismo mes, no el cierre del anterior — usarlo como ancla duplicaría el
    arrastre en cada re-ejecución dentro del mismo mes).

    Como `last_saved` ya viene filtrado a "estrictamente anterior" por la propia consulta, esta
    comprobación es solo un cinturón de seguridad, no la lógica principal."""
    if last_saved is not None and last_saved[0] < first_point_start:
        return last_saved[1]
    return 0.0


_RECENT_WINDOW = timedelta(days=40)  # más que un mes de margen — ver _async_last_saved_stat_before

# statistic_id para los que ya se avisó (una vez, ver async_backfill_energy_statistics) de que el
# arrastre de sum entre meses aplicó — para poder confirmar desde el log que funcionó en un cambio
# de mes real, sin repetir el mismo aviso en cada ciclo diario dentro del mismo mes. Se reinicia en
# cada arranque de Home Assistant (a propósito: así se reconfirma tras un reinicio a mitad de mes).
_carry_over_logged: set[str] = set()


async def _query_last_before(hass: HomeAssistant, statistic_id: str, start_time: datetime, before: datetime) -> tuple[datetime, float] | None:
    """(inicio, sum) del último punto guardado en [start_time, before), o None si no hay ninguno.
    Consulta BLOQUEANTE a la base de datos del recorder, se ejecuta en su executor."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    def _query() -> tuple[datetime, float] | None:
        result = statistics_during_period(
            hass,
            start_time=start_time,
            end_time=before,
            statistic_ids={statistic_id},
            period="hour",
            units=None,
            types={"sum"},
        )
        rows = result.get(statistic_id)
        if not rows:
            return None
        last = rows[-1]  # ordenados ascendente por start — el último es el más reciente antes de `before`
        start = last["start"]
        if isinstance(start, (int, float)):  # timestamp UNIX crudo, no datetime (según versión de HA)
            start = dt_util.utc_from_timestamp(start)
        return start, last["sum"]

    return await get_instance(hass).async_add_executor_job(_query)


async def _async_last_saved_stat_before(hass: HomeAssistant, statistic_id: str, before: datetime) -> tuple[datetime, float] | None:
    """(inicio, sum) del último punto ya guardado ESTRICTAMENTE ANTERIOR a `before`, o None si no
    hay ninguno (primera vez que se crea el statistic_id).

    A propósito NO se usa `get_last_statistics` (da "el último punto guardado en general", sin
    importar cuándo — bug real: a partir del segundo día del mes en curso ese "último punto" ya es
    una hora de ESTE MISMO MES, reescrita el día anterior, no el cierre del mes pasado; usarlo como
    ancla duplicaba el arrastre en cada re-ejecución del mismo mes). `statistics_during_period` sí
    acepta `end_time` (exclusivo) para acotar la búsqueda a "antes de donde voy a escribir ahora",
    sin importar qué día del mes sea este run.

    Se busca primero en los últimos `_RECENT_WINDOW` (margen de sobra sobre un mes) en vez de desde
    el principio de los tiempos — con años de histórico externo por hora, traer TODO el histórico
    solo para quedarse con el último punto es un escaneo/transferencia innecesaria en el caso normal
    (el punto que buscamos casi siempre es de ayer). Si esa ventana reciente no tiene nada (p.ej.
    Home Assistant estuvo apagado semanas seguidas a caballo de un cambio de mes, o incluso meses),
    se repite sin límite de ventana — sin este fallback, un hueco así perdería en silencio el
    arrastre real en vez de solo tardar un poco más esa vez."""
    recent = await _query_last_before(hass, statistic_id, before - _RECENT_WINDOW, before)
    if recent is not None:
        return recent
    return await _query_last_before(hass, statistic_id, dt_util.utc_from_timestamp(0), before)


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

    try:
        # `has_mean` está deprecado a favor de `mean_type` (HA 2026.11) — pero mean_type no existe
        # en versiones más antiguas que las mínimas soportadas (ver hacs.json), así que se usa si
        # está disponible y si no se cae al campo viejo.
        from homeassistant.components.recorder.models import StatisticMeanType

        mean_type_kwargs: dict = {"mean_type": StatisticMeanType.NONE}
    except ImportError:
        mean_type_kwargs = {"has_mean": False}

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
                last_saved = await _async_last_saved_stat_before(hass, statistic_id, points[0][0])
            except Exception as err:  # noqa: BLE001 — sin poder leerlo, se asume "sin dato previo" (0.0)
                _LOGGER.warning("No se pudo leer el último sum guardado de %s (%s): %s", cups, flow, err)
                last_saved = None
            running_total = _carry_over_sum(last_saved, points[0][0])

            if last_saved is not None and statistic_id not in _carry_over_logged:
                _LOGGER.info(
                    "Arrastrando sum=%.3f de %s (%s) desde antes de %s",
                    last_saved[1],
                    cups,
                    flow,
                    points[0][0].isoformat(),
                )
                _carry_over_logged.add(statistic_id)

            stats: list[StatisticData] = []
            for start, value in points:
                running_total += value
                stats.append(StatisticData(start=start, sum=running_total, state=value))

            metadata = StatisticMetaData(
                **mean_type_kwargs,
                has_sum=True,
                name=f"e-distribución {cups} — energía {label}",
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            )
            async_add_external_statistics(hass, metadata, stats)
        except Exception as err:  # noqa: BLE001 — un fallo aquí no debe romper el arranque de la integración
            _LOGGER.warning("No se pudo rellenar el histórico de %s (%s): %s", cups, flow, err)
