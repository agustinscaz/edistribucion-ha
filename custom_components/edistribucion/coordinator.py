"""DataUpdateCoordinator: pide datos al add-on cada X minutos, uno por suministro."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import EdistribucionApiClient, EdistribucionApiError, InvalidCredentialsError, PasswordChangeRequiredError
from .const import (
    CONF_CONTRACTED_POWER_PUNTA,
    CONF_CONTRACTED_POWER_VALLE,
    CONF_SUPPLY_POINTS,
    CONSECUTIVE_FAILURES_FOR_REPAIR,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
)
from .costs import LLANO, PUNTA, VALLE, cost_breakdown, surplus_compensation_value
from .esios import DEFAULT_PVPC_ZONE, EsiosError, async_get_pvpc_prices_for_day
from .statistics import _parse_day, async_backfill_cost_statistics, async_backfill_derived_daily_statistics, async_backfill_energy_statistics

_LOGGER = logging.getLogger(__name__)

RANGE_MONTH = "3"
RANGE_WEEK = "2"

ISSUE_CONNECTION = "addon_connection_failed"
ISSUE_INVALID_CREDENTIALS = "invalid_credentials"
ISSUE_PASSWORD_CHANGE_REQUIRED = "password_change_required"

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
        self._last_derived_backfill_day: str | None = None
        # Última vez que se vio cambiar importado/exportado "de hoy" de cada CUPS — para el
        # atributo de "frescura del dato" (ver sensor.py): la curva horaria de e-distribución se
        # publica con retraso, así que el valor puede quedarse igual varias horas sin que eso
        # signifique que la integración esté fallando.
        self._last_value_change: dict[str, dict[str, datetime]] = {}
        self._previous_values: dict[str, dict[str, float]] = {}
        # Acumulado de los meses YA COMPLETADOS de este año, recalculado una vez al día a partir de
        # las estadísticas del recorder (issue #27 — ver _async_update_year_to_date_if_needed), no
        # de volver a pedirle el histórico a e-distribución. `_year_to_date_completed` es la suma
        # (por CUPS); `_year_to_date_details` es el desglose mes a mes que usa diagnostics.py/el
        # atributo `meses_completados_detalle` del sensor de coste acumulado (issues #25/#29). El
        # mes en curso se suma en vivo aparte, con lo que ya se tiene en `bundle["month"]`.
        self._year_to_date_completed: dict[str, dict[str, float]] = {}
        self._year_to_date_details: dict[str, dict[int, dict[str, float]]] = {}
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

                bundle: dict = {
                    "supply_point": sp,
                    "consumption": None,
                    "week": None,
                    "month": None,
                    "month_last_year": None,
                    "contract": None,
                }

                # Las 5 llamadas de este CUPS van en PARALELO (issue #17), no una detrás de otra:
                # cada una pasa por su propio ciclo de 3 reintentos con backoff en
                # `EdistribucionApiClient._request` (hasta ~90s en el peor caso) — en secuencial,
                # un add-on completamente caído hacía fallar las 5 EN CADENA (~5x ese tiempo) antes
                # de darse por vencido con este CUPS, para descubrir al final exactamente el mismo
                # fallo de conexión que ya se sabía desde la primera llamada. `return_exceptions`
                # preserva la independencia de cada una (un fallo aquí no debe tirar las demás).
                a_year_ago = (dt_util.now() - timedelta(days=365)).strftime("%Y-%m-%d")
                contract, consumption, week, month, month_last_year = await asyncio.gather(
                    self.client.async_get_contracted_power(cont_id),
                    self.client.async_get_consumption(cont_id),
                    self.client.async_get_consumption(cont_id, RANGE_WEEK),
                    self.client.async_get_consumption(cont_id, RANGE_MONTH),
                    self.client.async_get_consumption(cont_id, RANGE_MONTH, a_year_ago),
                    return_exceptions=True,
                )

                if isinstance(contract, EdistribucionApiError):
                    _LOGGER.warning("No se pudo leer la potencia contratada real de %s: %s", sp.get("cups"), contract)
                elif isinstance(contract, BaseException):
                    raise contract
                else:
                    bundle["contract"] = contract
                    sp[CONF_CONTRACTED_POWER_PUNTA] = contract.get("contractedPowerPuntaKw") or 0
                    sp[CONF_CONTRACTED_POWER_VALLE] = contract.get("contractedPowerValleKw") or 0

                if isinstance(consumption, EdistribucionApiError):
                    _LOGGER.warning("No se pudo leer consumo de hoy de %s: %s", sp.get("cups"), consumption)
                elif isinstance(consumption, BaseException):
                    raise consumption
                else:
                    bundle["consumption"] = consumption

                if isinstance(week, EdistribucionApiError):
                    _LOGGER.warning("No se pudo leer consumo semanal de %s: %s", sp.get("cups"), week)
                elif isinstance(week, BaseException):
                    raise week
                else:
                    bundle["week"] = week

                if isinstance(month, EdistribucionApiError):
                    _LOGGER.warning("No se pudo leer consumo mensual de %s: %s", sp.get("cups"), month)
                elif isinstance(month, BaseException):
                    raise month
                else:
                    bundle["month"] = month

                if isinstance(month_last_year, EdistribucionApiError):
                    # Normal si el contrato es más nuevo que un año — no hay nada que comparar todavía.
                    _LOGGER.debug("Sin histórico de hace un año para %s: %s", sp.get("cups"), month_last_year)
                elif isinstance(month_last_year, BaseException):
                    raise month_last_year
                else:
                    bundle["month_last_year"] = month_last_year

                self._track_value_freshness(cont_id, bundle)
                data[cont_id] = bundle

            self.last_success_time = dt_util.utcnow()
            self._consecutive_failures = 0
            ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_CONNECTION}_{self.entry_id}")
            ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_INVALID_CREDENTIALS}_{self.entry_id}")
            ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_PASSWORD_CHANGE_REQUIRED}_{self.entry_id}")
            await self._async_backfill_statistics_if_needed(data)
            await self._async_backfill_derived_statistics_if_needed(data)
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
        except PasswordChangeRequiredError as err:
            # Distinto de credenciales incorrectas: la contraseña configurada sigue siendo la
            # correcta, pero e-distribución exige cambiarla (política de caducidad periódica,
            # confirmado en vivo el 05-sep-2026) antes de dejar continuar — hace falta acción manual
            # del usuario en la Zona Privada, no tiene sentido reintentar solo.
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"{ISSUE_PASSWORD_CHANGE_REQUIRED}_{self.entry_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="password_change_required",
            )
            raise UpdateFailed(f"e-distribución exige cambiar la contraseña de la cuenta: {err}") from err
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
        menudo no haría daño, pero tampoco aportaría nada.

        También mantiene las estadísticas EXTERNAS de coste (issue #27: `energy_cost`/`power_cost`/
        `surplus_compensation`) que alimentan el acumulado del año — ver
        `_async_update_year_to_date_if_needed`."""
        today_key = dt_util.now().strftime("%Y-%m-%d")
        if self._last_backfill_day == today_key:
            return
        for bundle in data.values():
            sp = bundle.get("supply_point") or {}
            await async_backfill_energy_statistics(self.hass, sp.get("cups", ""), bundle.get("month"))
            await async_backfill_cost_statistics(self.hass, sp.get("cups", ""), sp, bundle.get("month"), self.pvpc_prices)
        self._last_backfill_day = today_key

    async def _async_backfill_derived_statistics_if_needed(self, data: dict) -> None:
        """Como `_async_backfill_statistics_if_needed` (Panel de Energía), pero para los sensores
        derivados "_hoy" propios de la integración (issue #20): kWh/coste importado y exportado por
        tramo, energía importada/exportada de hoy, y compensación de excedentes de hoy. Un corte de
        sesión de varios días deja estos sensores SIN estadísticas para esos días — el dato en sí
        sigue disponible al recuperarse (`bundle["month"]` cubre ~30 días hacia atrás, ver
        RANGE_MONTH), solo faltaba reconstruir el histórico de Home Assistant.

        Solo rellena, por sensor, los DÍAS QUE NO TENGAN NINGUNA ESTADÍSTICA (ver
        `statistics.async_backfill_derived_daily_statistics`) — un día con polling normal no se
        toca, para no sustituir su histórico horario real por un único punto diario más basto."""
        today_key = dt_util.now().strftime("%Y-%m-%d")
        if self._last_derived_backfill_day == today_key:
            return
        registry = er.async_get(self.hass)
        today_str = dt_util.now().strftime("%d/%m/%Y")
        for cont_id, bundle in data.items():
            sp = bundle.get("supply_point") or {}
            hourly = ((bundle.get("month") or {}).get("hourlyByDate")) or {}
            past_dates = sorted((d for d in hourly if d != today_str), key=lambda d: datetime.strptime(d, "%d/%m/%Y"))
            if not past_dates:
                continue
            await self._async_backfill_derived_for_cups(registry, cont_id, sp, hourly, past_dates)
        self._last_derived_backfill_day = today_key

    async def _async_backfill_derived_for_cups(
        self, registry: er.EntityRegistry, cont_id: str, sp: dict, hourly: dict, past_dates: list[str]
    ) -> None:
        """Recalcula, para CADA día pasado de `past_dates`, el mismo desglose que ya usa
        `native_value` de cada sensor "_hoy" (ver sensor.py: `_EdistribucionTramoSensor`/
        `_EdistribucionExportTramoSensor`/`EdistribucionImportedEnergySensor`/etc.), a partir del
        `hourlyByDate` de ESE día en vez de "el día más reciente" — así se reutiliza exactamente la
        misma fórmula que ya está verificada en vivo, sin duplicar reglas de impuestos/tramos aparte.
        Un sensor que no existe (tarifa/opciones no lo crean, ver `async_setup_entry`) se salta solo
        (la búsqueda en el registro de entidades devuelve None)."""
        import_prices = {PUNTA: sp.get("price_punta") or 0, LLANO: sp.get("price_llano") or 0, VALLE: sp.get("price_valle") or 0}
        surplus_price = sp.get("surplus_price") or 0
        export_prices = {PUNTA: surplus_price, LLANO: surplus_price, VALLE: surplus_price}
        holiday_region = sp.get("holiday_region")
        zone = sp.get("pvpc_zone") or DEFAULT_PVPC_ZONE
        iee_percent = sp.get("iee_percent") or 0
        iva_percent = sp.get("iva_percent") or 0

        imported_series: list[tuple[datetime, float]] = []
        exported_series: list[tuple[datetime, float]] = []
        import_breakdown_series: dict[str, list[tuple[datetime, float]]] = {
            "kwh_punta": [], "kwh_llano": [], "kwh_valle": [], "coste_punta": [], "coste_llano": [], "coste_valle": [],
        }
        export_breakdown_series: dict[str, list[tuple[datetime, float]]] = {
            "kwh_punta": [], "kwh_llano": [], "kwh_valle": [], "coste_punta": [], "coste_llano": [], "coste_valle": [],
        }
        surplus_series: list[tuple[datetime, float]] = []

        for date_str in past_dates:
            hours = hourly[date_str]
            day_start = _parse_day(date_str)
            day_source = {"hourlyByDate": {date_str: hours}}

            imported_kwh = sum(h.get("importedKwh") or 0 for h in hours)
            exported_kwh = sum(h.get("exportedKwh") or 0 for h in hours)
            imported_series.append((day_start, imported_kwh))
            exported_series.append((day_start, exported_kwh))

            import_breakdown = cost_breakdown(
                day_source, import_prices, holiday_region, iee_percent=iee_percent, iva_percent=iva_percent, zone=zone
            )
            if import_breakdown:
                for key, series in import_breakdown_series.items():
                    series.append((day_start, import_breakdown[key]))

            export_breakdown = cost_breakdown(day_source, export_prices, holiday_region, field="exportedKwh", zone=zone)
            if export_breakdown:
                for key, series in export_breakdown_series.items():
                    series.append((day_start, export_breakdown[key]))

            surplus_series.append((day_start, surplus_compensation_value(sp, exported_kwh) or 0.0))

        kwh_unit, kwh_class = UnitOfEnergy.KILO_WATT_HOUR, "energy"
        eur_unit, eur_class = "EUR", None
        targets: list[tuple[str, str | None, str | None, list[tuple[datetime, float]]]] = [
            (f"{cont_id}_imported_energy_today", kwh_unit, kwh_class, imported_series),
            (f"{cont_id}_exported_energy_today", kwh_unit, kwh_class, exported_series),
        ]
        for tramo in (PUNTA, LLANO, VALLE):
            targets.append((f"{cont_id}_{tramo}_kwh_today", kwh_unit, kwh_class, import_breakdown_series[f"kwh_{tramo}"]))
            targets.append((f"{cont_id}_{tramo}_cost_today", eur_unit, eur_class, import_breakdown_series[f"coste_{tramo}"]))
            targets.append((f"{cont_id}_{tramo}_exported_kwh_today", kwh_unit, kwh_class, export_breakdown_series[f"kwh_{tramo}"]))
            targets.append(
                (f"{cont_id}_{tramo}_exported_compensation_today", eur_unit, eur_class, export_breakdown_series[f"coste_{tramo}"])
            )
        targets.append((f"{cont_id}_surplus_compensation_today", eur_unit, eur_class, surplus_series))

        for unique_id, unit, unit_class, day_values in targets:
            if not day_values:
                continue
            entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
            if entity_id is None:
                continue  # sensor no creado (tarifa/opciones no lo requieren) — nada que rellenar
            await async_backfill_derived_daily_statistics(self.hass, entity_id, unit, unit_class, day_values)

    _YEAR_TO_DATE_METRICS = (
        ("imported_kwh", "imported_energy"),
        ("exported_kwh", "exported_energy"),
        ("cost", "energy_cost"),
        ("power_cost", "power_cost"),
        ("surplus_compensation", "surplus_compensation"),
    )

    async def _async_monthly_statistic_changes(self, statistic_id: str, start: datetime, end: datetime) -> dict[int, float]:
        """{mes (1-12): `change` de ESE mes} para `statistic_id` entre `start` y `end` (exclusivo),
        vía `recorder.statistics_during_period(period="month", types={"change"})` — `change` ya es
        la resta hecha por el propio recorder entre el `sum` al principio y al final de cada mes,
        así que no hace falta guardar ni sumar nada aparte (issue #27). Un mes SIN ninguna fila
        devuelta (`statistic_id` no existía todavía esos días — CUPS recién configurado, o métrica
        nueva en esta versión) queda AUSENTE del dict, no en 0.0 — para diferenciarlo de un mes que
        sí se pudo medir y dio 0 kWh/EUR real."""
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import statistics_during_period

        def _query() -> dict[int, float]:
            result = statistics_during_period(
                self.hass, start_time=start, end_time=end, statistic_ids={statistic_id}, period="month", units=None, types={"change"}
            )
            changes: dict[int, float] = {}
            for row in result.get(statistic_id) or []:
                row_start = row["start"]
                if isinstance(row_start, (int, float)):  # timestamp UNIX crudo según versión de HA
                    row_start = dt_util.utc_from_timestamp(row_start)
                changes[dt_util.as_local(row_start).month] = row.get("change") or 0.0
            return changes

        try:
            return await get_instance(self.hass).async_add_executor_job(_query)
        except Exception as err:  # noqa: BLE001 — un fallo de lectura no debe romper el ciclo
            _LOGGER.warning("No se pudo leer estadísticas de %s para el acumulado del año: %s", statistic_id, err)
            return {}

    async def _async_update_year_to_date_if_needed(self, data: dict) -> None:
        """Una vez al día, recalcula el acumulado de los meses YA COMPLETADOS de este año a partir
        de las estadísticas EXTERNAS que esta misma integración mantiene con arrastre correcto
        entre días/meses (`edistribucion:<cups>_imported_energy`/`_exported_energy` para energía,
        `_energy_cost`/`_power_cost`/`_surplus_compensation` para EUR — ver
        `_async_backfill_statistics_if_needed` y statistics.py).

        Issue #27: ANTES esto le volvía a pedir a e-distribución (`async_get_consumption`) cada mes
        ya cerrado del año, y e-distribución solo retiene granularidad diaria ~1-2 meses hacia
        atrás — pasado ese margen, la API devolvía 0.0 SIN error (issue #25), dejando el acumulado
        del año permanentemente por debajo de lo real. Las estadísticas del recorder, en cambio,
        persisten indefinidamente una vez escritas, así que ya no hace falta volver a pedirle nada
        al add-on para esto: se lee lo que el propio Home Assistant ya tiene guardado.

        LIMITACIÓN (aceptada, ver issue #27): un mes anterior a que la estadística correspondiente
        empezara a escribirse (CUPS instalado a mitad de año, o justo tras esta actualización para
        coste/potencia/compensación, nuevas en esta versión) no tiene ningún punto que sumar y
        cuenta como 0 — de forma HONESTA (sin inventar un cero disfrazado de dato real, a
        diferencia del bug de #25)."""
        today_key = dt_util.now().strftime("%Y-%m-%d")
        if self._year_to_date_fetched_day == today_key:
            return
        if "recorder" not in self.hass.config.components:
            self._year_to_date_fetched_day = today_key
            return

        now = dt_util.now()
        start_of_year = dt_util.as_utc(now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0))
        start_of_this_month = dt_util.as_utc(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))

        for cont_id, bundle in data.items():
            sp = bundle.get("supply_point") or {}
            cups = (sp.get("cups") or "").lower()
            totals = {field: 0.0 for field, _suffix in self._YEAR_TO_DATE_METRICS}
            details: dict[int, dict[str, float]] = {month: dict(totals) for month in range(1, now.month)}

            if cups and start_of_this_month > start_of_year:  # enero: ningún mes cerrado todavía
                for field, suffix in self._YEAR_TO_DATE_METRICS:
                    statistic_id = f"{DOMAIN}:{cups}_{suffix}"
                    changes = await self._async_monthly_statistic_changes(statistic_id, start_of_year, start_of_this_month)
                    for month, change in changes.items():
                        if month not in details:
                            continue  # fuera de rango (no debería pasar, cinturón de seguridad)
                        details[month][field] = round(change, 4)
                        totals[field] += change

            self._year_to_date_completed[cont_id] = {field: round(value, 4) for field, value in totals.items()}
            self._year_to_date_details[cont_id] = details
        self._year_to_date_fetched_day = today_key

    def year_to_date_completed_months(self, cont_id: str) -> dict[str, float]:
        """kWh importado/exportado, coste de energía, término de potencia y compensación de
        excedentes de los meses YA COMPLETADOS de este año para este CUPS (recalculado una vez al
        día a partir de estadísticas del recorder, ver `_async_update_year_to_date_if_needed`) —
        falta sumarle el mes en curso, que cada sensor añade en vivo con lo que ya tiene a mano."""
        return self._year_to_date_completed.get(
            cont_id,
            {"imported_kwh": 0.0, "exported_kwh": 0.0, "cost": 0.0, "power_cost": 0.0, "surplus_compensation": 0.0},
        )

    def year_to_date_month_details(self, cont_id: str) -> dict[int, dict[str, float]]:
        """Detalle MES A MES (1-12, meses ya completados de este año) de lo que hay acumulado en
        las estadísticas del recorder para este CUPS — pensado para diagnosticar sin depender de
        logs en debug (issue #25/#29): cada mes incluye las 5 métricas de
        `year_to_date_completed_months`, en 0.0 si esa estadística todavía no tenía ningún punto
        ese mes (ver la LIMITACIÓN de `_async_update_year_to_date_if_needed`)."""
        return self._year_to_date_details.get(cont_id, {})

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
