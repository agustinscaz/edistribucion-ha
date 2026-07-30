"""Constantes de la integración e-distribución."""

DOMAIN = "edistribucion"

CONF_HOST = "host"
CONF_PORT = "port"

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8099
DEFAULT_SCAN_INTERVAL_MINUTES = 15

# Opciones: qué suministros seguir y con qué alias — dict {contId: {"track": bool, "alias": str}}
CONF_SUPPLY_POINTS = "supply_points"

# Precio fijo €/kWh para el sensor de coste estimado (opcional — sin él, no se crean esos sensores)
CONF_PRICE_PER_KWH = "price_per_kwh"

# Umbral de fallos consecutivos del coordinator antes de levantar un Repair issue (ver repairs.py)
CONSECUTIVE_FAILURES_FOR_REPAIR = 3

# Rango por defecto para el sensor de "hoy": el add-on ya agrega por día en dailyTotals.
