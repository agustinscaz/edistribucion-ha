"""DataUpdateCoordinator: pide datos al add-on cada X minutos, uno por suministro."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import EdistribucionApiClient, EdistribucionApiError, InvalidCredentialsError
from .const import (
    CONF_CONTRACTED_POWER_PUNTA,
    CONF_CONTRACTED_POWER_VALLE,
    CONF_SUPPLY_POINTS,
    CONSECUTIVE_FAILURES_FOR_REPAIR,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
)
from .costs import estimate_energy_cost
from .esios import DEFAULT_PVPC_ZONE, EsiosError, async_get_pvpc_prices_for_day
from .statistics import async_backfill_energy_statistics

_LOGGER = logging.getLogger(__name__)

RANGE_MONTH = "3"
RANGE_WEEK = "2"

ISSUE_CONNECTION = "addon_connection_failed"
ISSUE_INVALID_CREDENTIALS = "invalid_credentials"

_PVPC_STORAGE_VERSION = 1


def _is_current_month_price_key(key: str, now: datetime) -> bool:
    """¿La clave "DD/MM/YYYY H" de un precio PVPC cacheado pertenece al mes/año de `now`?"""
    date_part = key.split(" ", 1)[0]
    return len(date_part) == 10 and date_part[3:10] == now.strftime("%m/%Y")


def _latest_daily_values(consumption: dict | None) -> dict[str, float] | None:
    """{"imported": kwh, "exported": kwh} del día más reciente en dailyTotals, o None sin datos."""
    if not consumption or not consumption.get("dailyTotals"):
        return None
    latest = max(consumption["dailyTotals"], key=lambda d: datetime.strptime(d["date"], "%d/%m/%Y"))
    return {"imported": latest.get("importedKwh") or 0.0, "exported": latest.get("exportedKwh") or 0.0}


class EdistribucionCoordinator(DataUpdateCoordinator):
    """Mantiene: lista de suministros (filtrados/con alias según opciones) + consumo (hoy/semana/mes),
    comparativa con el mismo mes del año anterior, y potencia de cada uno."""

    def __init__(self, hass: HomeAssistant, client: EdistribucionApiClient, entry: ConfigEntry) -> None:
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES)
        super().__init__(hass, _LOGGER, name="edistribucion", update_interval=timedelta(minutes=interval))
        self.client = client
        self.entry_id = entry.entry_id
        self.supply_point_options: dict[str, dict] = entry.options.get(CONF_SUPPLY_POINTS, {})
        # Término de potencia (kW contratados + €/kW/día) y zona PVPC son por CUPS — ver
        # supply_point_options. Los precios PVPC se cachean por zona, porque distintos CUPS podrían
        # usar zonas distintas (poco habitual, pero posible).
        self.pvpc_prices: dict[str, dict[str, float]] = {}
        self._pvpc_fetched_date: str | None = None
        # Persistencia en disco del caché de precios PVPC — sin esto, un reinicio de HA lo perdía
        # por completo y había que volver a pedirle a ESIOS el mes en curso día a día de nuevo (ver
        # async_load_pvpc_prices_cache / _async_save_pvpc_prices_cache).
        self._pvpc_store: Store = Store(hass, _PVPC_STORAGE_VERSION, f"{DOMAIN}_pvpc_prices_{entry.entry_id}")
        self.last_success_time: datetime | None = None
        self._consecutive_failures = 0
        self._last_backfill_day: str | None = None
        # Última vez que se vio cambiar importado/exportado "de hoy" de cada CUPS — para el
        # atributo de "frescura del dato" (ver sensor.py): la curva horaria de e-distribución se
        # publica con retraso, así que el valor puede quedarse igual varias horas sin que eso
        # signifique que la integración esté fallando.
        self._last_value_change: dict[str, dict[str, datetime]] = {}
        self._previous_values: dict[str, dict[str, float]] = {}
        # Total (kWh/coste) de cada mes YA COMPLETADO, cacheado por (cont_id, año, mes) — un mes
        # cerrado no vuelve a cambiar nunca, así que una vez pedido no hace falta volver a pedirlo
        # (ver _async_update_year_to_date_if_needed). `_year_to_date_completed` es la SUMA ya
        # hecha de esta caché para el año en curso, recalculada a partir de ella cada día (barato,
        # sin llamadas al add-on) — el mes en curso se suma en vivo aparte, con lo que ya se tiene
        # en `bundle["month"]`, no hace falta guardarlo en ninguna de las dos cachés.
        self._year_to_date_month_cache: dict[tuple[str, int, int], dict[str, float]] = {}
        self._year_to_date_completed: dict[str, dict[str, float]] = {}
        self._year_to_date_fetched_day: str | None = None

    def _pvpc_zones_needed(self) -> set[str]:
        """Zonas de las que hace falta precio PVPC — no solo si la tarifa activa es "pvpc": el
        simulador de tarifas (ver costs.estimate_cost_as_tariff) también necesita el precio real
        aunque el CUPS esté en fija/tramos, para poder comparar "qué habría costado con pvpc"."""
        return {opts.get("pvpc_zone") or DEFAULT_PVPC_ZONE for opts in self.supply_point_options.values() if opts.get("track", True) is not False}

    async def _async_update_pvpc_prices(self) -> None:
        """Los precios PVPC solo cambian una vez al día (se publican ~20:15 para el día
        siguiente) — se pide como mucho una vez por día, no en cada ciclo de actualización, para no
        pedir de más a la API pública de ESIOS sin necesidad. El archivo público solo da UN día por
        petición, así que se piden uno a uno los días del mes que aún no tengamos en caché, para
        cada zona que use algún CUPS con tarifa pvpc."""
        zones = self._pvpc_zones_needed()
        if not zones:
            return
        today = dt_util.now().date()
        today_key = today.strftime("%Y-%m-%d")
        if self._pvpc_fetched_date == today_key:
            return

        session = async_get_clientsession(self.hass)
        tomorrow = today + timedelta(days=1)
        for zone in zones:
            zone_prices = self.pvpc_prices.setdefault(zone, {})
            day = today.replace(day=1)
            while day <= tomorrow:
                day_key_prefix = f"{day.strftime('%d/%m/%Y')} "
                if not any(k.startswith(day_key_prefix) for k in zone_prices):
                    try:
                        zone_prices.update(await async_get_pvpc_prices_for_day(session, zone, day))
                    except EsiosError as err:
                        _LOGGER.warning("No se pudieron obtener precios PVPC de ESIOS (zona %s) para %s: %s", zone, day, err)
                        break  # si ESIOS falla (red, baneo...), se reintenta en el próximo ciclo
                day += timedelta(days=1)
        self._pvpc_fetched_date = today_key
        await self._async_save_pvpc_prices_cache()

    async def async_load_pvpc_prices_cache(self) -> None:
        """Restaura, si lo hay, el caché de precios PVPC guardado en disco antes del último
        reinicio de HA — para no depender de volver a pedirle a ESIOS desde cero todo el mes en
        curso. Se llama una vez, antes del primer refresh (ver __init__.py). Solo se conservan las
        horas del MES ACTUAL: los precios de un mes ya pasado no sirven para nada (nadie los pide)
        y así el archivo en disco no crece sin límite ciclo a ciclo."""
        stored = await self._pvpc_store.async_load()
        if not stored:
            return
        now = dt_util.now()
        for zone, prices in stored.items():
            if not isinstance(prices, dict):
                continue
            zone_prices = self.pvpc_prices.setdefault(zone, {})
            zone_prices.update({k: v for k, v in prices.items() if _is_current_month_price_key(k, now)})

    async def _async_save_pvpc_prices_cache(self) -> None:
        """Vuelca a disco el caché de precios PVPC en memoria — solo las horas del mes en curso
        (ver `async_load_pvpc_prices_cache`, mismo motivo: no acumular meses pasados sin límite)."""
        now = dt_util.now()
        to_store = {
            zone: {k: v for k, v in prices.items() if _is_current_month_price_key(k, now)} for zone, prices in self.pvpc_prices.items()
        }
        await self._pvpc_store.async_save(to_store)

    async def async_force_refresh_pvpc_prices(self) -> None:
        """Fuerza un refresco de precios PVPC ya (botón de la integración), sin esperar al ciclo
        diario — útil si ESIOS falló antes, o si acaban de publicar los precios de mañana."""
        self._pvpc_fetched_date = None
        await self._async_update_pvpc_prices()
        await self.async_request_refresh()

    async def _async_update_data(self) -> dict:
        try:
            await self._async_update_pvpc_prices()
            supply_points = await self.client.async_get_supply_points()
            data: dict[str, dict] = {}
            for sp in supply_points:
                cont_id = sp["contId"]
                opts = self.supply_point_options.get(cont_id, {})
                if opts.get("track", True) is False:
                    continue  # el usuario decidió no seguir este suministro (opciones de la integración)
                # Se mezclan alias + tarifa/precios/excedentes configurados para ESTE CUPS en el
                # propio dict del suministro, para que sensor.py los tenga a mano sin plumbing extra.
                sp = {**sp, **{k: v for k, v in opts.items() if k != "track"}}

                cups_id = sp["cupsId"]
                bundle: dict = {
                    "supply_point": sp,
                    "consumption": None,
                    "week": None,
                    "month": None,
                    "month_last_year": None,
                    "max_power_demand": None,
                    "contract": None,
                }

                try:
                    # Potencia contratada real (punta/valle) + metadatos del contrato — sacada de la
                    # propia distribuidora, no de un valor que teclee el usuario (ver v1.11.0).
                    bundle["contract"] = await self.client.async_get_contracted_power(cont_id)
                    sp[CONF_CONTRACTED_POWER_PUNTA] = bundle["contract"].get("contractedPowerPuntaKw") or 0
                    sp[CONF_CONTRACTED_POWER_VALLE] = bundle["contract"].get("contractedPowerValleKw") or 0
                except EdistribucionApiError as err:
                    _LOGGER.warning("No se pudo leer la potencia contratada real de %s: %s", sp.get("cups"), err)

                try:
                    bundle["consumption"] = await self.client.async_get_consumption(cont_id)
                except EdistribucionApiError as err:
                    _LOGGER.warning("No se pudo leer consumo de hoy de %s: %s", sp.get("cups"), err)
                try:
                    bundle["week"] = await self.client.async_get_consumption(cont_id, RANGE_WEEK)
                except EdistribucionApiError as err:
                    _LOGGER.warning("No se pudo leer consumo semanal de %s: %s", sp.get("cups"), err)
                try:
                    bundle["month"] = await self.client.async_get_consumption(cont_id, RANGE_MONTH)
                except EdistribucionApiError as err:
                    _LOGGER.warning("No se pudo leer consumo mensual de %s: %s", sp.get("cups"), err)
                try:
                    a_year_ago = (dt_util.now() - timedelta(days=365)).strftime("%Y-%m-%d")
                    bundle["month_last_year"] = await self.client.async_get_consumption(cont_id, RANGE_MONTH, a_year_ago)
                except EdistribucionApiError as err:
                    # Normal si el contrato es más nuevo que un año — no hay nada que comparar todavía.
                    _LOGGER.debug("Sin histórico de hace un año para %s: %s", sp.get("cups"), err)
                try:
                    bundle["max_power_demand"] = await self.client.async_get_max_power_demand(cups_id)
                except EdistribucionApiError as err:
                    _LOGGER.debug("Sin potencia máxima para %s (normal si no tiene telegestión): %s", sp.get("cups"), err)

                self._track_value_freshness(cont_id, bundle)
                data[cont_id] = bundle

            self.last_success_time = dt_util.utcnow()
            self._consecutive_failures = 0
            ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_CONNECTION}_{self.entry_id}")
            ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_INVALID_CREDENTIALS}_{self.entry_id}")
            await self._async_backfill_statistics_if_needed(data)
            await self._async_update_year_to_date_if_needed(data)
            return data
        except InvalidCredentialsError as err:
            # Caso inequívoco: no tiene sentido esperar a varios fallos seguidos como con un fallo de
            # red genérico — se avisa ya de que hace falta corregir dni/password en el add-on.
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"{ISSUE_INVALID_CREDENTIALS}_{self.entry_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="invalid_credentials",
            )
            raise UpdateFailed(f"Credenciales incorrectas en el add-on: {err}") from err
        except EdistribucionApiError as err:
            self._consecutive_failures += 1
            if self._consecutive_failures == CONSECUTIVE_FAILURES_FOR_REPAIR:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    f"{ISSUE_CONNECTION}_{self.entry_id}",
                    is_fixable=False,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key="addon_connection_failed",
                )
            raise UpdateFailed(f"Error hablando con el add-on de e-distribución: {err}") from err

    async def _async_backfill_statistics_if_needed(self, data: dict) -> None:
        """Repite el relleno de estadísticas del Dashboard de Energía una vez al día (no solo al
        configurar la integración) — así los meses nuevos se rellenan solos aunque Home Assistant
        lleve semanas sin reiniciarse. Es idempotente (ver statistics.py), así que repetirlo más a
        menudo no haría daño, pero tampoco aportaría nada."""
        today_key = dt_util.now().strftime("%Y-%m-%d")
        if self._last_backfill_day == today_key:
            return
        for bundle in data.values():
            sp = bundle.get("supply_point") or {}
            await async_backfill_energy_statistics(self.hass, sp.get("cups", ""), bundle.get("month"))
        self._last_backfill_day = today_key

    async def _async_update_year_to_date_if_needed(self, data: dict) -> None:
        """Una vez al día, se asegura de tener cacheado el total de CADA mes ya completado de este
        año (el mes en curso se suma en vivo cada ciclo con lo que ya se tiene en
        `bundle["month"]`, no hace falta repetirlo). Un mes cerrado no vuelve a cambiar NUNCA, así
        que solo se le pide al add-on la primera vez que hace falta (ver
        `_year_to_date_month_cache`) — no todos los meses completados en cada ejecución diaria,
        que en diciembre serían 11 llamadas de más por CUPS y por día, para siempre, sin ganar nada
        (el número no cambia hasta que cierra un mes nuevo).

        LIMITACIÓN conocida con tarifa "pvpc": el coste de meses anteriores usa los precios PVPC
        que haya cacheados en `self.pvpc_prices` (solo el mes en curso, ver
        `_async_update_pvpc_prices`) — no se vuelve a pedir el histórico de precios a ESIOS día a
        día para no sobrecargar esa API pública, así que el coste de meses PVPC anteriores al
        actual puede salir incompleto (ver `horas_sin_precio` si se calcula aparte)."""
        today_key = dt_util.now().strftime("%Y-%m-%d")
        if self._year_to_date_fetched_day == today_key:
            return
        now = dt_util.now()
        for cont_id, bundle in data.items():
            sp = bundle.get("supply_point") or {}
            for month in range(1, now.month):  # meses ya completados de este año (1..mes_actual-1)
                cache_key = (cont_id, now.year, month)
                if cache_key in self._year_to_date_month_cache:
                    continue  # mes cerrado, ya cacheado — no cambia, no hace falta volver a pedirlo
                month_date = now.replace(month=month, day=1).strftime("%Y-%m-%d")
                try:
                    month_data = await self.client.async_get_consumption(cont_id, RANGE_MONTH, month_date)
                except EdistribucionApiError as err:
                    _LOGGER.debug(
                        "Sin consumo de %s/%s para el acumulado del año de %s: %s", month, now.year, sp.get("cups"), err
                    )
                    continue
                breakdown = estimate_energy_cost(sp, month_data.get("totalImportedKwh"), month_data, self.pvpc_prices)
                self._year_to_date_month_cache[cache_key] = {
                    "imported_kwh": month_data.get("totalImportedKwh") or 0.0,
                    "exported_kwh": month_data.get("totalExportedKwh") or 0.0,
                    "cost": (breakdown.get("total") or 0.0) if breakdown else 0.0,
                }

            # Suma SOLO los meses de ESTE cont_id y de ESTE año ya cacheados — filtrar por año
            # evita arrastrar totales de años anteriores si Home Assistant lleva corriendo sin
            # reiniciar más de un año (la caché en memoria no se limpia sola al cambiar de año).
            totals = {"imported_kwh": 0.0, "exported_kwh": 0.0, "cost": 0.0}
            for (c_id, year, _month), values in self._year_to_date_month_cache.items():
                if c_id == cont_id and year == now.year:
                    totals["imported_kwh"] += values["imported_kwh"]
                    totals["exported_kwh"] += values["exported_kwh"]
                    totals["cost"] += values["cost"]
            self._year_to_date_completed[cont_id] = totals
        self._year_to_date_fetched_day = today_key

    def year_to_date_completed_months(self, cont_id: str) -> dict[str, float]:
        """kWh importado/exportado y coste estimado de los meses YA COMPLETADOS de este año para
        este CUPS (cacheado una vez al día, ver `_async_update_year_to_date_if_needed`) — falta
        sumarle el mes en curso, que cada sensor añade en vivo con lo que ya tiene a mano."""
        return self._year_to_date_completed.get(cont_id, {"imported_kwh": 0.0, "exported_kwh": 0.0, "cost": 0.0})

    def _track_value_freshness(self, cont_id: str, bundle: dict) -> None:
        """Registra cuándo cambió por última vez el importado/exportado "de hoy" de este CUPS —
        para poder distinguir "sin consumo" de "dato atascado esperando sync del distribuidor" (ver
        sensor.py). La curva horaria de e-distribución se publica con retraso: que este ciclo del
        coordinator haya ido bien no significa que el DATO en sí sea reciente."""
        values = _latest_daily_values(bundle.get("consumption"))
        if values is None:
            return
        now = dt_util.utcnow()
        previous = self._previous_values.get(cont_id)
        tracked = self._last_value_change.setdefault(cont_id, {})
        for flow, value in values.items():
            if flow not in tracked or previous is None or value != previous.get(flow):
                tracked[flow] = now
        self._previous_values[cont_id] = values

    def last_value_change(self, cont_id: str, flow: str) -> datetime | None:
        """Última vez que cambió el importado/exportado "de hoy" de este CUPS, o None si aún no
        hay datos suficientes para saberlo."""
        return self._last_value_change.get(cont_id, {}).get(flow)
