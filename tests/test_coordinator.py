"""Tests de coordinator.py — fusión de opciones en el dict del suministro, caché de precios PVPC
por zona, y manejo de fallos (Repairs). Necesita objetos reales de Home Assistant — se verifica vía
CI, no en el sandbox de desarrollo local (sin pip)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.edistribucion.api import EdistribucionApiError, InvalidCredentialsError
from custom_components.edistribucion.const import CONF_SUPPLY_POINTS, DOMAIN
from custom_components.edistribucion.coordinator import EdistribucionCoordinator

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def _supply_point(cont_id="cont1", cups="ES123", cups_id="cups1", active=True):
    return {
        "contId": cont_id,
        "cupsId": cups_id,
        "cups": cups,
        "address": "Calle Falsa 123",
        "tariff": "2.0TD",
        "active": active,
        "startDate": "2026-01-01",
        "endDate": None,
    }


def _make_client(supply_points=None):
    client = AsyncMock()
    client.async_get_supply_points.return_value = supply_points if supply_points is not None else [_supply_point()]
    client.async_get_consumption.return_value = {"totalImportedKwh": 5.0, "hourlyByDate": {}}
    client.async_get_contracted_power.return_value = {
        "contractedPowerPuntaKw": 3.5,
        "contractedPowerValleKw": 3.5,
        "status": "EN VIGOR",
    }
    return client


def _make_entry(hass, options=None):
    entry = MockConfigEntry(domain=DOMAIN, data={"host": "localhost", "port": 8099}, options=options or {})
    entry.add_to_hass(hass)
    return entry


async def test_basic_bundle_shape(hass):
    entry = _make_entry(hass)
    client = _make_client()
    coordinator = EdistribucionCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert set(data) == {"cont1"}
    bundle = data["cont1"]
    assert bundle["supply_point"]["cups"] == "ES123"
    assert bundle["consumption"]["totalImportedKwh"] == 5.0
    assert bundle["contract"]["contractedPowerPuntaKw"] == 3.5


async def test_untracked_supply_point_is_excluded(hass):
    entry = _make_entry(hass, options={CONF_SUPPLY_POINTS: {"cont1": {"track": False}}})
    client = _make_client()
    coordinator = EdistribucionCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert data == {}


async def test_options_are_merged_into_supply_point_dict(hass):
    entry = _make_entry(
        hass,
        options={CONF_SUPPLY_POINTS: {"cont1": {"track": True, "alias": "Casa", "tariff_type": "fija", "fixed_price": 0.2}}},
    )
    client = _make_client()
    coordinator = EdistribucionCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    sp = data["cont1"]["supply_point"]
    assert sp["alias"] == "Casa"
    assert sp["tariff_type"] == "fija"
    assert sp["fixed_price"] == 0.2
    assert "track" not in sp  # "track" es solo para filtrar, no debe colarse en el dict del CUPS


async def test_contracted_power_overwrites_merged_options(hass):
    """La potencia contratada real (del endpoint) debe prevalecer, no la que hubiera en opciones
    antiguas (ver migración de versiones anteriores)."""
    entry = _make_entry(hass, options={CONF_SUPPLY_POINTS: {"cont1": {"contracted_power_punta_kw": 999}}})
    client = _make_client()
    coordinator = EdistribucionCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert data["cont1"]["supply_point"]["contracted_power_punta_kw"] == 3.5


async def test_contracted_power_failure_does_not_crash_bundle(hass):
    entry = _make_entry(hass)
    client = _make_client()
    client.async_get_contracted_power.side_effect = EdistribucionApiError("caído")
    coordinator = EdistribucionCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert data["cont1"]["contract"] is None


async def test_invalid_credentials_raises_update_failed_and_creates_repair(hass):
    entry = _make_entry(hass)
    client = _make_client()
    client.async_get_supply_points.side_effect = InvalidCredentialsError("credenciales malas")
    coordinator = EdistribucionCoordinator(hass, client, entry)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"invalid_credentials_{entry.entry_id}")
    assert issue is not None


async def test_generic_error_creates_repair_after_threshold(hass):
    from custom_components.edistribucion.const import CONSECUTIVE_FAILURES_FOR_REPAIR

    entry = _make_entry(hass)
    client = _make_client()
    client.async_get_supply_points.side_effect = EdistribucionApiError("red caída")
    coordinator = EdistribucionCoordinator(hass, client, entry)

    for i in range(CONSECUTIVE_FAILURES_FOR_REPAIR):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
        issue = ir.async_get(hass).async_get_issue(DOMAIN, f"addon_connection_failed_{entry.entry_id}")
        if i < CONSECUTIVE_FAILURES_FOR_REPAIR - 1:
            assert issue is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"addon_connection_failed_{entry.entry_id}") is not None


async def test_successful_update_clears_repairs(hass):
    entry = _make_entry(hass)
    client = _make_client()
    coordinator = EdistribucionCoordinator(hass, client, entry)
    ir.async_create_issue(
        hass, DOMAIN, f"addon_connection_failed_{entry.entry_id}", is_fixable=False, severity=ir.IssueSeverity.ERROR, translation_key="x"
    )

    await coordinator._async_update_data()

    assert ir.async_get(hass).async_get_issue(DOMAIN, f"addon_connection_failed_{entry.entry_id}") is None


async def test_value_freshness_tracks_change_and_stays_stable(hass):
    """La curva horaria de e-distribución se publica con retraso — el timestamp de "último cambio"
    solo debe avanzar cuando el valor de verdad cambia, no en cada ciclo del coordinator."""
    entry = _make_entry(hass)
    client = _make_client()
    client.async_get_consumption.return_value = {
        "totalImportedKwh": 5.0,
        "dailyTotals": [{"date": "30/07/2026", "importedKwh": 5.0, "exportedKwh": 0.0}],
        "hourlyByDate": {},
    }
    coordinator = EdistribucionCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    first_change = coordinator.last_value_change("cont1", "imported")
    assert first_change is not None

    await coordinator._async_update_data()  # mismo valor -> no debe moverse el timestamp
    assert coordinator.last_value_change("cont1", "imported") == first_change

    client.async_get_consumption.return_value = {
        "totalImportedKwh": 6.0,
        "dailyTotals": [{"date": "30/07/2026", "importedKwh": 6.0, "exportedKwh": 0.0}],
        "hourlyByDate": {},
    }
    await coordinator._async_update_data()
    assert coordinator.last_value_change("cont1", "imported") > first_change


async def test_value_freshness_none_without_daily_totals(hass):
    entry = _make_entry(hass)
    coordinator = EdistribucionCoordinator(hass, _make_client(), entry)
    await coordinator._async_update_data()
    assert coordinator.last_value_change("cont1", "imported") is None


async def test_backfill_runs_once_per_day(hass, monkeypatch):
    entry = _make_entry(hass)
    coordinator = EdistribucionCoordinator(hass, _make_client(), entry)

    calls = {"n": 0}

    async def fake_backfill(hass_arg, cups, month_data):
        calls["n"] += 1

    monkeypatch.setattr("custom_components.edistribucion.coordinator.async_backfill_energy_statistics", fake_backfill)

    await coordinator._async_update_data()
    assert calls["n"] == 1

    await coordinator._async_update_data()
    assert calls["n"] == 1  # mismo día, no se repite

    coordinator._last_backfill_day = None  # simula que cambió el día
    await coordinator._async_update_data()
    assert calls["n"] == 2


async def test_year_to_date_sums_completed_months_plus_current_month(hass, monkeypatch):
    entry = _make_entry(hass, options={CONF_SUPPLY_POINTS: {"cont1": {"tariff_type": "fija", "fixed_price": 0.2}}})
    client = _make_client()

    async def fake_consumption(cont_id, range_type=None, date=None):
        if date in ("2026-01-01", "2026-02-01"):  # meses YA COMPLETADOS (ene, feb) pedidos por el YTD
            return {"totalImportedKwh": 10.0, "totalExportedKwh": 1.0}
        return {"totalImportedKwh": 5.0, "totalExportedKwh": 0.0, "hourlyByDate": {}}

    client.async_get_consumption.side_effect = fake_consumption
    coordinator = EdistribucionCoordinator(hass, client, entry)
    monkeypatch.setattr(
        "custom_components.edistribucion.coordinator.dt_util.now", lambda: datetime(2026, 3, 15, tzinfo=timezone.utc)
    )

    await coordinator._async_update_data()

    completed = coordinator.year_to_date_completed_months("cont1")
    assert completed["imported_kwh"] == 20.0  # 2 meses completados (ene, feb) x 10 kWh
    assert completed["exported_kwh"] == 2.0
    assert completed["cost"] == pytest.approx(4.0)  # 20 kWh x 0.2 €/kWh (fija)


async def test_year_to_date_no_completed_months_in_january(hass, monkeypatch):
    entry = _make_entry(hass, options={CONF_SUPPLY_POINTS: {"cont1": {"tariff_type": "fija", "fixed_price": 0.2}}})
    coordinator = EdistribucionCoordinator(hass, _make_client(), entry)
    monkeypatch.setattr(
        "custom_components.edistribucion.coordinator.dt_util.now", lambda: datetime(2026, 1, 10, tzinfo=timezone.utc)
    )

    await coordinator._async_update_data()

    completed = coordinator.year_to_date_completed_months("cont1")
    assert completed == {"imported_kwh": 0.0, "exported_kwh": 0.0, "cost": 0.0}


async def test_year_to_date_checks_at_most_once_per_day(hass, monkeypatch):
    entry = _make_entry(hass, options={CONF_SUPPLY_POINTS: {"cont1": {"tariff_type": "fija", "fixed_price": 0.2}}})
    client = _make_client()
    calls = {"n": 0}

    async def fake_consumption(cont_id, range_type=None, date=None):
        if date == "2026-01-01":
            calls["n"] += 1
            return {"totalImportedKwh": 10.0, "totalExportedKwh": 0.0}
        return {"totalImportedKwh": 5.0, "totalExportedKwh": 0.0, "hourlyByDate": {}}

    client.async_get_consumption.side_effect = fake_consumption
    coordinator = EdistribucionCoordinator(hass, client, entry)
    monkeypatch.setattr(
        "custom_components.edistribucion.coordinator.dt_util.now", lambda: datetime(2026, 2, 15, tzinfo=timezone.utc)
    )

    await coordinator._async_update_data()
    assert calls["n"] == 1

    await coordinator._async_update_data()
    assert calls["n"] == 1  # mismo día, no se repite


async def test_year_to_date_never_refetches_an_already_closed_month(hass, monkeypatch):
    """El punto clave de la caché permanente: un mes ya cerrado y cacheado no se vuelve a pedir
    NUNCA, ni siquiera al "cambiar de día" (a diferencia del backfill/pvpc, que sí se repiten cada
    día) — un mes cerrado no cambia jamás, así que solo cambiar de MES (revelar un mes nuevo
    completado) debe generar una petición nueva."""
    entry = _make_entry(hass, options={CONF_SUPPLY_POINTS: {"cont1": {"tariff_type": "fija", "fixed_price": 0.2}}})
    client = _make_client()
    calls = {"n": 0}

    async def fake_consumption(cont_id, range_type=None, date=None):
        if date in ("2026-01-01", "2026-02-01"):
            calls["n"] += 1
            return {"totalImportedKwh": 10.0, "totalExportedKwh": 0.0}
        return {"totalImportedKwh": 5.0, "totalExportedKwh": 0.0, "hourlyByDate": {}}

    client.async_get_consumption.side_effect = fake_consumption
    coordinator = EdistribucionCoordinator(hass, client, entry)
    monkeypatch.setattr(
        "custom_components.edistribucion.coordinator.dt_util.now", lambda: datetime(2026, 2, 15, tzinfo=timezone.utc)
    )

    await coordinator._async_update_data()
    assert calls["n"] == 1  # solo enero está completado en febrero

    # Simula que pasó un día (sin cambiar de mes): enero ya está cacheado, NO debe volver a pedirse.
    coordinator._year_to_date_fetched_day = None
    await coordinator._async_update_data()
    assert calls["n"] == 1

    # Cambia el mes: ahora febrero también está completado -> una petición nueva, SOLO para febrero
    # (enero sigue en caché, no se vuelve a pedir).
    monkeypatch.setattr(
        "custom_components.edistribucion.coordinator.dt_util.now", lambda: datetime(2026, 3, 15, tzinfo=timezone.utc)
    )
    coordinator._year_to_date_fetched_day = None
    await coordinator._async_update_data()
    assert calls["n"] == 2


class TestPvpcZonesNeeded:
    def test_only_tracked_supply_points_count(self, hass):
        entry = _make_entry(
            hass,
            options={
                CONF_SUPPLY_POINTS: {
                    "c1": {"track": True, "pvpc_zone": "PCB"},
                    "c2": {"track": False, "pvpc_zone": "CYM"},
                }
            },
        )
        coordinator = EdistribucionCoordinator(hass, _make_client(), entry)
        assert coordinator._pvpc_zones_needed() == {"PCB"}

    def test_defaults_to_pcb_when_zone_unset(self, hass):
        entry = _make_entry(hass, options={CONF_SUPPLY_POINTS: {"c1": {"track": True}}})
        coordinator = EdistribucionCoordinator(hass, _make_client(), entry)
        assert coordinator._pvpc_zones_needed() == {"PCB"}

    def test_needed_even_when_tariff_is_not_pvpc(self, hass):
        """El simulador de tarifas necesita el precio pvpc aunque la tarifa activa sea otra."""
        entry = _make_entry(
            hass, options={CONF_SUPPLY_POINTS: {"c1": {"track": True, "tariff_type": "fija", "pvpc_zone": "CYM"}}}
        )
        coordinator = EdistribucionCoordinator(hass, _make_client(), entry)
        assert coordinator._pvpc_zones_needed() == {"CYM"}


async def test_pvpc_prices_fetched_once_per_day(hass, monkeypatch):
    entry = _make_entry(hass, options={CONF_SUPPLY_POINTS: {"c1": {"track": True, "pvpc_zone": "PCB"}}})
    coordinator = EdistribucionCoordinator(hass, _make_client(), entry)

    call_count = {"n": 0}

    async def fake_fetch(session, zone, day):
        call_count["n"] += 1
        return {f"{day.strftime('%d/%m/%Y')} 0": 0.1}

    monkeypatch.setattr("custom_components.edistribucion.coordinator.async_get_pvpc_prices_for_day", fake_fetch)

    await coordinator._async_update_pvpc_prices()
    first_call_count = call_count["n"]
    assert first_call_count > 0

    await coordinator._async_update_pvpc_prices()
    assert call_count["n"] == first_call_count  # no repite peticiones el mismo día


async def test_force_refresh_bypasses_daily_cache(hass, monkeypatch):
    """Si el día de mañana aún no está publicado en ESIOS, la primera pasada lo deja sin cachear
    (rompe el bucle con EsiosError). Un refresco forzado, una vez publicado, debe volver a pedirlo
    — no basta con resetear `_pvpc_fetched_date`, ya que el caché por día (`zone_prices`) seguiría
    cubriendo el resto del mes y no generaría ninguna petición nueva."""
    from custom_components.edistribucion.esios import EsiosError

    entry = _make_entry(hass, options={CONF_SUPPLY_POINTS: {"c1": {"track": True, "pvpc_zone": "PCB"}}})
    client = _make_client()
    coordinator = EdistribucionCoordinator(hass, client, entry)

    call_count = {"n": 0}
    tomorrow_published = {"flag": False}

    async def fake_fetch(session, zone, day):
        call_count["n"] += 1
        from homeassistant.util import dt as dt_util

        if day > dt_util.now().date() and not tomorrow_published["flag"]:
            raise EsiosError("aún no publicado")
        return {f"{day.strftime('%d/%m/%Y')} 0": 0.1}

    monkeypatch.setattr("custom_components.edistribucion.coordinator.async_get_pvpc_prices_for_day", fake_fetch)

    await coordinator._async_update_pvpc_prices()
    n_after_first = call_count["n"]

    tomorrow_published["flag"] = True
    await coordinator.async_force_refresh_pvpc_prices()
    assert call_count["n"] > n_after_first  # el refresco forzado sí volvió a pedir el día que faltaba


async def test_esios_error_does_not_crash_update(hass, monkeypatch):
    from custom_components.edistribucion.esios import EsiosError

    entry = _make_entry(hass, options={CONF_SUPPLY_POINTS: {"c1": {"track": True, "pvpc_zone": "PCB"}}})
    coordinator = EdistribucionCoordinator(hass, _make_client(), entry)

    async def failing_fetch(session, zone, day):
        raise EsiosError("ESIOS caído")

    monkeypatch.setattr("custom_components.edistribucion.coordinator.async_get_pvpc_prices_for_day", failing_fetch)

    await coordinator._async_update_pvpc_prices()  # no debe lanzar
    assert coordinator.pvpc_prices.get("PCB", {}) == {}


async def test_pvpc_cache_survives_simulated_restart(hass, monkeypatch):
    """Un reinicio de HA crea una instancia NUEVA de EdistribucionCoordinator — sin persistencia a
    disco, el caché en memoria se perdía por completo (issue reportado: "reinicié hoy y tuvo que
    volver a pedir todo el mes"). Simulamos el reinicio con dos instancias distintas sobre el mismo
    entry_id (mismo Store)."""
    from homeassistant.util import dt as dt_util

    entry = _make_entry(hass, options={CONF_SUPPLY_POINTS: {"c1": {"track": True, "pvpc_zone": "PCB"}}})

    async def fake_fetch(session, zone, day):
        return {f"{day.strftime('%d/%m/%Y')} 0": 0.1}

    monkeypatch.setattr("custom_components.edistribucion.coordinator.async_get_pvpc_prices_for_day", fake_fetch)

    coordinator_before = EdistribucionCoordinator(hass, _make_client(), entry)
    await coordinator_before._async_update_pvpc_prices()
    assert coordinator_before.pvpc_prices["PCB"]  # se rellenó de verdad

    # "Reinicio": instancia nueva, memoria en blanco, mismo entry_id (mismo archivo de Store)
    coordinator_after = EdistribucionCoordinator(hass, _make_client(), entry)
    assert coordinator_after.pvpc_prices == {}
    await coordinator_after.async_load_pvpc_prices_cache()
    assert coordinator_after.pvpc_prices["PCB"] == coordinator_before.pvpc_prices["PCB"]

    call_count = {"n": 0}

    async def counting_fetch(session, zone, day):
        call_count["n"] += 1
        return {f"{day.strftime('%d/%m/%Y')} 0": 0.1}

    monkeypatch.setattr("custom_components.edistribucion.coordinator.async_get_pvpc_prices_for_day", counting_fetch)
    await coordinator_after._async_update_pvpc_prices()
    assert call_count["n"] == 0  # ya estaba todo cacheado — no hizo falta volver a pedirle nada a ESIOS


async def test_pvpc_cache_load_discards_stale_month_entries(hass):
    """Precios de un mes YA PASADO en el archivo (p.ej. quedaron de antes de este cambio, o el
    reinicio ocurrió cruzando un mes) no sirven para nada — no deberían colarse en memoria."""
    from homeassistant.util import dt as dt_util

    entry = _make_entry(hass, options={CONF_SUPPLY_POINTS: {"c1": {"track": True, "pvpc_zone": "PCB"}}})
    coordinator = EdistribucionCoordinator(hass, _make_client(), entry)

    now = dt_util.now()
    stale_month = now.replace(day=1) - timedelta(days=1)
    await coordinator._pvpc_store.async_save(
        {
            "PCB": {
                f"{stale_month.strftime('%d/%m/%Y')} 0": 0.05,
                f"{now.strftime('%d/%m/%Y')} 0": 0.1,
            }
        }
    )

    await coordinator.async_load_pvpc_prices_cache()
    assert set(coordinator.pvpc_prices["PCB"]) == {f"{now.strftime('%d/%m/%Y')} 0"}


async def test_pvpc_save_prunes_stale_month_entries(hass):
    """Simétrico al de carga: al guardar, tampoco se debe volcar a disco un mes ya pasado que
    hubiera quedado en memoria (evita que el archivo crezca sin límite ciclo a ciclo)."""
    from homeassistant.util import dt as dt_util

    entry = _make_entry(hass, options={CONF_SUPPLY_POINTS: {"c1": {"track": True, "pvpc_zone": "PCB"}}})
    coordinator = EdistribucionCoordinator(hass, _make_client(), entry)

    now = dt_util.now()
    stale_month = now.replace(day=1) - timedelta(days=1)
    coordinator.pvpc_prices["PCB"] = {
        f"{stale_month.strftime('%d/%m/%Y')} 0": 0.05,
        f"{now.strftime('%d/%m/%Y')} 0": 0.1,
    }

    await coordinator._async_save_pvpc_prices_cache()
    stored = await coordinator._pvpc_store.async_load()
    assert set(stored["PCB"]) == {f"{now.strftime('%d/%m/%Y')} 0"}


async def test_load_pvpc_cache_noop_without_stored_file(hass):
    entry = _make_entry(hass, options={CONF_SUPPLY_POINTS: {"c1": {"track": True, "pvpc_zone": "PCB"}}})
    coordinator = EdistribucionCoordinator(hass, _make_client(), entry)
    await coordinator.async_load_pvpc_prices_cache()  # no debe lanzar sin archivo previo
    assert coordinator.pvpc_prices == {}
