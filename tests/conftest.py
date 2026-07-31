"""Configuración común de pytest.

Los módulos SIN lógica de Home Assistant (costs.py, esios.py, migration.py...) usan imports
relativos internos (`.const`, `.esios`), así que hace falta importarlos como parte de un paquete de
verdad — pero el paquete real `custom_components/edistribucion/__init__.py` importa
`homeassistant`. En este sandbox de desarrollo (sin pip) ese paquete no está instalado, así que se
registran paquetes "stub" vacíos en `sys.modules` para `custom_components` y
`custom_components.edistribucion` ANTES de que nada los importe, así el `__init__.py` real nunca se
ejecuta, pero los submódulos individuales (costs.py, esios.py, const.py, migration.py) sí se cargan
de verdad, tal cual son.

IMPORTANTE: en CI (`pip install -r requirements_test.txt`) el paquete `homeassistant` real SÍ está
instalado — ahí NO hay que tocar `sys.modules` en absoluto, porque los tests que dependen de Home
Assistant real (coordinator.py, sensor.py, config_flow.py, __init__.py...) necesitan que
`custom_components.edistribucion` sea el paquete real (con su `__init__.py` real ejecutado), no un
stub vacío — si se stubea siempre, `pytest_homeassistant_custom_component` encuentra el stub en
`sys.modules` en vez de cargar el de verdad, y HA falla con "No setup or config entry setup function
defined" para TODOS los tests que hacen `hass.config_entries.async_setup(...)`.
"""

import sys
import types
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_CUSTOM_COMPONENTS_DIR = _ROOT / "custom_components"
_EDISTRIBUCION_DIR = _CUSTOM_COMPONENTS_DIR / "edistribucion"

try:
    import homeassistant  # noqa: F401

    _HOMEASSISTANT_AVAILABLE = True
except ModuleNotFoundError:
    _HOMEASSISTANT_AVAILABLE = False

if not _HOMEASSISTANT_AVAILABLE:
    if "custom_components" not in sys.modules:
        _stub = types.ModuleType("custom_components")
        _stub.__path__ = [str(_CUSTOM_COMPONENTS_DIR)]
        sys.modules["custom_components"] = _stub

    if "custom_components.edistribucion" not in sys.modules:
        _edist_stub = types.ModuleType("custom_components.edistribucion")
        _edist_stub.__path__ = [str(_EDISTRIBUCION_DIR)]
        sys.modules["custom_components.edistribucion"] = _edist_stub
        # El import normal de Python deja el submódulo como atributo del padre (`parent.child`) —
        # como aquí lo registramos a mano en sys.modules sin pasar por el import real, hay que
        # replicarlo, o `monkeypatch.setattr("custom_components.edistribucion.X", ...)` no
        # encuentra el camino.
        sys.modules["custom_components"].edistribucion = _edist_stub

    # Sin el paquete real de Home Assistant instalado: algunos módulos puros (migration.py) solo
    # necesitan `ConfigEntry`/`HomeAssistant` como TYPE HINTS (nunca se instancian de verdad dentro
    # de su lógica), así que un stub mínimo basta para poder importarlos y probar su lógica real sin
    # necesitar Home Assistant completo.
    _ha = types.ModuleType("homeassistant")
    _ha_config_entries = types.ModuleType("homeassistant.config_entries")
    _ha_core = types.ModuleType("homeassistant.core")

    class ConfigEntry:  # noqa: D101
        pass

    class HomeAssistant:  # noqa: D101
        pass

    _ha_config_entries.ConfigEntry = ConfigEntry
    _ha_core.HomeAssistant = HomeAssistant
    _ha.config_entries = _ha_config_entries
    _ha.core = _ha_core

    sys.modules["homeassistant"] = _ha
    sys.modules["homeassistant.config_entries"] = _ha_config_entries
    sys.modules["homeassistant.core"] = _ha_core

    # statistics.py también usa `homeassistant.const.UnitOfEnergy` y `homeassistant.util.dt`
    # (solo para el formato/huso horario, no lógica de HA de verdad) — un stub mínimo basta para
    # poder probar en local su lógica pura (_hourly_points, _daily_points, _parse_day/_parse_hour).
    _ha_const = types.ModuleType("homeassistant.const")

    class UnitOfEnergy:  # noqa: D101
        KILO_WATT_HOUR = "kWh"

    _ha_const.UnitOfEnergy = UnitOfEnergy

    _ha_util = types.ModuleType("homeassistant.util")
    _ha_util_dt = types.ModuleType("homeassistant.util.dt")
    # UTC como "huso local" en el stub (Home Assistant de verdad usaría el huso configurado) — para
    # comprobar la fórmula de conversión, no el comportamiento exacto de zonas horarias de HA.
    _ha_util_dt.as_utc = lambda value: value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    _ha_util_dt.as_local = lambda value: value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    _ha_util_dt.now = lambda: datetime.now(timezone.utc)
    _ha_util_dt.utcnow = lambda: datetime.now(timezone.utc)
    _ha_util.dt = _ha_util_dt
    _ha.const = _ha_const
    _ha.util = _ha_util

    sys.modules["homeassistant.const"] = _ha_const
    sys.modules["homeassistant.util"] = _ha_util
    sys.modules["homeassistant.util.dt"] = _ha_util_dt

if _HOMEASSISTANT_AVAILABLE:
    import pytest

    @pytest.fixture(autouse=True)
    def _no_real_pvpc_network_calls(monkeypatch):
        """El coordinador pide precios PVPC a ESIOS en cuanto haya algún CUPS activo (lo necesita
        el simulador de tarifas aunque la tarifa activa no sea pvpc — ver
        coordinator._pvpc_zones_needed). Los tests que no están probando ese fetch en concreto
        (alta/baja de la integración, flujo de opciones...) no registran un mock para la URL de
        ESIOS, así que sin esto la petición real revienta con un AssertionError de
        `aioclient_mock` ("No mock registered...") en vez de fallar limpiamente como un
        `EsiosError` — y tira abajo el setup entero. Los tests que sí prueban el fetch en sí
        (test_coordinator.py, test_esios.py) sobrescriben este parche localmente."""
        try:
            import custom_components.edistribucion.coordinator as _coordinator_module
        except ImportError:
            return

        async def _fake_fetch(session, zone, day):
            return {}

        monkeypatch.setattr(_coordinator_module, "async_get_pvpc_prices_for_day", _fake_fetch, raising=False)
