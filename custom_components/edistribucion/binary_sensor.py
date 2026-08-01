"""Binary sensors: ¿pudo la última actualización hablar con el add-on sin error?"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EdistribucionCoordinator
from .device import hub_device_info


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: EdistribucionCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EdistribucionConnectivitySensor(coordinator, entry)])


class EdistribucionConnectivitySensor(CoordinatorEntity[EdistribucionCoordinator], BinarySensorEntity):
    """ON = la última actualización pudo hablar con el add-on sin error."""

    _attr_has_entity_name = True
    entity_description = BinarySensorEntityDescription(
        key="connected",
        translation_key="connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
    )

    def __init__(self, coordinator: EdistribucionCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_connected"
        self._attr_device_info = hub_device_info(entry.entry_id)

    @property
    def is_on(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def available(self) -> bool:
        # Este sensor en concreto SIEMPRE está disponible: es precisamente el que informa de si
        # hay conexión o no (si fuera "no disponible" cuando falla, no serviría para eso).
        return True
