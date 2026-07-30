"""Constantes de la integración e-distribución."""

DOMAIN = "edistribucion"

CONF_HOST = "host"
CONF_PORT = "port"

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8099
DEFAULT_SCAN_INTERVAL_MINUTES = 15

# Opciones: qué suministros seguir, con qué alias, y tarifa/precios de CADA UNO — dict
# {contId: {"track": bool, "alias": str, "tariff_type": ..., "fixed_price": ..., "price_punta": ...,
#   "price_llano": ..., "price_valle": ..., "surplus_compensation": bool,
#   "surplus_price": ...}} — el precio PVPC NO va aquí: es global (ver CONF_ESIOS_API_KEY/CONF_ESIOS_GEO_ID)
CONF_SUPPLY_POINTS = "supply_points"

# Tipos de tarifa de energía, configurables por CUPS (no a nivel de toda la integración, distintos
# suministros pueden tener contratos distintos):
# - fija: un único precio €/kWh para todo.
# - tramos: precio €/kWh por franja horaria (punta/llano/valle), calculado hora a hora con el
#   consumo real (hourlyByDate) — más preciso que una media diaria.
# - pvpc: precio real hora a hora sacado directamente de la API de ESIOS/REE (indicador 1001), con
#   la clave y región configuradas a nivel de la integración (ver más abajo) — el precio PVPC es el
#   mismo para todos los CUPS de una misma región, no depende del contrato.
TARIFF_FIJA = "fija"
TARIFF_TRAMOS = "tramos"
TARIFF_PVPC = "pvpc"
TARIFF_TYPES = (TARIFF_FIJA, TARIFF_TRAMOS, TARIFF_PVPC)

# ESIOS/REE (PVPC): clave gratuita solicitada por email a la propia REE, y región (geo_id) — es lo
# mismo para todos los suministros que usen tarifa "pvpc", así que va a nivel de la integración.
CONF_ESIOS_API_KEY = "esios_api_key"
CONF_ESIOS_GEO_ID = "esios_geo_id"
ESIOS_GEO_IDS = {
    "8741": "Península",
    "8742": "Canarias",
    "8743": "Baleares",
    "8744": "Ceuta",
    "8745": "Melilla",
}
DEFAULT_ESIOS_GEO_ID = "8741"

# Término de potencia: en la 2.0TD son solo DOS periodos (P1/P2, con horario distinto al de
# energía), ambos se facturan siempre (no es "uno u otro" según la hora) — por eso no hace falta
# clasificar horas, solo potencia contratada × precio por día, para cada periodo. Esto SÍ es a nivel
# de toda la integración (no por CUPS) — normalmente la misma potencia contratada aplica a la
# instalación completa.
CONF_CONTRACTED_POWER_P1 = "contracted_power_p1_kw"
CONF_CONTRACTED_POWER_P2 = "contracted_power_p2_kw"
CONF_PRICE_POWER_P1 = "price_power_p1"  # €/kW/día
CONF_PRICE_POWER_P2 = "price_power_p2"  # €/kW/día
POWER_TERM_KEYS = (CONF_CONTRACTED_POWER_P1, CONF_CONTRACTED_POWER_P2, CONF_PRICE_POWER_P1, CONF_PRICE_POWER_P2)

# Umbral de fallos consecutivos del coordinator antes de levantar un Repair issue (ver repairs.py)
CONSECUTIVE_FAILURES_FOR_REPAIR = 3

# Rango por defecto para el sensor de "hoy": el add-on ya agrega por día en dailyTotals.
