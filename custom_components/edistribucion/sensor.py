"""Sensores de e-distribución: importado/exportado de hoy y potencia máxima demandada."""

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

    entities: list[SensorEntity] = []
    for cont_id, bundle in coordinator.data.items():
        sp = bundle["supply_point"]
        entities.append(EdistribucionImportedEnergySensor(coordinator, cont_id, sp))
        entities.append(EdistribucionExportedEnergySensor(coordinator, cont_id, sp))
        entities.append(EdistribucionMaxPowerSensor(coordinator, cont_id, sp))

    async_add_entities(entities)


class _EdistribucionBaseSensor(CoordinatorEntity[EdistribucionCoordinator], SensorEntity):
    """Sensor de un punto de suministro concreto (identificado por contId)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EdistribucionCoordinator, cont_id: str, supply_point: dict) -> None:
        super().__init__(coordinator)
        self._cont_id = cont_id
        cups = supply_point.get("cups", cont_id)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, cont_id)},
            name=f"e-distribución {cups}",
            manufacturer="e-distribución",
            model=supply_point.get("tariff"),
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
    )

    def __init__(self, coordinator, cont_id, supply_point) -> None:
        super().__init__(coordinator, cont_id, supply_point)
        self._attr_unique_id = f"{cont_id}_exported_energy_today"

    @property
    def native_value(self) -> float | None:
        day = _latest_daily_total(self._bundle.get("consumption"))
        return day["exportedKwh"] if day else None


class EdistribucionMaxPowerSensor(_EdistribucionBaseSensor):
    entity_description = SensorEntityDescription(
        key="max_power_demand",
        translation_key="max_power_demand",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
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
    def available(self) -> bool:
        return super().available and self._bundle.get("max_power_demand") is not None
