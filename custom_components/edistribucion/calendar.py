"""Calendario de consumo diario por suministro: permite navegar día a día / mes a mes en el
Dashboard de Calendario de Home Assistant, viendo lo importado/exportado de cada día como si fuera
un evento. Pide los datos al add-on bajo demanda para el mes que se esté mirando (no solo el mes
actual que ya cachea el coordinator), reutilizando el parámetro `date` de referencia."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import EdistribucionApiError
from .const import DOMAIN
from .coordinator import EdistribucionCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: EdistribucionCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        EdistribucionConsumptionCalendar(coordinator, cont_id, bundle["supply_point"])
        for cont_id, bundle in coordinator.data.items()
    ]
    async_add_entities(entities)


def _latest_daily_total(consumption: dict | None) -> dict | None:
    if not consumption or not consumption.get("dailyTotals"):
        return None
    return max(consumption["dailyTotals"], key=lambda d: datetime.strptime(d["date"], "%d/%m/%Y"))


def _day_to_event(day: dict) -> CalendarEvent | None:
    try:
        day_date = datetime.strptime(day["date"], "%d/%m/%Y").date()
    except (KeyError, ValueError, TypeError):
        return None
    imported = day.get("importedKwh") or 0
    exported = day.get("exportedKwh") or 0
    summary = f"⚡ {imported:.2f} kWh importados"
    if exported:
        summary += f" · {exported:.2f} kWh exportados"
    return CalendarEvent(start=day_date, end=day_date + timedelta(days=1), summary=summary)


class EdistribucionConsumptionCalendar(CoordinatorEntity[EdistribucionCoordinator], CalendarEntity):
    """Un evento de calendario por día con datos, con el kWh importado/exportado en el título."""

    _attr_has_entity_name = True
    _attr_translation_key = "consumption_calendar"

    def __init__(self, coordinator: EdistribucionCoordinator, cont_id: str, supply_point: dict) -> None:
        super().__init__(coordinator)
        self._cont_id = cont_id
        cups = supply_point.get("cups", cont_id)
        name = supply_point.get("alias") or f"e-distribución {cups}"
        self._attr_unique_id = f"{cont_id}_consumption_calendar"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, cont_id)},
            name=name,
            manufacturer="e-distribución",
            model=supply_point.get("tariff"),
            via_device=(DOMAIN, coordinator.entry_id),
        )

    @property
    def event(self) -> CalendarEvent | None:
        """El 'evento actual/próximo' que pide HA fuera del Dashboard de Calendario: el día más
        reciente con datos (ya cacheado por el coordinator, sin llamada extra)."""
        bundle = self.coordinator.data.get(self._cont_id, {})
        day = _latest_daily_total(bundle.get("consumption"))
        return _day_to_event(day) if day else None

    async def async_get_events(self, hass: HomeAssistant, start_date: datetime, end_date: datetime) -> list[CalendarEvent]:
        """Lo que pide el Dashboard de Calendario al navegar — pedimos al add-on un 'mes' por cada
        mes distinto que solape con el rango visible (normalmente 1, a veces 2 en la vista mensual)."""
        events: list[CalendarEvent] = []
        range_start = start_date.date()
        range_end = end_date.date()
        cursor = range_start.replace(day=1)  # primer día del mes de inicio, para iterar mes a mes exacto

        while cursor <= range_end:
            try:
                data = await self.coordinator.client.async_get_consumption(self._cont_id, "3", cursor.strftime("%Y-%m-%d"))
            except EdistribucionApiError:
                data = None
            for day in (data or {}).get("dailyTotals", []):
                event = _day_to_event(day)
                if event and range_start <= event.start <= range_end:
                    events.append(event)

            cursor = (cursor.replace(year=cursor.year + 1, month=1) if cursor.month == 12 else cursor.replace(month=cursor.month + 1))
        return events
