"""Migra ajustes guardados con versiones anteriores de la integración.

Durante el desarrollo, varias claves de opciones se han renombrado y/o movido de "global" (a nivel
de toda la integración) a "por CUPS" — término de potencia (`price_power_p1/p2` -> `price_power_
punta/valle`), zona PVPC (`pvpc_zone` pasó de global a por CUPS), y la vieja clave/región de ESIOS
(`esios_api_key`/`esios_geo_id`, sustituidas por la zona PVPC pública). Sin esta migración, cada uno
de esos cambios de esquema deja huérfano lo que el usuario ya había configurado (vuelve a 0/valor
por defecto) y hay que rellenarlo de nuevo a mano.

Se ejecuta una vez en cada arranque (`async_setup_entry`) — si no hay ninguna clave legada, no hace
nada (comprobación barata, no afecta a instalaciones ya migradas).
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_PRICE_POWER_PUNTA, CONF_PRICE_POWER_VALLE, CONF_SUPPLY_POINTS
from .esios import PVPC_ZONES, ZONE_CEUTA_MELILLA, ZONE_PENINSULA_BALEARES_CANARIAS

# Ceuta/Melilla en la vieja codificación de geo_id de ESIOS (indicadores 1001) — el resto de
# geo_ids (Península/Canarias/Baleares) se corresponden con la zona pública PCB.
_LEGACY_CYM_GEO_IDS = {"8744", "8745"}

# Claves que llegaron a existir a nivel GLOBAL (entry.options) en versiones anteriores y ya no se
# leen ahí — o se migran por-CUPS, o simplemente se descartan (potencia contratada: ya no es una
# opción, se lee en vivo de e-distribución).
_LEGACY_GLOBAL_KEYS_TO_DROP = (
    "contracted_power_p1_kw",
    "contracted_power_p2_kw",
    "price_power_p1",
    "price_power_p2",
    "price_power_punta",
    "price_power_valle",
    "pvpc_zone",
    "esios_api_key",
    "esios_geo_id",
)


def async_migrate_legacy_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    options = dict(entry.options)
    if not any(key in options for key in _LEGACY_GLOBAL_KEYS_TO_DROP):
        return  # ya migrado (o instalación nueva) — nada que hacer

    legacy_price_punta = options.get("price_power_p1", options.get("price_power_punta"))
    legacy_price_valle = options.get("price_power_p2", options.get("price_power_valle"))
    legacy_pvpc_zone = options.get("pvpc_zone")
    legacy_geo_id = options.get("esios_geo_id")
    if not legacy_pvpc_zone and legacy_geo_id:
        legacy_pvpc_zone = ZONE_CEUTA_MELILLA if legacy_geo_id in _LEGACY_CYM_GEO_IDS else ZONE_PENINSULA_BALEARES_CANARIAS
    if legacy_pvpc_zone not in PVPC_ZONES:
        legacy_pvpc_zone = None

    supply_points = {cont_id: dict(sp_opts) for cont_id, sp_opts in options.get(CONF_SUPPLY_POINTS, {}).items()}
    for cont_id, sp_opts in supply_points.items():
        if legacy_price_punta is not None:
            sp_opts.setdefault(CONF_PRICE_POWER_PUNTA, legacy_price_punta)
        if legacy_price_valle is not None:
            sp_opts.setdefault(CONF_PRICE_POWER_VALLE, legacy_price_valle)
        if legacy_pvpc_zone is not None:
            sp_opts.setdefault("pvpc_zone", legacy_pvpc_zone)
    if supply_points:
        options[CONF_SUPPLY_POINTS] = supply_points

    for key in _LEGACY_GLOBAL_KEYS_TO_DROP:
        options.pop(key, None)

    hass.config_entries.async_update_entry(entry, options=options)
