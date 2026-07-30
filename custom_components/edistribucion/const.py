"""Constantes de la integración e-distribución."""

DOMAIN = "edistribucion"

CONF_HOST = "host"
CONF_PORT = "port"

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8099
DEFAULT_SCAN_INTERVAL_MINUTES = 15

# Opciones: qué suministros seguir y con qué alias — dict {contId: {"track": bool, "alias": str}}
CONF_SUPPLY_POINTS = "supply_points"

# Precios €/kWh por periodo horario, para el coste estimado (todos opcionales — si están todos a
# 0, no se crean esos sensores). Calculado hora a hora con el consumo real (hourlyByDate), no una
# media — mucho más preciso que un único precio para todo el día.
CONF_PRICE_PUNTA = "price_punta"
CONF_PRICE_LLANO = "price_llano"
CONF_PRICE_VALLE = "price_valle"
PRICE_PERIOD_KEYS = (CONF_PRICE_PUNTA, CONF_PRICE_LLANO, CONF_PRICE_VALLE)

# Umbral de fallos consecutivos del coordinator antes de levantar un Repair issue (ver repairs.py)
CONSECUTIVE_FAILURES_FOR_REPAIR = 3

# Rango por defecto para el sensor de "hoy": el add-on ya agrega por día en dailyTotals.
