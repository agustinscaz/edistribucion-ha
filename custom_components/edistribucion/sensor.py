"""Sensores de e-distribución: importado/exportado (hoy/semana/mes) y potencia máxima demandada."""

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
    average_price_per_kwh,
    estimate_cost_as_tariff,
    estimate_energy_cost,
    max_power_by_period,
    max_power_reported,
    power_cost,
    self_consumption_ratio,
    surplus_compensation_value,
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
        entities.append(EdistribucionMaxPowerSensor(coordinator, cont_id, sp))
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
        if sp.get("surplus_compensation") and sp.get("surplus_price"):
            entities.append(EdistribucionSurplusCompensationTodaySensor(coordinator, cont_id, sp))
            entities.append(EdistribucionSurplusCompensationMonthSensor(coordinator, cont_id, sp))
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
    sin tener que mirar el histórico a mano."""
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


class EdistribucionMaxPowerSensor(_EdistribucionBaseSensor):
    entity_description = SensorEntityDescription(
        key="max_power_demand",
        translation_key="max_power_demand",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=3,
        entity_registry_enabled_default=True,
    )

    def __init__(self, coordinator, cont_id, supply_point) -> None:
        super().__init__(coordinator, cont_id, supply_point)
        self._attr_unique_id = f"{cont_id}_max_power_demand"

    @property
    def native_value(self) -> float | None:
        power = self._bundle.get("max_power_demand")
        points = power.get("points") if power else None
        if not points:
            return None
        return points[-1].get("valueKw")

    @property
    def extra_state_attributes(self) -> dict:
        power = self._bundle.get("max_power_demand")
        points = power.get("points") if power else None
        if not points:
            return {}
        last = points[-1]
        by_period = max_power_by_period(power)
        return {
            "date": last.get("date"),
            "hour": last.get("hour"),
            "periods": last.get("periods"),
            "max_value_reported": power.get("maxValue"),
            # Máximo real de TODO el periodo devuelto (no solo el último punto) — ver
            # binary_sensor.EdistribucionPowerExcessSensor para la comparación contra lo contratado.
            "maximo_real_kw": max_power_reported(power),
            **({"maximo_por_periodo_kw": by_period} if by_period else {}),
        }

    @property
    def available(self) -> bool:
        return super().available and self._bundle.get("max_power_demand") is not None


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
    pvpc, ver opciones). Es una estimación (no considera festivos, término de potencia, ni
    excedentes — esos van en sensores aparte) — para tener una idea, no para cuadrar con la
    factura real."""

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
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

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
    """Precio PVPC de la hora en curso — UN solo sensor (no 24 por hora): el resto de precios del
    día (y de mañana, si ya están publicados) van como atributo, no como entidades separadas."""

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
    def _zone_prices(self) -> dict[str, float]:
        return self.coordinator.pvpc_prices.get(self._zone, {})

    @property
    def native_value(self) -> float | None:
        now = dt_util.now()
        return self._zone_prices.get(f"{now.strftime('%d/%m/%Y')} {now.hour}")

    @property
    def extra_state_attributes(self) -> dict:
        prices = self._zone_prices
        now = dt_util.now()
        tomorrow = now + timedelta(days=1)

        def _day_prices(day) -> dict[str, float]:
            date_str = day.strftime("%d/%m/%Y")
            return {f"{h:02d}h": prices[f"{date_str} {h}"] for h in range(24) if f"{date_str} {h}" in prices}

        return {
            "zona": self._zone,
            "precios_hoy": _day_prices(now),
            "precios_manana": _day_prices(tomorrow),
        }

    @property
    def available(self) -> bool:
        return super().available and bool(self._zone_prices)


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
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator, cont_id, supply_point) -> None:
        super().__init__(coordinator, cont_id, supply_point, "surplus_compensation_today")
        self._attr_unique_id = f"{cont_id}_surplus_compensation_today"

    @property
    def _exported_kwh(self) -> float | None:
        day = _latest_daily_total(self._bundle.get("consumption"))
        return day["exportedKwh"] if day else None


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
    """Término de potencia fijo del día de ESTE CUPS: kW contratados × precio €/kW/día, sumando punta y valle — se
    factura siempre, no depende del consumo ni de la franja horaria."""

    entity_description = SensorEntityDescription(
        key="power_cost_today",
        translation_key="power_cost_today",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="EUR",
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
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
        daily_cost = power_cost(self._bundle.get("supply_point") or {})
        return {"dias_facturados": self._days_elapsed, "coste_diario": daily_cost}


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
