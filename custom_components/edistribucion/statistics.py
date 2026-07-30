"""Relleno de histórico en el Dashboard de Energía usando la Statistics API del recorder.

Se ejecuta una vez al configurar cada suministro (usando el consumo mensual ya disponible), para
que el Dashboard de Energía no empiece con el gráfico vacío. Es idempotente (mismo statistic_id +
misma fecha se sobrescribe, no se duplica), así que es seguro llamarlo en cada arranque.

Esta es la parte más "avanzada" de toda la integración (la Statistics API del recorder no es
trivial y ha cambiado de forma sutil entre versiones de Home Assistant) — por eso todo aquí está
envuelto en manejo de errores generoso: si falla, se registra un aviso y la integración sigue
funcionando con normalidad (solo te quedas sin el relleno retroactivo, no sin los sensores).
"""

from __future__ import annotations

import logging
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


async def async_backfill_energy_statistics(hass: HomeAssistant, cups: str, month_data: dict | None) -> None:
    if not month_data or not month_data.get("dailyTotals"):
        return
    if "recorder" not in hass.config.components:
        return

    try:
        from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
        from homeassistant.components.recorder.statistics import async_add_external_statistics
    except ImportError as err:  # el recorder no está disponible en esta instalación
        _LOGGER.debug("Recorder/Statistics API no disponible, sin relleno de histórico: %s", err)
        return

    days = sorted(month_data["dailyTotals"], key=lambda d: _parse_day(d["date"]))

    for flow, field, label in (
        ("imported", "importedKwh", "importada"),
        ("exported", "exportedKwh", "exportada"),
    ):
        try:
            statistic_id = f"{DOMAIN}:{cups.lower()}_{flow}_energy"
            running_total = 0.0
            stats: list[StatisticData] = []
            for day in days:
                running_total += day.get(field) or 0.0
                stats.append(StatisticData(start=_parse_day(day["date"]), sum=running_total, state=day.get(field) or 0.0))
            if not stats:
                continue
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
