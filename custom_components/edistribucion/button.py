"""Botones: actualizar datos ahora, y forzar una reconexión (login fresco) en el add-on."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EdistribucionCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: EdistribucionCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            EdistribucionRefreshButton(coordinator, entry),
            EdistribucionReloginButton(coordinator, entry),
        ]
    )


class _EdistribucionHubButton(CoordinatorEntity[EdistribucionCoordinator], ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: EdistribucionCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="e-distribución (add-on)",
            manufacturer="e-distribución (no oficial)",
        )


class EdistribucionRefreshButton(_EdistribucionHubButton):
    """Vuelve a pedir los datos al add-on ya (sin esperar al próximo ciclo de 15 min)."""

    entity_description = ButtonEntityDescription(key="refresh", translation_key="refresh")

    def __init__(self, coordinator: EdistribucionCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_refresh"

    async def async_press(self) -> None:
        await self.coordinator.async_request_refresh()


class EdistribucionReloginButton(_EdistribucionHubButton):
    """Fuerza un login fresco en el add-on (por si la sesión está rara) y luego actualiza."""

    entity_description = ButtonEntityDescription(key="relogin", translation_key="relogin")

    def __init__(self, coordinator: EdistribucionCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_relogin"

    async def async_press(self) -> None:
        await self.coordinator.client.async_relogin()
        await self.coordinator.async_request_refresh()
