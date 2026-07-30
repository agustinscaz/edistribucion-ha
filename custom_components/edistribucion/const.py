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

# Término de potencia: en la 2.0TD son solo DOS periodos (P1/P2, con horario distinto al de
# energía), ambos se facturan siempre (no es "uno u otro" según la hora) — por eso no hace falta
# clasificar horas, solo potencia contratada × precio por día, para cada periodo.
CONF_CONTRACTED_POWER_P1 = "contracted_power_p1_kw"
CONF_CONTRACTED_POWER_P2 = "contracted_power_p2_kw"
CONF_PRICE_POWER_P1 = "price_power_p1"  # €/kW/día
CONF_PRICE_POWER_P2 = "price_power_p2"  # €/kW/día
POWER_TERM_KEYS = (CONF_CONTRACTED_POWER_P1, CONF_CONTRACTED_POWER_P2, CONF_PRICE_POWER_P1, CONF_PRICE_POWER_P2)

# Umbral de fallos consecutivos del coordinator antes de levantar un Repair issue (ver repairs.py)
CONSECUTIVE_FAILURES_FOR_REPAIR = 3

# Rango por defecto para el sensor de "hoy": el add-on ya agrega por día en dailyTotals.
