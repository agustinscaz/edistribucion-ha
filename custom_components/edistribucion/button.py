"""Botones: actualizar datos ahora, y forzar una reconexión (login fresco) en el add-on."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, TARIFF_PVPC
from .coordinator import EdistribucionCoordinator
from .device import hub_device_info


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: EdistribucionCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[ButtonEntity] = [
        EdistribucionRefreshButton(coordinator, entry),
        EdistribucionReloginButton(coordinator, entry),
    ]
    if any(opts.get("tariff_type") == TARIFF_PVPC for opts in coordinator.supply_point_options.values()):
        entities.append(EdistribucionRefreshPvpcButton(coordinator, entry))
    async_add_entities(entities)


class _EdistribucionHubButton(CoordinatorEntity[EdistribucionCoordinator], ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: EdistribucionCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_device_info = hub_device_info(entry.entry_id)


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


class EdistribucionRefreshPvpcButton(_EdistribucionHubButton):
    """Fuerza un refresco de precios PVPC ya, sin esperar al ciclo diario (p.ej. si ESIOS falló
    antes, o para comprobar si ya han publicado los precios de mañana)."""

    entity_description = ButtonEntityDescription(key="refresh_pvpc", translation_key="refresh_pvpc")

    def __init__(self, coordinator: EdistribucionCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_refresh_pvpc"

    async def async_press(self) -> None:
        await self.coordinator.async_force_refresh_pvpc_prices()
