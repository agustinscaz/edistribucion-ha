"""Constantes de la integración e-distribución."""

DOMAIN = "edistribucion"

CONF_HOST = "host"
CONF_PORT = "port"

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8099
DEFAULT_SCAN_INTERVAL_MINUTES = 15

# Opciones: qué suministros seguir, con qué alias, y tarifa/precios de CADA UNO — dict
# {contId: {"track": bool, "alias": str, "tariff_type": ..., "fixed_price": ..., "price_punta": ...,
#   "price_llano": ..., "price_valle": ..., "pvpc_entity": ..., "surplus_compensation": bool,
#   "surplus_price": ...}}
CONF_SUPPLY_POINTS = "supply_points"

# Tipos de tarifa de energía, configurables por CUPS (no a nivel de toda la integración, distintos
# suministros pueden tener contratos distintos):
# - fija: un único precio €/kWh para todo.
# - tramos: precio €/kWh por franja horaria (punta/llano/valle), calculado hora a hora con el
#   consumo real (hourlyByDate) — más preciso que una media diaria.
# - pvpc: se referencia un sensor YA EXISTENTE en HA (p.ej. de la integración oficial ESIOS) y se
#   usa su valor ACTUAL como precio — OJO, es una limitación real: no hay histórico de precios PVPC
#   hora a hora aquí, así que el coste de "hoy"/"mes" con PVPC es una aproximación con el precio de
#   AHORA, no el que tocaba en cada hora pasada.
TARIFF_FIJA = "fija"
TARIFF_TRAMOS = "tramos"
TARIFF_PVPC = "pvpc"
TARIFF_TYPES = (TARIFF_FIJA, TARIFF_TRAMOS, TARIFF_PVPC)

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
