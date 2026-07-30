"""Sensores de e-distribución: importado/exportado (hoy/semana/mes) y potencia máxima demandada."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, TARIFF_FIJA, TARIFF_PVPC
from .coordinator import EdistribucionCoordinator
from .costs import estimate_energy_cost, power_cost, surplus_compensation_value
from .device import hub_device_info


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
        if power_cost(sp) > 0:
            entities.append(EdistribucionPowerCostTodaySensor(coordinator, cont_id, sp))
            entities.append(EdistribucionPowerCostMonthSensor(coordinator, cont_id, sp))
        if sp.get("surplus_compensation") and sp.get("surplus_price"):
            entities.append(EdistribucionSurplusCompensationTodaySensor(coordinator, cont_id, sp))
            entities.append(EdistribucionSurplusCompensationMonthSensor(coordinator, cont_id, sp))

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
        return {
            "date": last.get("date"),
            "hour": last.get("hour"),
            "periods": last.get("periods"),
            "max_value_reported": power.get("maxValue"),
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
