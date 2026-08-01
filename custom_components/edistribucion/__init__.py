"""Integración e-distribución: sensores de consumo/potencia hablando con el add-on local."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .api import EdistribucionApiClient, EdistribucionApiError
from .const import DOMAIN
from .coordinator import RANGE_MONTH, EdistribucionCoordinator
from .costs import monthly_summary_csv
from .esios import DEFAULT_PVPC_ZONE, cheapest_window, pvpc_prices_to_csv
from .migration import async_migrate_legacy_options
from .statistics import async_backfill_energy_statistics, months_back

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON, Platform.CALENDAR]

SERVICE_CONSULTAR_CONSUMO = "consultar_consumo"
SERVICE_CONSULTAR_CONSUMO_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): str,
        vol.Optional("range", default="3"): vol.In(["1", "2", "3"]),
        vol.Optional("fecha"): str,
    }
)

SERVICE_EXPORTAR_PRECIOS_PVPC = "exportar_precios_pvpc"
SERVICE_EXPORTAR_PRECIOS_PVPC_SCHEMA = vol.Schema({vol.Optional("zona"): str})

SERVICE_HORAS_MAS_BARATAS_PVPC = "horas_mas_baratas_pvpc"
SERVICE_HORAS_MAS_BARATAS_PVPC_SCHEMA = vol.Schema(
    {
        vol.Required("horas"): vol.All(int, vol.Range(min=1, max=24)),
        vol.Optional("zona", default=DEFAULT_PVPC_ZONE): str,
    }
)

SERVICE_RESUMEN_MENSUAL = "resumen_mensual"
SERVICE_RESUMEN_MENSUAL_SCHEMA = vol.Schema({vol.Required("device_id"): str})

SERVICE_RELLENAR_HISTORICO = "rellenar_historico"
SERVICE_RELLENAR_HISTORICO_SCHEMA = vol.Schema(
    {
        vol.Optional("device_id"): str,
        vol.Optional("meses", default=12): vol.All(int, vol.Range(min=1, max=36)),
    }
)


def _resolve_device(hass: HomeAssistant, device_id: str) -> tuple[EdistribucionCoordinator, str]:
    """(coordinator, cont_id) del dispositivo (CUPS) al que apunta `device_id` — comparte la
    resolución que usan todos los servicios que operan sobre UN suministro concreto."""
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

    return target_coordinator, cont_id


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    async_migrate_legacy_options(hass, entry)
    session = async_get_clientsession(hass)
    client = EdistribucionApiClient(session, entry.data[CONF_HOST], entry.data[CONF_PORT])
    coordinator = EdistribucionCoordinator(hass, client, entry)
    await coordinator.async_load_pvpc_prices_cache()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not hass.services.has_service(DOMAIN, SERVICE_CONSULTAR_CONSUMO):

        async def _async_service_consultar_consumo(call: ServiceCall) -> dict:
            """Consulta bajo demanda un rango/fecha concretos (no solo lo que ya cachea el coordinator)."""
            target_coordinator, cont_id = _resolve_device(hass, call.data["device_id"])
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

    if not hass.services.has_service(DOMAIN, SERVICE_EXPORTAR_PRECIOS_PVPC):

        async def _async_service_exportar_precios_pvpc(call: ServiceCall) -> dict:
            """Vuelca a CSV los precios PVPC ya cacheados (de todas las entradas de configuración,
            no solo una) — para analizarlos fuera de Home Assistant."""
            zone_filter = call.data.get("zona")
            prices_by_zone: dict[str, dict[str, float]] = {}
            for target_coordinator in hass.data.get(DOMAIN, {}).values():
                for zone, prices in target_coordinator.pvpc_prices.items():
                    prices_by_zone.setdefault(zone, {}).update(prices)
            return {"csv": pvpc_prices_to_csv(prices_by_zone, zone_filter)}

        hass.services.async_register(
            DOMAIN,
            SERVICE_EXPORTAR_PRECIOS_PVPC,
            _async_service_exportar_precios_pvpc,
            schema=SERVICE_EXPORTAR_PRECIOS_PVPC_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_HORAS_MAS_BARATAS_PVPC):

        async def _async_service_horas_mas_baratas_pvpc(call: ServiceCall) -> dict:
            """Ventana de N horas CONSECUTIVAS más barata a partir de ahora, con los precios PVPC
            ya cacheados — para automatizar cargas oportunistas (coche eléctrico, batería) sin
            mirar los precios a mano."""
            zone = call.data.get("zona", DEFAULT_PVPC_ZONE)
            prices: dict[str, float] = {}
            for target_coordinator in hass.data.get(DOMAIN, {}).values():
                prices.update(target_coordinator.pvpc_prices.get(zone, {}))
            result = cheapest_window(prices, call.data["horas"], dt_util.now())
            if result is None:
                raise ServiceValidationError(
                    "No hay suficientes horas consecutivas con precio publicado por delante (prueba con menos horas, "
                    "o espera a que se publiquen los precios de mañana)"
                )
            return result

        hass.services.async_register(
            DOMAIN,
            SERVICE_HORAS_MAS_BARATAS_PVPC,
            _async_service_horas_mas_baratas_pvpc,
            schema=SERVICE_HORAS_MAS_BARATAS_PVPC_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_RESUMEN_MENSUAL):

        async def _async_service_resumen_mensual(call: ServiceCall) -> dict:
            """Resumen del mes en curso (coste por periodo, término de potencia, excedentes) en
            texto CSV — una estimación propia, no la factura real (ver limitaciones documentadas en
            costs.py)."""
            target_coordinator, cont_id = _resolve_device(hass, call.data["device_id"])
            bundle = target_coordinator.data.get(cont_id)
            if bundle is None:
                raise ServiceValidationError("Todavía no hay datos para ese suministro")
            sp = bundle.get("supply_point") or {}
            return {"resumen": monthly_summary_csv(sp, bundle.get("month"), target_coordinator.pvpc_prices)}

        hass.services.async_register(
            DOMAIN,
            SERVICE_RESUMEN_MENSUAL,
            _async_service_resumen_mensual,
            schema=SERVICE_RESUMEN_MENSUAL_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_RELLENAR_HISTORICO):

        async def _async_service_rellenar_historico(call: ServiceCall) -> dict:
            """Rellena el histórico de estadísticas del Dashboard de Energía para los últimos
            `meses` (incluido el mes en curso, ver `months_back`) — el backfill automático del
            coordinator solo se repite día a día para el mes en curso, así que esto sirve para
            recuperar de golpe meses anteriores (recién instalada la integración, o si vienes de
            otra solución). Sin `device_id`, rellena todos los suministros seguidos."""
            meses = call.data["meses"]
            device_id = call.data.get("device_id")

            targets: list[tuple[EdistribucionCoordinator, str, str]] = []
            if device_id:
                target_coordinator, cont_id = _resolve_device(hass, device_id)
                bundle = target_coordinator.data.get(cont_id) or {}
                cups = (bundle.get("supply_point") or {}).get("cups", "")
                targets.append((target_coordinator, cont_id, cups))
            else:
                for target_coordinator in hass.data.get(DOMAIN, {}).values():
                    for cont_id, bundle in target_coordinator.data.items():
                        cups = (bundle.get("supply_point") or {}).get("cups", "")
                        targets.append((target_coordinator, cont_id, cups))

            now = dt_util.now()
            months_filled = 0
            for target_coordinator, cont_id, cups in targets:
                for month_start in months_back(now, meses):
                    try:
                        month_data = await target_coordinator.client.async_get_consumption(
                            cont_id, RANGE_MONTH, month_start.strftime("%Y-%m-%d")
                        )
                    except EdistribucionApiError as err:
                        _LOGGER.debug("Sin consumo de %s para %s: %s", cups, month_start.strftime("%Y-%m"), err)
                        continue
                    await async_backfill_energy_statistics(hass, cups, month_data)
                    months_filled += 1

            return {"suministros": len(targets), "meses_rellenados": months_filled}

        hass.services.async_register(
            DOMAIN,
            SERVICE_RELLENAR_HISTORICO,
            _async_service_rellenar_historico,
            schema=SERVICE_RELLENAR_HISTORICO_SCHEMA,
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
