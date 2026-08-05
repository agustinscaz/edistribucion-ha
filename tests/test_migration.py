"""Tests de migration.py — traslada ajustes legados (globales) al esquema actual (por CUPS), sin
pisar valores que el CUPS ya tuviera propios. `ConfigEntry`/`HomeAssistant` solo se usan como type
hints aquí (nunca se instancian de verdad dentro de la lógica), así que unos objetos ligeros de
prueba bastan — ver conftest.py para el porqué del stub de `homeassistant`."""

from __future__ import annotations

from custom_components.edistribucion.const import CONF_SUPPLY_POINTS, DEFAULT_IEE_PERCENT, DEFAULT_IVA_PERCENT
from custom_components.edistribucion.migration import async_apply_default_tax_percentages, async_migrate_legacy_options


class FakeEntry:
    def __init__(self, options: dict) -> None:
        self.options = options


class FakeConfigEntries:
    def __init__(self) -> None:
        self.updated_with: dict | None = None

    def async_update_entry(self, entry, options) -> None:
        self.updated_with = options
        entry.options = options


class FakeHass:
    def __init__(self) -> None:
        self.config_entries = FakeConfigEntries()


def test_new_install_without_legacy_keys_is_noop():
    entry = FakeEntry({"scan_interval": 15, CONF_SUPPLY_POINTS: {"c1": {"price_power_punta": 0.08}}})
    original = dict(entry.options)
    hass = FakeHass()

    async_migrate_legacy_options(hass, entry)

    assert entry.options == original
    assert hass.config_entries.updated_with is None  # no debe llamar a async_update_entry si no hace falta


def test_migrates_v19x_global_price_and_zone_to_per_cups():
    entry = FakeEntry(
        {
            "scan_interval": 15,
            "price_power_punta": 0.08,
            "price_power_valle": 0.03,
            "pvpc_zone": "CYM",
            CONF_SUPPLY_POINTS: {"c1": {"track": True, "tariff_type": "tramos", "price_punta": 0.25}},
        }
    )
    hass = FakeHass()

    async_migrate_legacy_options(hass, entry)

    assert "price_power_punta" not in entry.options
    assert "price_power_valle" not in entry.options
    assert "pvpc_zone" not in entry.options
    sp = entry.options[CONF_SUPPLY_POINTS]["c1"]
    assert sp["price_power_punta"] == 0.08
    assert sp["price_power_valle"] == 0.03
    assert sp["pvpc_zone"] == "CYM"
    assert sp["price_punta"] == 0.25  # lo que ya tenía el CUPS no se toca


def test_migrates_oldest_p1_p2_and_esios_geo_id_ceuta():
    entry = FakeEntry(
        {
            "scan_interval": 15,
            "contracted_power_p1_kw": 4.6,
            "contracted_power_p2_kw": 4.6,
            "price_power_p1": 0.09,
            "price_power_p2": 0.02,
            "esios_api_key": "AAAA-BBBB",
            "esios_geo_id": "8744",  # Ceuta
            CONF_SUPPLY_POINTS: {"c1": {"track": True, "tariff_type": "pvpc"}},
        }
    )
    hass = FakeHass()

    async_migrate_legacy_options(hass, entry)

    for legacy_key in ("contracted_power_p1_kw", "contracted_power_p2_kw", "esios_api_key", "esios_geo_id"):
        assert legacy_key not in entry.options
    sp = entry.options[CONF_SUPPLY_POINTS]["c1"]
    assert sp["price_power_punta"] == 0.09
    assert sp["price_power_valle"] == 0.02
    assert sp["pvpc_zone"] == "CYM"


def test_esios_geo_id_non_ceuta_maps_to_peninsula_zone():
    entry = FakeEntry(
        {
            "esios_geo_id": "8741",  # Península
            CONF_SUPPLY_POINTS: {"c1": {}},
        }
    )
    hass = FakeHass()

    async_migrate_legacy_options(hass, entry)

    assert entry.options[CONF_SUPPLY_POINTS]["c1"]["pvpc_zone"] == "PCB"


def test_does_not_overwrite_existing_per_cups_value():
    entry = FakeEntry(
        {
            "price_power_punta": 0.99,  # legado global
            CONF_SUPPLY_POINTS: {"c1": {"price_power_punta": 0.05}},  # el CUPS ya tenía el suyo
        }
    )
    hass = FakeHass()

    async_migrate_legacy_options(hass, entry)

    assert entry.options[CONF_SUPPLY_POINTS]["c1"]["price_power_punta"] == 0.05


def test_already_migrated_install_is_noop():
    entry = FakeEntry(
        {
            "scan_interval": 15,
            CONF_SUPPLY_POINTS: {"c1": {"tariff_type": "tramos", "price_power_punta": 0.08, "pvpc_zone": "PCB"}},
        }
    )
    original = dict(entry.options)
    hass = FakeHass()

    async_migrate_legacy_options(hass, entry)

    assert entry.options == original
    assert hass.config_entries.updated_with is None


def test_no_supply_points_at_all_does_not_crash():
    entry = FakeEntry({"price_power_punta": 0.08})
    hass = FakeHass()

    async_migrate_legacy_options(hass, entry)

    assert "price_power_punta" not in entry.options
    assert hass.config_entries.updated_with is not None


def test_invalid_legacy_zone_is_dropped_not_propagated():
    """Si por lo que sea la zona legada no es una zona PVPC válida, no se propaga basura."""
    entry = FakeEntry(
        {
            "pvpc_zone": "ZONA-INVENTADA",
            CONF_SUPPLY_POINTS: {"c1": {}},
        }
    )
    hass = FakeHass()

    async_migrate_legacy_options(hass, entry)

    assert "pvpc_zone" not in entry.options[CONF_SUPPLY_POINTS]["c1"]


def test_fills_default_iee_and_iva_when_missing():
    """CUPS que nunca abrió Opciones desde que se añadió IEE/IVA (v1.27.0/v1.28.0) — se rellenan
    con los valores sugeridos para que los sensores de coste reflejen impuestos desde ya."""
    entry = FakeEntry({CONF_SUPPLY_POINTS: {"c1": {"tariff_type": "fija", "fixed_price": 0.2}}})
    hass = FakeHass()

    async_apply_default_tax_percentages(hass, entry)

    sp = entry.options[CONF_SUPPLY_POINTS]["c1"]
    assert sp["iee_percent"] == DEFAULT_IEE_PERCENT
    assert sp["iva_percent"] == DEFAULT_IVA_PERCENT
    assert sp["fixed_price"] == 0.2  # lo que ya tenía el CUPS no se toca


def test_does_not_overwrite_explicit_zero_tax_values():
    """Un 0 puesto A PROPÓSITO (p.ej. para simular sin impuestos) no se pisa — setdefault, no
    asignación directa."""
    entry = FakeEntry({CONF_SUPPLY_POINTS: {"c1": {"iee_percent": 0, "iva_percent": 0}}})
    hass = FakeHass()

    async_apply_default_tax_percentages(hass, entry)

    sp = entry.options[CONF_SUPPLY_POINTS]["c1"]
    assert sp["iee_percent"] == 0
    assert sp["iva_percent"] == 0
    assert hass.config_entries.updated_with is None  # ya migrado, no hace falta tocar nada


def test_fills_only_missing_key_when_one_already_set():
    entry = FakeEntry({CONF_SUPPLY_POINTS: {"c1": {"iva_percent": 10}}})  # solo IVA guardado
    hass = FakeHass()

    async_apply_default_tax_percentages(hass, entry)

    sp = entry.options[CONF_SUPPLY_POINTS]["c1"]
    assert sp["iee_percent"] == DEFAULT_IEE_PERCENT
    assert sp["iva_percent"] == 10  # no se pisa el que ya tenía


def test_multiple_supply_points_all_filled():
    entry = FakeEntry(
        {
            CONF_SUPPLY_POINTS: {
                "c1": {"tariff_type": "fija"},
                "c2": {"tariff_type": "pvpc", "iee_percent": 5.0, "iva_percent": 4},
            }
        }
    )
    hass = FakeHass()

    async_apply_default_tax_percentages(hass, entry)

    assert entry.options[CONF_SUPPLY_POINTS]["c1"]["iee_percent"] == DEFAULT_IEE_PERCENT
    assert entry.options[CONF_SUPPLY_POINTS]["c1"]["iva_percent"] == DEFAULT_IVA_PERCENT
    assert entry.options[CONF_SUPPLY_POINTS]["c2"]["iee_percent"] == 5.0
    assert entry.options[CONF_SUPPLY_POINTS]["c2"]["iva_percent"] == 4


def test_no_supply_points_is_noop():
    entry = FakeEntry({})
    hass = FakeHass()

    async_apply_default_tax_percentages(hass, entry)

    assert hass.config_entries.updated_with is None


def test_already_filled_install_is_noop():
    entry = FakeEntry({CONF_SUPPLY_POINTS: {"c1": {"iee_percent": 5.11269632, "iva_percent": 21}}})
    original = dict(entry.options)
    hass = FakeHass()

    async_apply_default_tax_percentages(hass, entry)

    assert entry.options == original
    assert hass.config_entries.updated_with is None
