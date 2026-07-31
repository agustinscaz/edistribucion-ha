"""Binary sensors: ¿pudo la última actualización hablar con el add-on sin error? ¿ha superado
alguna vez la potencia máxima real la potencia contratada?"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EdistribucionCoordinator
from .costs import max_power_by_period, max_power_reported, power_excess_detected
from .device import hub_device_info


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: EdistribucionCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = [EdistribucionConnectivitySensor(coordinator, entry)]
    for cont_id, bundle in coordinator.data.items():
        entities.append(EdistribucionPowerExcessSensor(coordinator, cont_id, bundle["supply_point"]))
    async_add_entities(entities)


class EdistribucionConnectivitySensor(CoordinatorEntity[EdistribucionCoordinator], BinarySensorEntity):
    """ON = la última actualización pudo hablar con el add-on sin error."""

    _attr_has_entity_name = True
    entity_description = BinarySensorEntityDescription(
        key="connected",
        translation_key="connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
    )

    def __init__(self, coordinator: EdistribucionCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_connected"
        self._attr_device_info = hub_device_info(entry.entry_id)

    @property
    def is_on(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def available(self) -> bool:
        # Este sensor en concreto SIEMPRE está disponible: es precisamente el que informa de si
        # hay conexión o no (si fuera "no disponible" cuando falla, no serviría para eso).
        return True


class EdistribucionPowerExcessSensor(CoordinatorEntity[EdistribucionCoordinator], BinarySensorEntity):
    """ON si la potencia máxima real demandada (según e-distribución, no una estimación propia) ha
    superado la potencia contratada — útil para confirmar con datos oficiales si algún margen de
    seguridad calculado a mano (p.ej. una automatización de carga nocturna) se quedó corto alguna
    vez. Sin valor si no hay telegestión o no se pudo leer el contrato."""

    _attr_has_entity_name = True
    entity_description = BinarySensorEntityDescription(
        key="power_excess",
        translation_key="power_excess",
        device_class=BinarySensorDeviceClass.PROBLEM,
    )

    def __init__(self, coordinator: EdistribucionCoordinator, cont_id: str, supply_point: dict) -> None:
        super().__init__(coordinator)
        self._cont_id = cont_id
        self._attr_unique_id = f"{cont_id}_power_excess"
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

    @property
    def is_on(self) -> bool | None:
        return power_excess_detected(self._bundle.get("max_power_demand"), self._bundle.get("contract"))

    @property
    def extra_state_attributes(self) -> dict:
        power = self._bundle.get("max_power_demand")
        contract = self._bundle.get("contract") or {}
        by_period = max_power_by_period(power)
        return {
            "maximo_real_kw": max_power_reported(power),
            "potencia_contratada_punta_kw": contract.get("contractedPowerPuntaKw"),
            "potencia_contratada_valle_kw": contract.get("contractedPowerValleKw"),
            **({"maximo_por_periodo_kw": by_period} if by_period else {}),
        }

    @property
    def available(self) -> bool:
        return super().available and self.is_on is not None
