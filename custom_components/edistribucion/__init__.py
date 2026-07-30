"""Integración e-distribución: sensores de consumo/potencia hablando con el add-on local."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EdistribucionApiClient, EdistribucionApiError
from .const import DOMAIN
from .coordinator import EdistribucionCoordinator
from .statistics import async_backfill_energy_statistics

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON]

SERVICE_CONSULTAR_CONSUMO = "consultar_consumo"
SERVICE_CONSULTAR_CONSUMO_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): str,
        vol.Optional("range", default="3"): vol.In(["1", "2", "3"]),
        vol.Optional("fecha"): str,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    client = EdistribucionApiClient(session, entry.data[CONF_HOST], entry.data[CONF_PORT])
    coordinator = EdistribucionCoordinator(hass, client, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    for bundle in coordinator.data.values():
        sp = bundle.get("supply_point") or {}
        await async_backfill_energy_statistics(hass, sp.get("cups", ""), bundle.get("month"))

    if not hass.services.has_service(DOMAIN, SERVICE_CONSULTAR_CONSUMO):

        async def _async_service_consultar_consumo(call: ServiceCall) -> dict:
            """Consulta bajo demanda un rango/fecha concretos (no solo lo que ya cachea el coordinator)."""
            device_id = call.data["device_id"]

            device = dr.async_get(hass).async_get(device_id)
            if device is None:
                raise ServiceValidationError("Dispositivo no encontrado")

            cont_id = next((identifier[1] for identifier in device.identifiers if identifier[0] == DOMAIN), None)
            if cont_id is None:
                raise ServiceValidationError("Ese dispositivo no corresponde a un suministro de e-distribución")

            entry_id = next(iter(device.config_entries), None)
            target_coordinator: EdistribucionCoordinator | None = hass.data.get(DOMAIN, {}).get(entry_id)
            if target_coordinator is None:
                raise ServiceValidationError("No se encontró la integración de e-distribución para ese dispositivo")

            try:
                return await target_coordinator.client.async_get_consumption(
                    cont_id, call.data.get("range", "3"), call.data.get("fecha")
                )
            except EdistribucionApiError as err:
                raise ServiceValidationError(str(err)) from err

        hass.services.async_register(
            DOMAIN,
            SERVICE_CONSULTAR_CONSUMO,
            _async_service_consultar_consumo,
            schema=SERVICE_CONSULTAR_CONSUMO_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Cuando cambian las opciones (intervalo, suministros seguidos, alias), recarga la integración."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded
