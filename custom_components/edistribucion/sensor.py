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

from .const import DOMAIN
from .coordinator import EdistribucionCoordinator
from .device import hub_device_info


def _latest_daily_total(consumption: dict | None) -> dict | None:
    """El add-on devuelve varios días (dailyTotals, DD/MM/YYYY) — nos quedamos con el más reciente."""
    if not consumption or not consumption.get("dailyTotals"):
        return None
    return max(
        consumption["dailyTotals"],
        key=lambda d: datetime.strptime(d["date"], "%d/%m/%Y"),
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: EdistribucionCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = [EdistribucionLastUpdateSensor(coordinator, entry.entry_id)]
    for cont_id, bundle in coordinator.data.items():
        sp = bundle["supply_point"]
        entities.append(EdistribucionImportedEnergySensor(coordinator, cont_id, sp))
        entities.append(EdistribucionExportedEnergySensor(coordinator, cont_id, sp))
        for period_key, period_label in (("week", "week"), ("month", "month")):
            entities.append(_EdistribucionPeriodEnergySensor(coordinator, cont_id, sp, period_key, "imported", f"imported_energy_{period_label}"))
            entities.append(_EdistribucionPeriodEnergySensor(coordinator, cont_id, sp, period_key, "exported", f"exported_energy_{period_label}"))
        entities.append(EdistribucionMaxPowerSensor(coordinator, cont_id, sp))

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
