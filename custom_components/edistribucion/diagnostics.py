"""Diagnósticos descargables desde la propia UI de Home Assistant (Ajustes → Dispositivos y
servicios → e-distribución → Descargar diagnósticos). Se redacta la dirección postal por ser el
dato más identificable; el resto (CUPS, consumos, potencia) se incluye tal cual para que sea útil
al depurar un problema."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import EdistribucionCoordinator


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    coordinator: EdistribucionCoordinator = hass.data[DOMAIN][entry.entry_id]

    supplies: dict[str, Any] = {}
    for cont_id, bundle in coordinator.data.items():
        sp = dict(bundle.get("supply_point") or {})
        sp.pop("address", None)
        supplies[cont_id] = {
            "supply_point": sp,
            "consumption": bundle.get("consumption"),
            "week": bundle.get("week"),
            "month": bundle.get("month"),
            # No tiene datos tan sensibles como la dirección (código de contrato, potencias,
            # comercializadora, tarifa — ya visibles igual como atributos del sensor de potencia
            # contratada) — útil para depurar ESE sensor sin acceso directo al log.
            "contract": bundle.get("contract"),
            # Caché de acumulado del año (issue #29): antes de esto, depurar un total anual
            # implausible (ver issue #25) exigía activar `logger.set_level: debug` y forzar un
            # reload a mano solo para ver qué tenía cacheado el coordinator — ahora está en el
            # propio volcado de diagnósticos, sin tocar la configuración de logging de nadie.
            "year_to_date_completed_months": coordinator.year_to_date_completed_months(cont_id),
            "year_to_date_month_details": coordinator.year_to_date_month_details(cont_id),
        }

    return {
        "entry": {"host": entry.data.get("host"), "port": entry.data.get("port")},
        "options": entry.options,
        "last_update_success": coordinator.last_update_success,
        "last_success_time": str(coordinator.last_success_time) if coordinator.last_success_time else None,
        "supply_points": supplies,
    }
