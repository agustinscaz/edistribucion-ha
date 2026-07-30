"""Constantes de la integración e-distribución."""

DOMAIN = "edistribucion"

CONF_HOST = "host"
CONF_PORT = "port"

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8099
DEFAULT_SCAN_INTERVAL_MINUTES = 15

# Opciones: qué suministros seguir, con qué alias, y tarifa/precios de CADA UNO — dict
# {contId: {"track": bool, "alias": str, "tariff_type": ..., "contracted_power_punta_kw": ...,
#   "contracted_power_valle_kw": ..., "price_power_punta": ..., "price_power_valle": ...,
#   "fixed_price": ..., "price_punta": ..., "price_llano": ..., "price_valle": ...,
#   "surplus_compensation": bool, "surplus_price": ..., "pvpc_zone": ...}} — TODO esto es por CUPS,
# incluida la potencia contratada y la zona PVPC, porque cada contrato puede ser distinto.
CONF_SUPPLY_POINTS = "supply_points"

# Tipos de tarifa de energía, configurables por CUPS (no a nivel de toda la integración, distintos
# suministros pueden tener contratos distintos):
# - fija: un único precio €/kWh para todo.
# - tramos: precio €/kWh por franja horaria (punta/llano/valle), calculado hora a hora con el
#   consumo real (hourlyByDate) — más preciso que una media diaria.
# - pvpc: precio real hora a hora sacado directamente del archivo público de PVPC de ESIOS/REE, para
#   la zona configurada en ESTE CUPS (ver esios.py) — sin clave ni registro.
TARIFF_FIJA = "fija"
TARIFF_TRAMOS = "tramos"
TARIFF_PVPC = "pvpc"
TARIFF_TYPES = (TARIFF_FIJA, TARIFF_TRAMOS, TARIFF_PVPC)

# ESIOS/REE (PVPC): zona de precio (ver esios.py — PCB para Península/Baleares/Canarias, CYM para
# Ceuta/Melilla), sin clave/API key: se usa el archivo público de PVPC. Por CUPS, no a nivel de la
# integración (podrías en teoría tener suministros en distintas zonas).
CONF_PVPC_ZONE = "pvpc_zone"

# Término de potencia: en la 2.0TD son solo DOS periodos (punta/valle, con horario distinto al de
# energía), ambos se facturan siempre (no es "uno u otro" según la hora) — por eso no hace falta
# clasificar horas, solo potencia contratada × precio por día, para cada periodo. Por CUPS, no a
# nivel de la integración (distintos contratos pueden tener potencias contratadas distintas).
CONF_CONTRACTED_POWER_PUNTA = "contracted_power_punta_kw"
CONF_CONTRACTED_POWER_VALLE = "contracted_power_valle_kw"
CONF_PRICE_POWER_PUNTA = "price_power_punta"  # €/kW/día
CONF_PRICE_POWER_VALLE = "price_power_valle"  # €/kW/día
POWER_TERM_KEYS = (CONF_CONTRACTED_POWER_PUNTA, CONF_CONTRACTED_POWER_VALLE, CONF_PRICE_POWER_PUNTA, CONF_PRICE_POWER_VALLE)

# Umbral de fallos consecutivos del coordinator antes de levantar un Repair issue (ver repairs.py)
CONSECUTIVE_FAILURES_FOR_REPAIR = 3

# Rango por defecto para el sensor de "hoy": el add-on ya agrega por día en dailyTotals.
