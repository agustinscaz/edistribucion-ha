"""Configuración común de pytest.

Los módulos SIN lógica de Home Assistant (costs.py, esios.py, migration.py...) usan imports
relativos internos (`.const`, `.esios`), así que hace falta importarlos como parte de un paquete de
verdad — pero el paquete real `custom_components/edistribucion/__init__.py` importa
`homeassistant`, que no está instalado en este entorno de pruebas puro. Se registran paquetes
"stub" vacíos en `sys.modules` para `custom_components` y `custom_components.edistribucion` ANTES
de que nada los importe, así el `__init__.py` real nunca se ejecuta, pero los submódulos
individuales (costs.py, esios.py, const.py, migration.py) sí se cargan de verdad, tal cual son.

Los módulos que SÍ requieren objetos reales de Home Assistant (coordinator.py, sensor.py,
config_flow.py, __init__.py, button.py...) no se pueden probar así — esos usan
`pytest-homeassistant-custom-component` (ver tests que empiezan por test_ha_*), que necesita el
paquete `homeassistant` real instalado (`pip install -r requirements_test.txt`); en este sandbox de
desarrollo no hay `pip`, así que esos se verifican vía CI, no localmente.
"""

import sys
import types
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_CUSTOM_COMPONENTS_DIR = _ROOT / "custom_components"
_EDISTRIBUCION_DIR = _CUSTOM_COMPONENTS_DIR / "edistribucion"

if "custom_components" not in sys.modules:
    _stub = types.ModuleType("custom_components")
    _stub.__path__ = [str(_CUSTOM_COMPONENTS_DIR)]
    sys.modules["custom_components"] = _stub

if "custom_components.edistribucion" not in sys.modules:
    _edist_stub = types.ModuleType("custom_components.edistribucion")
    _edist_stub.__path__ = [str(_EDISTRIBUCION_DIR)]
    sys.modules["custom_components.edistribucion"] = _edist_stub
    # El import normal de Python deja el submódulo como atributo del padre (`parent.child`) — como
    # aquí lo registramos a mano en sys.modules sin pasar por el import real, hay que replicarlo,
    # o `monkeypatch.setattr("custom_components.edistribucion.X", ...)` no encuentra el camino.
    sys.modules["custom_components"].edistribucion = _edist_stub

try:
    import homeassistant  # noqa: F401
except ModuleNotFoundError:
    # Sin el paquete real de Home Assistant instalado (no hay pip en este sandbox de desarrollo):
    # algunos módulos puros (migration.py) solo necesitan `ConfigEntry`/`HomeAssistant` como TYPE
    # HINTS (nunca se instancian de verdad dentro de su lógica), así que un stub mínimo basta para
    # poder importarlos y probar su lógica real sin necesitar Home Assistant completo. Si el
    # paquete real SÍ está instalado (p.ej. en CI, vía pytest-homeassistant-custom-component), no
    # se toca nada — se usa el de verdad.
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
