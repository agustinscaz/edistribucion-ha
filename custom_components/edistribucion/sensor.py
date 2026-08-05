"""Sensores de e-distribución: importado/exportado (hoy/semana/mes), potencia contratada y coste
estimado (con desglose por tramo, importado y exportado)."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, TARIFF_FIJA, TARIFF_PVPC, TARIFF_TRAMOS, TARIFF_TYPES
from .coordinator import EdistribucionCoordinator
from .costs import (
    LLANO,
    PUNTA,
    VALLE,
    apply_iee,
    apply_iva,
    average_price_per_kwh,
    cost_breakdown,
    current_period,
    estimate_cost_as_tariff,
    estimate_energy_cost,
    next_period_change,
    power_cost,
    self_consumption_ratio,
    surplus_compensation_value,
    tramo_prices_today,
)
from .device import hub_device_info
from .esios import DEFAULT_PVPC_ZONE

_TARIFF_LABELS = {TARIFF_FIJA: "fija", TARIFF_TRAMOS: "tramos", TARIFF_PVPC: "pvpc"}


def _latest_daily_total(consumption: dict | None) -> dict | None:
    """El add-on devuelve varios días (dailyTotals, DD/MM/YYYY) — nos quedamos con el más reciente."""
    if not consumption or not consumption.get("dailyTotals"):
        return None
    return max(
        consumption["dailyTotals"],
        key=lambda d: datetime.strptime(d["date"], "%d/%m/%Y"),
    )


def _latest_day_hourly(consumption: dict | None) -> dict | None:
    """Como `_latest_daily_total` pero con el `hourlyByDate` recortado solo al día más reciente —
    para que el coste "de hoy" no incluya el día extra que trae de más el consumo por defecto."""
    if not consumption or not consumption.get("hourlyByDate"):
        return None
    latest_date = max(consumption["hourlyByDate"], key=lambda d: datetime.strptime(d, "%d/%m/%Y"))
    return {"hourlyByDate": {latest_date: consumption["hourlyByDate"][latest_date]}}


def _energy_cost_configured(sp: dict) -> bool:
    """¿Hay suficiente configurado en este CUPS como para que el coste de energía pueda dar algo?"""
    tariff_type = sp.get("tariff_type")
    if tariff_type == TARIFF_FIJA:
        return bool(sp.get("fixed_price"))
    if tariff_type == TARIFF_PVPC:
        return True  # no hace falta configurar nada más — el precio PVPC es público, sin clave
    return bool(sp.get("price_punta") or sp.get("price_llano") or sp.get("price_valle"))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: EdistribucionCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = [
        EdistribucionLastUpdateSensor(coordinator, entry.entry_id),
        EdistribucionNextUpdateSensor(coordinator, entry.entry_id),
    ]
    for cont_id, bundle in coordinator.data.items():
        sp = bundle["supply_point"]
        entities.append(EdistribucionImportedEnergySensor(coordinator, cont_id, sp))
        entities.append(EdistribucionExportedEnergySensor(coordinator, cont_id, sp))
        for period_key, period_label in (("week", "week"), ("month", "month")):
            entities.append(_EdistribucionPeriodEnergySensor(coordinator, cont_id, sp, period_key, "imported", f"imported_energy_{period_label}"))
            entities.append(_EdistribucionPeriodEnergySensor(coordinator, cont_id, sp, period_key, "exported", f"exported_energy_{period_label}"))
        entities.append(EdistribucionContractedPowerSensor(coordinator, cont_id, sp))
        entities.append(EdistribucionMonthVsLastYearSensor(coordinator, cont_id, sp))
        if _energy_cost_configured(sp):
            entities.append(EdistribucionEstimatedCostTodaySensor(coordinator, cont_id, sp))
            entities.append(EdistribucionEstimatedCostMonthSensor(coordinator, cont_id, sp))
            entities.append(EdistribucionAveragePriceMonthSensor(coordinator, cont_id, sp))
            entities.append(EdistribucionYearToDateCostSensor(coordinator, cont_id, sp))
        if sp.get("tariff_type") == TARIFF_PVPC:
            entities.append(EdistribucionCurrentPvpcPriceSensor(coordinator, cont_id, sp))
        active_tariff = sp.get("tariff_type") or TARIFF_TRAMOS
        for simulated_tariff in TARIFF_TYPES:
            if simulated_tariff == active_tariff:
                continue  # ya lo cubre el sensor de coste estimado normal, no lo dupliques
            if simulated_tariff == TARIFF_FIJA and not sp.get("fixed_price"):
                continue
            if simulated_tariff == TARIFF_TRAMOS and not (sp.get("price_punta") or sp.get("price_llano") or sp.get("price_valle")):
                continue
            entities.append(EdistribucionSimulatedCostMonthSensor(coordinator, cont_id, sp, simulated_tariff))
        if power_cost(sp) > 0:
            entities.append(EdistribucionPowerCostTodaySensor(coordinator, cont_id, sp))
            entities.append(EdistribucionPowerCostMonthSensor(coordinator, cont_id, sp))
            if _energy_cost_configured(sp):
                entities.append(EdistribucionEstimatedCostTodayWithPowerSensor(coordinator, cont_id, sp))
                entities.append(EdistribucionEstimatedCostMonthWithPowerSensor(coordinator, cont_id, sp))
        if sp.get("surplus_compensation") and sp.get("surplus_price"):
            entities.append(EdistribucionSurplusCompensationTodaySensor(coordinator, cont_id, sp))
            entities.append(EdistribucionSurplusCompensationWeekSensor(coordinator, cont_id, sp))
            entities.append(EdistribucionSurplusCompensationMonthSensor(coordinator, cont_id, sp))
            for period_key in ("today", "month"):
                for tramo in (PUNTA, LLANO, VALLE):
                    entities.append(_EdistribucionExportTramoSensor(coordinator, cont_id, sp, period_key, tramo, "kwh"))
                    entities.append(_EdistribucionExportTramoSensor(coordinator, cont_id, sp, period_key, tramo, "compensation"))
        if active_tariff == TARIFF_TRAMOS and _energy_cost_configured(sp):
            for period_key in ("today", "month"):
                for tramo in (PUNTA, LLANO, VALLE):
                    entities.append(_EdistribucionTramoSensor(coordinator, cont_id, sp, period_key, tramo, "kwh"))
                    entities.append(_EdistribucionTramoSensor(coordinator, cont_id, sp, period_key, tramo, "cost"))
            entities.append(EdistribucionCurrentTramoPriceSensor(coordinator, cont_id, sp))
            entities.append(EdistribucionNextTramoPeriodChangeSensor(coordinator, cont_id, sp))
        if (bundle.get("month") or {}).get("totalExportedKwh"):
            entities.append(EdistribucionSelfConsumptionTodaySensor(coordinator, cont_id, sp))
            entities.append(EdistribucionSelfConsumptionMonthSensor(coordinator, cont_id, sp))

    async_add_entities(entities)


class EdistribucionLastUpdateSensor(CoordinatorEntity[EdistribucionCoordinator], SensorEntity):
    """Marca de tiempo de la última vez que se pudo hablar con el add-on sin error."""

    _attr_has_entity_name = True
    entity_description = SensorEntityDescription(
        key="last_update",
        translation_key="last_update",
        device_class=SensorDeviceClass.TIMESTAMP,
    )

    def __init__(self, coordinator: EdistribucionCoordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_last_update"
        self._attr_device_info = hub_device_info(entry_id)

    @property
    def native_value(self):
        return self.coordinator.last_success_time

    @property
    def available(self) -> bool:
        return True


class EdistribucionNextUpdateSensor(CoordinatorEntity[EdistribucionCoordinator], SensorEntity):
    """Estimación de cuándo tocará la siguiente actualización (última correcta + intervalo)."""

    _attr_has_entity_name = True
    entity_description = SensorEntityDescription(
        key="next_update",
        translation_key="next_update",
        device_class=SensorDeviceClass.TIMESTAMP,
    )

    def __init__(self, coordinator: EdistribucionCoordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_next_update"
        self._attr_device_info = hub_device_info(entry_id)

    @property
    def native_value(self):
        if not self.coordinator.last_success_time:
            return None
        return self.coordinator.last_success_time + self.coordinator.update_interval

    @property
    def available(self) -> bool:
        return True


class _EdistribucionBaseSensor(CoordinatorEntity[EdistribucionCoordinator], SensorEntity):
    """Sensor de un punto de suministro concreto (identificado por contId)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EdistribucionCoordinator, cont_id: str, supply_point: dict) -> None:
        super().__init__(coordinator)
        self._cont_id = cont_id
        cups = supply_point.get("cups", cont_id)
        name = supply_point.get("alias") or f"e-distribución {cups}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, cont_id)},
            name=name,
            manufacturer="e-distribución",
            model=supply_point.get("tariff"),
            via_device=(DOMAIN, coordinator.entry_id),
        )

    @property
    def _bundle(self) -> dict:
        return self.coordinator.data.get(self._cont_id, {})


def _freshness_attributes(coordinator: EdistribucionCoordinator, cont_id: str, flow: str) -> dict:
    """Cuándo cambió por última vez este valor — la curva horaria de e-distribución se publica con
    retraso, así que "hoy" puede quedarse igual varias horas sin que sea un fallo de la integración
    (ver coordinator._track_value_freshness). Permite distinguir eso de un dato realmente atascado
    sin tener que mirar el histórico a mano.

    OJO: `ultimo_cambio` es "cuándo lo detectó por primera vez ESTE coordinator", no "cuándo cambió
    de verdad del lado de e-distribución" — solo hay polling (cada `scan_interval`, minutos), no
    push, así que tiene ese margen de error. Es una COTA INFERIOR de antigüedad ("como mínimo así
    de viejo"), no una medición exacta al segundo."""
    last_change = coordinator.last_value_change(cont_id, flow)
    if last_change is None:
        return {}
    return {
        "ultimo_cambio": last_change.isoformat(),
        "minutos_sin_cambiar": round((dt_util.utcnow() - last_change).total_seconds() / 60),
    }


class EdistribucionImportedEnergySensor(_EdistribucionBaseSensor):
    entity_description = SensorEntityDescription(
        key="imported_energy_today",
        translation_key="imported_energy_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    )

    def __init__(self, coordinator, cont_id, supply_point) -> None:
        super().__init__(coordinator, cont_id, supply_point)
        self._attr_unique_id = f"{cont_id}_imported_energy_today"

    @property
    def native_value(self) -> float | None:
        day = _latest_daily_total(self._bundle.get("consumption"))
        return day["importedKwh"] if day else None

    @property
    def extra_state_attributes(self) -> dict:
        return _freshness_attributes(self.coordinator, self._cont_id, "imported")


class EdistribucionExportedEnergySensor(_EdistribucionBaseSensor):
    entity_description = SensorEntityDescription(
        key="exported_energy_today",
        translation_key="exported_energy_today",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    )

    def __init__(self, coordinator, cont_id, supply_point) -> None:
        super().__init__(coordinator, cont_id, supply_point)
        self._attr_unique_id = f"{cont_id}_exported_energy_today"

    @property
    def native_value(self) -> float | None:
        day = _latest_daily_total(self._bundle.get("consumption"))
        return day["exportedKwh"] if day else None

    @property
    def extra_state_attributes(self) -> dict:
        return _freshness_attributes(self.coordinator, self._cont_id, "exported")


class _EdistribucionPeriodEnergySensor(_EdistribucionBaseSensor):
    """Total importado/exportado de un periodo (semana/mes). El detalle día a día del periodo
    queda disponible como atributo `daily_totals` (fecha + kWh de ese día)."""

    _attr_state_class = SensorStateClass.TOTAL
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator, cont_id, supply_point, period_key: str, flow: str, translation_key: str) -> None:
        super().__init__(coordinator, cont_id, supply_point)
        self._period_key = period_key  # "week" | "month"
        self._flow = flow  # "imported" | "exported"
        self._attr_unique_id = f"{cont_id}_{flow}_energy_{period_key}"
        self._attr_translation_key = translation_key

    @property
    def _period(self) -> dict | None:
        return self._bundle.get(self._period_key)

    @property
    def native_value(self) -> float | None:
        period = self._period
        if not period:
            return None
        return period.get(f"total{self._flow.capitalize()}Kwh")

    @property
    def extra_state_attributes(self) -> dict:
        period = self._period
        if not period:
            return {}
        field = f"{self._flow}Kwh"
        return {"daily_totals": [{"date": d["date"], "kwh": d.get(field)} for d in period.get("dailyTotals", [])]}

    @property
    def available(self) -> bool:
        return super().available and self._period is not None


class EdistribucionContractedPowerSensor(_EdistribucionBaseSensor):
    """Potencia contratada (punta) leída EN VIVO de e-distribución — no es un valor que teclees tú,
    así que refleja cambios reales de contrato sin tener que enterarte por la factura. La de valle,
    y los metadatos del contrato (código, estado, comercializadora, tarifa), van como atributos."""

    entity_description = SensorEntityDescription(
        key="contracted_power",
        translation_key="contracted_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=2,
    )

    def __init__(self, coordinator, cont_id, supply_point) -> None:
        super().__init__(coordinator, cont_id, supply_point)
        self._attr_unique_id = f"{cont_id}_contracted_power"

    @property
    def native_value(self) -> float | None:
        contract = self._bundle.get("contract")
        return contract.get("contractedPowerPuntaKw") if contract else None

    @property
    def extra_state_attributes(self) -> dict:
        contract = self._bundle.get("contract")
        if not contract:
            return {}
        return {
            "potencia_valle_kw": contract.get("contractedPowerValleKw"),
            "codigo_contrato": contract.get("contractCode"),
            "estado_contrato": contract.get("status"),
            "comercializadora": contract.get("marketer"),
            "tarifa": contract.get("tariff"),
        }

    @property
    def available(self) -> bool:
        return super().available and self._bundle.get("contract") is not None


class _EdistribucionEstimatedCostSensor(_EdistribucionBaseSensor):
    """Coste estimado de energía según el tipo de tarifa configurado para este CUPS (fija/tramos/
    pvpc, ver opciones), CON el IEE y el IVA de este CUPS aplicados EN ESE ORDEN (ver
    costs.apply_iee/apply_iva — 0% cada uno si no se han configurado). Sigue siendo una ESTIMACIÓN:
    no incluye el término de potencia ni la compensación de excedentes (esos van en sensores aparte,
    ver EdistribucionPowerCostTodaySensor/MonthSensor y EdistribucionSurplusCompensation*Sensor) ni
    el alquiler de equipos de medida — para tener una idea, no para cuadrar con la factura real."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "EUR"
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator, cont_id, supply_point, translation_key: str) -> None:
        super().__init__(coordinator, cont_id, supply_point)
        self._attr_translation_key = translation_key

    @property
    def _imported_kwh(self) -> float | None:
        raise NotImplementedError

    @property
    def _hourly_source(self) -> dict | None:
        raise NotImplementedError

    @property
    def _breakdown(self) -> dict | None:
        sp = self._bundle.get("supply_point") or {}
        return estimate_energy_cost(sp, self._imported_kwh, self._hourly_source, self.coordinator.pvpc_prices)

    @property
    def native_value(self) -> float | None:
        breakdown = self._breakdown
        return breakdown["total"] if breakdown else None

    @property
    def extra_state_attributes(self) -> dict:
        return self._breakdown or {}


class EdistribucionEstimatedCostTodaySensor(_EdistribucionEstimatedCostSensor):
    # device_class=MONETARY (heredado de _EdistribucionEstimatedCostSensor) solo admite None o
    # TOTAL como state_class — TOTAL_INCREASING es inválido para "monetary" y HA rechaza la
    # entidad entera al añadirla ("Error adding entity ... impossible considering device class").
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator, cont_id, supply_point) -> None:
        super().__init__(coordinator, cont_id, supply_point, "estimated_cost_today")
        self._attr_unique_id = f"{cont_id}_estimated_cost_today"

    @property
    def _imported_kwh(self) -> float | None:
        day = _latest_daily_total(self._bundle.get("consumption"))
        return day["importedKwh"] if day else None

    @property
    def _hourly_source(self) -> dict | None:
        return _latest_day_hourly(self._bundle.get("consumption"))

    @property
    def extra_state_attributes(self) -> dict:
        return {**super().extra_state_attributes, **_freshness_attributes(self.coordinator, self._cont_id, "imported")}


class EdistribucionEstimatedCostMonthSensor(_EdistribucionEstimatedCostSensor):
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator, cont_id, supply_point) -> None:
        super().__init__(coordinator, cont_id, supply_point, "estimated_cost_month")
        self._attr_unique_id = f"{cont_id}_estimated_cost_month"

    @property
    def _imported_kwh(self) -> float | None:
        month = self._bundle.get("month")
        return month.get("totalImportedKwh") if month else None

    @property
    def _hourly_source(self) -> dict | None:
        return self._bundle.get("month")


class _EdistribucionTramoSensor(_EdistribucionBaseSensor):
    """kWh o coste de UN tramo (punta/llano/valle) de la tarifa 'tramos' (ver costs.cost_breakdown)
    — mismo cálculo que ya usa EdistribucionEstimatedCostTodaySensor/MonthSensor (que lo trae solo
    como atributo anidado), pero como entidad propia por tramo, para poder graficar o automatizar
    sobre un tramo concreto sin tener que leer un atributo. Solo se crea con tarifa 'tramos' activa
    y algún precio de tramo configurado (ver async_setup_entry) — punta/llano/valle no existen en
    fija/pvpc. El coste de cada tramo lleva el IEE y el IVA de este CUPS aplicados (kind="cost"),
    igual que el total del sensor de coste estimado — kind="kwh" no se ve afectado, claro."""

    _attr_suggested_display_precision = 2

    def __init__(self, coordinator, cont_id, supply_point, period_key: str, tramo: str, kind: str) -> None:
        super().__init__(coordinator, cont_id, supply_point)
        self._period_key = period_key  # "today" | "month"
        self._tramo = tramo  # "punta" | "llano" | "valle"
        self._kind = kind  # "kwh" | "cost"
        translation_key = f"{tramo}_{kind}_{period_key}"
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{cont_id}_{translation_key}"
        if kind == "kwh":
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING if period_key == "today" else SensorStateClass.TOTAL
        else:
            # device_class=MONETARY solo admite None o TOTAL como state_class (ver v1.21.1).
            self._attr_device_class = SensorDeviceClass.MONETARY
            self._attr_native_unit_of_measurement = "EUR"
            self._attr_state_class = SensorStateClass.TOTAL

    @property
    def _hourly_source(self) -> dict | None:
        if self._period_key == "today":
            return _latest_day_hourly(self._bundle.get("consumption"))
        return self._bundle.get("month")

    @property
    def native_value(self) -> float | None:
        sp = self._bundle.get("supply_point") or {}
        prices = {PUNTA: sp.get("price_punta") or 0, LLANO: sp.get("price_llano") or 0, VALLE: sp.get("price_valle") or 0}
        breakdown = cost_breakdown(
            self._hourly_source,
            prices,
            sp.get("holiday_region"),
            iee_percent=sp.get("iee_percent") or 0,
            iva_percent=sp.get("iva_percent") or 0,
            zone=sp.get("pvpc_zone") or DEFAULT_PVPC_ZONE,
        )
        if not breakdown:
            return None
        breakdown_key = f"kwh_{self._tramo}" if self._kind == "kwh" else f"coste_{self._tramo}"
        return breakdown.get(breakdown_key)


class _EdistribucionExportTramoSensor(_EdistribucionBaseSensor):
    """kWh exportados o compensación de UN tramo (punta/llano/valle) — mismo bucketing horario/
    festivo que `_EdistribucionTramoSensor`, pero sobre `exportedKwh` en vez de `importedKwh`. El
    precio de compensación es PLANO (mismo €/kWh todo el día, sin franjas horarias) — el € por
    tramo es solo proporcional al kWh exportado en ESE tramo, no hay un precio distinto que aplicar
    (a diferencia del consumo importado). Solo se crea con compensación de excedentes activada y
    precio configurado — independiente de la tarifa de importación activa, ya que el bucketing
    horario punta/llano/valle no depende de qué tarifa factura el consumo."""

    _attr_suggested_display_precision = 2

    def __init__(self, coordinator, cont_id, supply_point, period_key: str, tramo: str, kind: str) -> None:
        super().__init__(coordinator, cont_id, supply_point)
        self._period_key = period_key  # "today" | "month"
        self._tramo = tramo  # "punta" | "llano" | "valle"
        self._kind = kind  # "kwh" | "compensation"
        translation_key = f"{tramo}_exported_{kind}_{period_key}"
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{cont_id}_{translation_key}"
        if kind == "kwh":
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING if period_key == "today" else SensorStateClass.TOTAL
        else:
            # device_class=MONETARY solo admite None o TOTAL como state_class (ver v1.21.1).
            self._attr_device_class = SensorDeviceClass.MONETARY
            self._attr_native_unit_of_measurement = "EUR"
            self._attr_state_class = SensorStateClass.TOTAL

    @property
    def _hourly_source(self) -> dict | None:
        if self._period_key == "today":
            return _latest_day_hourly(self._bundle.get("consumption"))
        return self._bundle.get("month")

    @property
    def native_value(self) -> float | None:
        sp = self._bundle.get("supply_point") or {}
        price = sp.get("surplus_price") or 0
        prices = {PUNTA: price, LLANO: price, VALLE: price}
        breakdown = cost_breakdown(
            self._hourly_source, prices, sp.get("holiday_region"), field="exportedKwh", zone=sp.get("pvpc_zone") or DEFAULT_PVPC_ZONE
        )
        if not breakdown:
            return None
        breakdown_key = f"kwh_{self._tramo}" if self._kind == "kwh" else f"coste_{self._tramo}"
        return breakdown.get(breakdown_key)


class EdistribucionAveragePriceMonthSensor(_EdistribucionBaseSensor):
    """Precio medio real pagado por kWh este mes (coste total ÷ kWh importados) — para comparar
    contra otras ofertas del mercado sin tener que calcularlo a mano."""

    entity_description = SensorEntityDescription(
        key="average_price_month",
        translation_key="average_price_month",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="EUR/kWh",
        suggested_display_precision=4,
    )

    def __init__(self, coordinator, cont_id, supply_point) -> None:
        super().__init__(coordinator, cont_id, supply_point)
        self._attr_unique_id = f"{cont_id}_average_price_month"

    @property
    def native_value(self) -> float | None:
        sp = self._bundle.get("supply_point") or {}
        month = self._bundle.get("month")
        imported_kwh = month.get("totalImportedKwh") if month else None
        breakdown = estimate_energy_cost(sp, imported_kwh, month, self.coordinator.pvpc_prices)
        cost_total = breakdown.get("total") if breakdown else None
        return average_price_per_kwh(cost_total, imported_kwh)


class EdistribucionYearToDateCostSensor(_EdistribucionBaseSensor):
    """Coste estimado acumulado en lo que va de año: meses ya completados (recalculados una vez al
    día, ver coordinator._async_update_year_to_date_if_needed) + el mes en curso (en vivo, con lo
    que ya se tiene en `bundle["month"]`). Para tarifa pvpc, los meses anteriores al actual pueden
    salir con coste incompleto — no se vuelve a pedir el histórico de precios PVPC día a día a
    ESIOS para no sobrecargar esa API pública, solo se usa lo que ya haya cacheado del mes en
    curso."""

    entity_description = SensorEntityDescription(
        key="year_to_date_cost",
        translation_key="year_to_date_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="EUR",
        suggested_display_precision=2,
        state_class=SensorStateClass.TOTAL,
    )

    def __init__(self, coordinator, cont_id, supply_point) -> None:
        super().__init__(coordinator, cont_id, supply_point)
        self._attr_unique_id = f"{cont_id}_year_to_date_cost"

    @property
    def _current_month_breakdown(self) -> dict | None:
        sp = self._bundle.get("supply_point") or {}
        month = self._bundle.get("month")
        imported_kwh = month.get("totalImportedKwh") if month else None
        return estimate_energy_cost(sp, imported_kwh, month, self.coordinator.pvpc_prices)

    @property
    def native_value(self) -> float:
        completed = self.coordinator.year_to_date_completed_months(self._cont_id)
        current_month_cost = (self._current_month_breakdown or {}).get("total") or 0.0
        return round((completed.get("cost") or 0.0) + current_month_cost, 2)

    @property
    def extra_state_attributes(self) -> dict:
        completed = self.coordinator.year_to_date_completed_months(self._cont_id)
        month = self._bundle.get("month") or {}
        return {
            "kwh_importados_año": round((completed.get("imported_kwh") or 0.0) + (month.get("totalImportedKwh") or 0.0), 2),
            "kwh_exportados_año": round((completed.get("exported_kwh") or 0.0) + (month.get("totalExportedKwh") or 0.0), 2),
        }


class EdistribucionSimulatedCostMonthSensor(_EdistribucionEstimatedCostSensor):
    """Cuánto habría costado ESTE MES con una tarifa DISTINTA a la configurada, sobre el mismo
    consumo real — para comparar sin cambiar de tarifa de verdad. Ver
    `costs.estimate_cost_as_tariff`. Solo se crea si hay datos suficientes para simular esa tarifa
    (precio fijo puesto para simular "fija", algún precio de tramos puesto para simular "tramos" —
    "pvpc" siempre se puede simular, no necesita nada que rellenar)."""

    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator, cont_id, supply_point, simulated_tariff: str) -> None:
        super().__init__(coordinator, cont_id, supply_point, "simulated_cost_month")
        self._attr_unique_id = f"{cont_id}_simulated_cost_{simulated_tariff}_month"
        self._attr_translation_placeholders = {"tarifa": _TARIFF_LABELS.get(simulated_tariff, simulated_tariff)}
        self._simulated_tariff = simulated_tariff

    @property
    def _imported_kwh(self) -> float | None:
        month = self._bundle.get("month")
        return month.get("totalImportedKwh") if month else None

    @property
    def _hourly_source(self) -> dict | None:
        return self._bundle.get("month")

    @property
    def _breakdown(self) -> dict | None:
        sp = self._bundle.get("supply_point") or {}
        return estimate_cost_as_tariff(sp, self._simulated_tariff, self._imported_kwh, self._hourly_source, self.coordinator.pvpc_prices)


class EdistribucionCurrentPvpcPriceSensor(_EdistribucionBaseSensor):
    """Precio PVPC de la hora en curso, CON el IEE y el IVA de este CUPS aplicados EN ESE ORDEN —
    UN solo sensor (no 24 por hora): el resto de precios del día (y de mañana, si ya están
    publicados) van como atributo, no como entidades separadas. El precio sin impuestos (el que
    publica ESIOS/REE tal cual) va como atributo `precio_sin_impuestos`, por si se necesita para
    comparar contra otras fuentes."""

    entity_description = SensorEntityDescription(
        key="current_pvpc_price",
        translation_key="current_pvpc_price",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="EUR/kWh",
        suggested_display_precision=5,
        state_class=SensorStateClass.MEASUREMENT,
    )

    def __init__(self, coordinator, cont_id, supply_point) -> None:
        super().__init__(coordinator, cont_id, supply_point)
        self._attr_unique_id = f"{cont_id}_current_pvpc_price"

    @property
    def _zone(self) -> str:
        sp = self._bundle.get("supply_point") or {}
        return sp.get("pvpc_zone") or DEFAULT_PVPC_ZONE

    @property
    def _iee_percent(self) -> float:
        sp = self._bundle.get("supply_point") or {}
        return sp.get("iee_percent") or 0

    @property
    def _iva_percent(self) -> float:
        sp = self._bundle.get("supply_point") or {}
        return sp.get("iva_percent") or 0

    @property
    def _zone_prices(self) -> dict[str, float]:
        return self.coordinator.pvpc_prices.get(self._zone, {})

    def _with_taxes(self, price: float) -> float:
        return round(price * (1 + self._iee_percent / 100) * (1 + self._iva_percent / 100), 5)

    @property
    def native_value(self) -> float | None:
        now = dt_util.now()
        price = self._zone_prices.get(f"{now.strftime('%d/%m/%Y')} {now.hour}")
        if price is None:
            return None
        return self._with_taxes(price)

    @property
    def extra_state_attributes(self) -> dict:
        prices = self._zone_prices
        now = dt_util.now()
        tomorrow = now + timedelta(days=1)

        def _day_prices(day) -> dict[str, float]:
            date_str = day.strftime("%d/%m/%Y")
            return {
                f"{h:02d}h": self._with_taxes(prices[f"{date_str} {h}"])
                for h in range(24)
                if f"{date_str} {h}" in prices
            }

        return {
            "zona": self._zone,
            "iee_percent": self._iee_percent,
            "iva_percent": self._iva_percent,
            "precio_sin_impuestos": prices.get(f"{now.strftime('%d/%m/%Y')} {now.hour}"),
            "precios_hoy": _day_prices(now),
            "precios_manana": _day_prices(tomorrow),
        }

    @property
    def available(self) -> bool:
        return super().available and bool(self._zone_prices)


class EdistribucionCurrentTramoPriceSensor(_EdistribucionBaseSensor):
    """Precio de energía CON impuestos vigente AHORA MISMO para tarifa 'tramos' — equivalente a
    EdistribucionCurrentPvpcPriceSensor pero para tramos (ver issue #4): un único sensor con el
    periodo actual (punta/llano/valle) y estadísticas del día como atributos, para no tener que
    reimplementar en una plantilla Jinja aparte la lógica horaria que ya vive en costs.py — ese es
    justo el problema de un sensor casero desconectado de las Opciones reales del CUPS (holiday_
    region, pvpc_zone, iee_percent, iva_percent...), que se puede quedar desactualizado si cambias
    la tarifa. Solo se crea con tarifa 'tramos' activa y algún precio de tramo configurado."""

    entity_description = SensorEntityDescription(
        key="current_tramo_price",
        translation_key="current_tramo_price",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="EUR/kWh",
        suggested_display_precision=5,
        state_class=SensorStateClass.MEASUREMENT,
    )

    def __init__(self, coordinator, cont_id, supply_point) -> None:
        super().__init__(coordinator, cont_id, supply_point)
        self._attr_unique_id = f"{cont_id}_current_tramo_price"

    @property
    def _sp(self) -> dict:
        return self._bundle.get("supply_point") or {}

    @property
    def native_value(self) -> float | None:
        sp = self._sp
        now = dt_util.now()
        period = current_period(now, sp.get("holiday_region"), sp.get("pvpc_zone") or DEFAULT_PVPC_ZONE)
        prices = {PUNTA: sp.get("price_punta") or 0, LLANO: sp.get("price_llano") or 0, VALLE: sp.get("price_valle") or 0}
        return apply_iva(apply_iee(prices.get(period, 0), sp.get("iee_percent") or 0), sp.get("iva_percent") or 0)

    @property
    def extra_state_attributes(self) -> dict:
        sp = self._sp
        now = dt_util.now()
        period = current_period(now, sp.get("holiday_region"), sp.get("pvpc_zone") or DEFAULT_PVPC_ZONE)
        today_prices = list(tramo_prices_today(now, sp).values())
        return {
            "periodo_actual": period,
            "precio_minimo_hoy": round(min(today_prices), 5),
            "precio_medio_hoy": round(sum(today_prices) / len(today_prices), 5),
            "precio_maximo_hoy": round(max(today_prices), 5),
        }


class EdistribucionNextTramoPeriodChangeSensor(_EdistribucionBaseSensor):
    """Cuándo cambia el periodo tarifario (punta/llano/valle) respecto al vigente ahora mismo — para
    disparar automatizaciones de carga/consumo EXACTAMENTE en ese instante (con un `trigger: time`
    apuntando a este sensor, ver ejemplo en el README) en vez de a una hora fija a ciegas que asume
    dónde empieza cada periodo (ver issue #4). Va como sensor propio (no atributo del sensor de
    precio) porque un `trigger: time` de Home Assistant necesita el ESTADO de una entidad con
    device_class timestamp, no puede apuntar a un atributo."""

    entity_description = SensorEntityDescription(
        key="next_tramo_period_change",
        translation_key="next_tramo_period_change",
        device_class=SensorDeviceClass.TIMESTAMP,
    )

    def __init__(self, coordinator, cont_id, supply_point) -> None:
        super().__init__(coordinator, cont_id, supply_point)
        self._attr_unique_id = f"{cont_id}_next_tramo_period_change"

    @property
    def _sp(self) -> dict:
        return self._bundle.get("supply_point") or {}

    @property
    def native_value(self):
        sp = self._sp
        now = dt_util.now()
        return next_period_change(now, sp.get("holiday_region"), sp.get("pvpc_zone") or DEFAULT_PVPC_ZONE)

    @property
    def extra_state_attributes(self) -> dict:
        sp = self._sp
        now = dt_util.now()
        next_change = next_period_change(now, sp.get("holiday_region"), sp.get("pvpc_zone") or DEFAULT_PVPC_ZONE)
        siguiente_periodo = current_period(next_change, sp.get("holiday_region"), sp.get("pvpc_zone") or DEFAULT_PVPC_ZONE)
        return {"siguiente_periodo": siguiente_periodo}


class _EdistribucionSurplusCompensationSensor(_EdistribucionBaseSensor):
    """Compensación estimada por excedentes exportados: kWh exportados × precio configurado para
    este CUPS. Solo existe si se ha activado la compensación de excedentes en las opciones."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "EUR"
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator, cont_id, supply_point, translation_key: str) -> None:
        super().__init__(coordinator, cont_id, supply_point)
        self._attr_translation_key = translation_key

    @property
    def _exported_kwh(self) -> float | None:
        raise NotImplementedError

    @property
    def native_value(self) -> float | None:
        sp = self._bundle.get("supply_point") or {}
        return surplus_compensation_value(sp, self._exported_kwh)


class EdistribucionSurplusCompensationTodaySensor(_EdistribucionSurplusCompensationSensor):
    # Igual que en EdistribucionEstimatedCostTodaySensor: device_class=MONETARY no admite
    # TOTAL_INCREASING como state_class.
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator, cont_id, supply_point) -> None:
        super().__init__(coordinator, cont_id, supply_point, "surplus_compensation_today")
        self._attr_unique_id = f"{cont_id}_surplus_compensation_today"

    @property
    def _exported_kwh(self) -> float | None:
        day = _latest_daily_total(self._bundle.get("consumption"))
        return day["exportedKwh"] if day else None

    @property
    def extra_state_attributes(self) -> dict:
        return _freshness_attributes(self.coordinator, self._cont_id, "exported")


class EdistribucionSurplusCompensationWeekSensor(_EdistribucionSurplusCompensationSensor):
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator, cont_id, supply_point) -> None:
        super().__init__(coordinator, cont_id, supply_point, "surplus_compensation_week")
        self._attr_unique_id = f"{cont_id}_surplus_compensation_week"

    @property
    def _exported_kwh(self) -> float | None:
        week = self._bundle.get("week")
        return week.get("totalExportedKwh") if week else None


class EdistribucionSurplusCompensationMonthSensor(_EdistribucionSurplusCompensationSensor):
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator, cont_id, supply_point) -> None:
        super().__init__(coordinator, cont_id, supply_point, "surplus_compensation_month")
        self._attr_unique_id = f"{cont_id}_surplus_compensation_month"

    @property
    def _exported_kwh(self) -> float | None:
        month = self._bundle.get("month")
        return month.get("totalExportedKwh") if month else None


class _EdistribucionSelfConsumptionSensor(_EdistribucionBaseSensor):
    """Grado de AUTOSUFICIENCIA aproximado (%) — calculado solo con importado/exportado de
    e-distribución, no con generación solar real (que el contador no reporta). Ver
    `costs.self_consumption_ratio` para la definición exacta y sus limitaciones (casos límite
    correctos: 0% importado = 100%, nada exportado = 0%). Solo se crea si el CUPS ha exportado
    algo."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 1
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, cont_id, supply_point, translation_key: str) -> None:
        super().__init__(coordinator, cont_id, supply_point)
        self._attr_translation_key = translation_key

    @property
    def _imported_kwh(self) -> float | None:
        raise NotImplementedError

    @property
    def _exported_kwh(self) -> float | None:
        raise NotImplementedError

    @property
    def native_value(self) -> float | None:
        return self_consumption_ratio(self._imported_kwh, self._exported_kwh)


class EdistribucionSelfConsumptionTodaySensor(_EdistribucionSelfConsumptionSensor):
    def __init__(self, coordinator, cont_id, supply_point) -> None:
        super().__init__(coordinator, cont_id, supply_point, "self_consumption_today")
        self._attr_unique_id = f"{cont_id}_self_consumption_today"

    @property
    def _imported_kwh(self) -> float | None:
        day = _latest_daily_total(self._bundle.get("consumption"))
        return day["importedKwh"] if day else None

    @property
    def _exported_kwh(self) -> float | None:
        day = _latest_daily_total(self._bundle.get("consumption"))
        return day["exportedKwh"] if day else None


class EdistribucionSelfConsumptionMonthSensor(_EdistribucionSelfConsumptionSensor):
    def __init__(self, coordinator, cont_id, supply_point) -> None:
        super().__init__(coordinator, cont_id, supply_point, "self_consumption_month")
        self._attr_unique_id = f"{cont_id}_self_consumption_month"

    @property
    def _imported_kwh(self) -> float | None:
        month = self._bundle.get("month")
        return month.get("totalImportedKwh") if month else None

    @property
    def _exported_kwh(self) -> float | None:
        month = self._bundle.get("month")
        return month.get("totalExportedKwh") if month else None


class EdistribucionPowerCostTodaySensor(_EdistribucionBaseSensor):
    """Término de potencia fijo del día de ESTE CUPS, CON IEE + IVA: kW contratados × precio
    €/kW/día, sumando punta y valle, con el IEE y el IVA de este CUPS aplicados encima EN ESE ORDEN
    (ver costs.power_cost) — se factura siempre, no depende del consumo ni de la franja horaria."""

    entity_description = SensorEntityDescription(
        key="power_cost_today",
        translation_key="power_cost_today",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="EUR",
        suggested_display_precision=2,
        # MEASUREMENT es inválido combinado con device_class=MONETARY (HA solo admite None o
        # TOTAL) — HA rechazaba la entidad entera al añadirla.
        state_class=SensorStateClass.TOTAL,
    )

    def __init__(self, coordinator, cont_id, supply_point) -> None:
        super().__init__(coordinator, cont_id, supply_point)
        self._attr_unique_id = f"{cont_id}_power_cost_today"

    @property
    def native_value(self) -> float:
        return power_cost(self._bundle.get("supply_point") or {})

    @property
    def extra_state_attributes(self) -> dict:
        sp = self._bundle.get("supply_point") or {}
        return {
            "potencia_contratada_punta_kw": sp.get("contracted_power_punta_kw") or 0,
            "potencia_contratada_valle_kw": sp.get("contracted_power_valle_kw") or 0,
            "precio_punta_eur_kw_dia": sp.get("price_power_punta") or 0,
            "precio_valle_eur_kw_dia": sp.get("price_power_valle") or 0,
            "iee_percent": sp.get("iee_percent") or 0,
            "iva_percent": sp.get("iva_percent") or 0,
        }


class EdistribucionPowerCostMonthSensor(_EdistribucionBaseSensor):
    """Término de potencia acumulado del mes: coste diario × días ya facturados (los mismos días
    que ya tienen datos de energía, para cuadrar con el resto de sensores "mes")."""

    entity_description = SensorEntityDescription(
        key="power_cost_month",
        translation_key="power_cost_month",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="EUR",
        suggested_display_precision=2,
        state_class=SensorStateClass.TOTAL,
    )

    def __init__(self, coordinator, cont_id, supply_point) -> None:
        super().__init__(coordinator, cont_id, supply_point)
        self._attr_unique_id = f"{cont_id}_power_cost_month"

    @property
    def _days_elapsed(self) -> int:
        month = self._bundle.get("month")
        if not month:
            return 0
        return len(month.get("dailyTotals", []))

    @property
    def native_value(self) -> float:
        daily_cost = power_cost(self._bundle.get("supply_point") or {})
        return round(daily_cost * self._days_elapsed, 4)

    @property
    def extra_state_attributes(self) -> dict:
        sp = self._bundle.get("supply_point") or {}
        daily_cost = power_cost(sp)
        return {
            "dias_facturados": self._days_elapsed,
            "coste_diario": daily_cost,
            "iee_percent": sp.get("iee_percent") or 0,
            "iva_percent": sp.get("iva_percent") or 0,
        }


class EdistribucionEstimatedCostTodayWithPowerSensor(_EdistribucionBaseSensor):
    """Coste total estimado de HOY: coste de energía (igual que EdistribucionEstimatedCostTodaySensor,
    con IEE + IVA si están configurados) + término de potencia del día (igual que
    EdistribucionPowerCostTodaySensor, también con IEE + IVA) — una única cifra de "cuánto llevo
    hoy" sin tener que sumar dos sensores a mano. Solo se crea si hay término de potencia configurado
    (potencia contratada + precio) además del coste de energía — sigue faltando el alquiler de
    equipos de medida (ver costs.py)."""

    # device_class=MONETARY solo admite None o TOTAL como state_class (ver v1.21.1).
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "EUR"
    _attr_suggested_display_precision = 2
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator, cont_id, supply_point) -> None:
        super().__init__(coordinator, cont_id, supply_point)
        self._attr_translation_key = "estimated_cost_today_with_power"
        self._attr_unique_id = f"{cont_id}_estimated_cost_today_with_power"

    @property
    def _energy_cost(self) -> float | None:
        sp = self._bundle.get("supply_point") or {}
        day = _latest_daily_total(self._bundle.get("consumption"))
        imported_kwh = day["importedKwh"] if day else None
        hourly_source = _latest_day_hourly(self._bundle.get("consumption"))
        breakdown = estimate_energy_cost(sp, imported_kwh, hourly_source, self.coordinator.pvpc_prices)
        return breakdown["total"] if breakdown else None

    @property
    def _power_cost(self) -> float:
        return power_cost(self._bundle.get("supply_point") or {})

    @property
    def native_value(self) -> float | None:
        energy_cost = self._energy_cost
        if energy_cost is None:
            return None
        return round(energy_cost + self._power_cost, 4)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            **_freshness_attributes(self.coordinator, self._cont_id, "imported"),
            "coste_energia": self._energy_cost,
            "termino_potencia": self._power_cost,
        }


class EdistribucionEstimatedCostMonthWithPowerSensor(_EdistribucionBaseSensor):
    """Como EdistribucionEstimatedCostTodayWithPowerSensor pero para el mes en curso: coste de
    energía del mes + término de potencia acumulado (igual que EdistribucionPowerCostMonthSensor,
    que solo cuenta los días ya facturados, no el mes completo por adelantado)."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "EUR"
    _attr_suggested_display_precision = 2
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator, cont_id, supply_point) -> None:
        super().__init__(coordinator, cont_id, supply_point)
        self._attr_translation_key = "estimated_cost_month_with_power"
        self._attr_unique_id = f"{cont_id}_estimated_cost_month_with_power"

    @property
    def _energy_cost(self) -> float | None:
        sp = self._bundle.get("supply_point") or {}
        month = self._bundle.get("month")
        imported_kwh = month.get("totalImportedKwh") if month else None
        breakdown = estimate_energy_cost(sp, imported_kwh, month, self.coordinator.pvpc_prices)
        return breakdown["total"] if breakdown else None

    @property
    def _power_cost(self) -> float:
        month = self._bundle.get("month")
        days_elapsed = len(month.get("dailyTotals", [])) if month else 0
        return round(power_cost(self._bundle.get("supply_point") or {}) * days_elapsed, 4)

    @property
    def native_value(self) -> float | None:
        energy_cost = self._energy_cost
        if energy_cost is None:
            return None
        return round(energy_cost + self._power_cost, 4)

    @property
    def extra_state_attributes(self) -> dict:
        return {"coste_energia": self._energy_cost, "termino_potencia": self._power_cost}


class EdistribucionMonthVsLastYearSensor(_EdistribucionBaseSensor):
    """% de cambio del consumo importado de este mes frente al mismo mes del año anterior. Sin
    valor si el contrato es más nuevo que un año (no hay nada que comparar todavía)."""

    entity_description = SensorEntityDescription(
        key="month_vs_last_year",
        translation_key="month_vs_last_year",
        native_unit_of_measurement="%",
        suggested_display_precision=1,
    )

    def __init__(self, coordinator, cont_id, supply_point) -> None:
        super().__init__(coordinator, cont_id, supply_point)
        self._attr_unique_id = f"{cont_id}_month_vs_last_year"

    @property
    def native_value(self) -> float | None:
        this_year = self._bundle.get("month")
        last_year = self._bundle.get("month_last_year")
        if not this_year or not last_year:
            return None
        previous = last_year.get("totalImportedKwh")
        current = this_year.get("totalImportedKwh")
        if not previous:
            return None
        return round((current - previous) / previous * 100, 2)

    @property
    def extra_state_attributes(self) -> dict:
        this_year = self._bundle.get("month")
        last_year = self._bundle.get("month_last_year")
        return {
            "importado_este_mes_kwh": this_year.get("totalImportedKwh") if this_year else None,
            "importado_mismo_mes_año_anterior_kwh": last_year.get("totalImportedKwh") if last_year else None,
        }

    @property
    def available(self) -> bool:
        return super().available and self._bundle.get("month") is not None and self._bundle.get("month_last_year") is not None
