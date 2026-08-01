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
            "max_power_demand": bundle.get("max_power_demand"),
            # power_excess_detected() (costs.py) necesita max_power_demand Y contract juntos — sin
            # este, un diagnóstico exportado no trae lo necesario para depurar ese sensor sin acceso
            # directo al log. No tiene datos tan sensibles como la dirección (código de contrato,
            # potencias, comercializadora, tarifa — ya visibles igual como atributos del sensor de
            # potencia contratada).
            "contract": bundle.get("contract"),
        }

    return {
        "entry": {"host": entry.data.get("host"), "port": entry.data.get("port")},
        "options": entry.options,
        "last_update_success": coordinator.last_update_success,
        "last_success_time": str(coordinator.last_success_time) if coordinator.last_success_time else None,
        "supply_points": supplies,
    }
