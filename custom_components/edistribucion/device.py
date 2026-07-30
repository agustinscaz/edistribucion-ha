"""DeviceInfo compartido: el dispositivo 'hub' (no ligado a un CUPS concreto), donde viven el
binary_sensor de conexión, los botones y el sensor de última actualización."""

from __future__ import annotations

from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN


def hub_device_info(entry_id: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name="e-distribución (add-on)",
        manufacturer="e-distribución (no oficial)",
    )
