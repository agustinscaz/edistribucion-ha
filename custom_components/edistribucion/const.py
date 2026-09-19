"""Constantes de la integración e-distribución."""

DOMAIN = "edistribucion"

CONF_HOST = "host"
CONF_PORT = "port"

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8099
DEFAULT_SCAN_INTERVAL_MINUTES = 15

# Opciones: qué suministros seguir, con qué alias, y tarifa/precios de CADA UNO — dict
# {contId: {"track": bool, "alias": str, "tariff_type": ..., "price_power_punta": ...,
#   "price_power_valle": ..., "meter_rental_eur_day": ..., "fixed_price": ..., "price_punta": ...,
#   "price_llano": ..., "price_valle": ..., "surplus_compensation": bool, "surplus_price": ...,
#   "pvpc_zone": ...}} — esto es por CUPS, porque cada contrato puede ser distinto. La potencia
# contratada NO se pide aquí — se lee en vivo de e-distribución (ver
# coordinator.py/CONF_CONTRACTED_POWER_PUNTA/VALLE).
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
# clasificar horas, solo potencia contratada × precio por día, para cada periodo.
#
# La potencia contratada (kW) se lee EN VIVO de e-distribución (endpoint /contracted-power/:contId
# del add-on, ver coordinator.py) — no es una opción que rellene el usuario, así que estas dos
# claves solo existen como claves de DATOS dentro del dict del suministro (bundle["supply_point"]),
# nunca en CONF_SUPPLY_POINTS. El precio (€/kW/día) sí es comercial, no lo sabe la distribuidora, y
# por tanto SÍ es una opción manual por CUPS.
CONF_CONTRACTED_POWER_PUNTA = "contracted_power_punta_kw"
CONF_CONTRACTED_POWER_VALLE = "contracted_power_valle_kw"
CONF_PRICE_POWER_PUNTA = "price_power_punta"  # €/kW/día
CONF_PRICE_POWER_VALLE = "price_power_valle"  # €/kW/día

# Alquiler del equipo de medida (contador): lo factura la COMERCIALIZADORA, no e-distribución — no
# está disponible en la API de la distribuidora que consume api.py, así que no se puede leer en
# vivo de ningún lado (a diferencia de la potencia contratada). Es un importe FIJO por contrato
# (no cambia salvo que cambies de equipo), así que basta con teclearlo una vez mirando la factura
# de la comercializadora. Por defecto 0 para no romper a nadie que no lo configure (issue #24). Se
# suma al término de potencia ANTES de aplicar IEE/IVA (costs.power_cost) — en una factura real
# española, "alquiler de equipos de medida y control" es un concepto de base imponible más, junto a
# potencia y energía, no un importe ya con impuestos aplicado.
CONF_METER_RENTAL_EUR_DAY = "meter_rental_eur_day"  # €/día

# Región (CCAA) para festivos, por CUPS — con tarifa "tramos", un festivo entre semana cuenta como
# valle todo el día (igual que fin de semana), en vez de como si fuera un día laborable normal. Los
# códigos son los que usa la librería `holidays` (paquete "holidays" del manifest). "none" = no
# aplicar festivos (comportamiento anterior, por defecto).
CONF_HOLIDAY_REGION = "holiday_region"
DEFAULT_HOLIDAY_REGION = "none"
HOLIDAY_REGIONS = {
    "none": "No usar (todos los días laborables cuentan igual)",
    "AN": "Andalucía",
    "AR": "Aragón",
    "AS": "Asturias",
    "CB": "Cantabria",
    "CE": "Ceuta",
    "CL": "Castilla y León",
    "CM": "Castilla-La Mancha",
    "CN": "Canarias",
    "CT": "Cataluña",
    "EX": "Extremadura",
    "GA": "Galicia",
    "IB": "Illes Balears",
    "MC": "Murcia",
    "MD": "Madrid",
    "ML": "Melilla",
    "NC": "Navarra",
    "PV": "País Vasco",
    "RI": "La Rioja",
    "VC": "C. Valenciana",
}

# Dos impuestos (%) a aplicar sobre el coste de energía Y el término de potencia de ESTE CUPS (por
# CUPS, no global, porque algunos contratos tienen IVA reducido — p.ej. bono social térmico), EN
# ESTE ORDEN — igual que en una factura real, no son un recargo plano que se pueda fusionar en un
# único porcentaje sin perder precisión si el Estado cambia uno u otro por separado (ver issue #3):
#   1. IEE (Impuesto Especial sobre la Electricidad) — tipo estatal fijo, base imponible del IVA.
#   2. IVA — aplicado sobre el resultado de aplicar el IEE, no sobre el precio base.
# Ninguno se aplica a la compensación por excedentes (es una bonificación que se resta, no un
# consumo que se grave) ni al desglose de excedentes exportados por tramo — ver costs.py.
# Instalaciones YA EXISTENTES que no tengan estas claves guardadas las reciben con este valor
# sugerido en el primer arranque tras actualizar (ver migration.async_apply_default_tax_percentages
# — a diferencia de un rename, aquí no hay nada que "perder" al rellenar una clave que nunca
# existió, así que no hace falta esperar a que el usuario abra Opciones). Si alguien ya había puesto
# 0 a propósito para una de las dos, ese 0 se respeta (setdefault, no se sobrescribe).
# Issue #30: 5,11269632% es el tipo estatal GENERAL vigente, base imponible = energía + potencia
# (confirmado: no es solo sobre energía) — sigue siendo lo correcto para la inmensa mayoría de
# contratos. Pero existen exenciones de IEE NO automáticas (exigen inscripción específica en
# registros territoriales, art. 93/94.5/94.9 de la Ley de Impuestos Especiales) — la más relevante
# para esta integración es la de autoconsumo con excedentes acogido a compensación (art. 94.9): la
# energía compensada con el excedente horario queda exenta de IEE. Un usuario inscrito en esa
# exención verá 0€ de IEE en su factura real aunque tenga consumo de red — para ese caso hay que
# poner `iee_percent=0` a mano en Opciones, NO cambiar este default global (rompería el cálculo
# para la mayoría de usuarios, que sí pagan el tipo general).
CONF_IEE_PERCENT = "iee_percent"
DEFAULT_IEE_PERCENT = 5.11269632  # tipo estatal fijo vigente del IEE
CONF_IVA_PERCENT = "iva_percent"
DEFAULT_IVA_PERCENT = 21  # tipo general de IVA vigente para electricidad

# Umbral de fallos consecutivos del coordinator antes de levantar un Repair issue (ver repairs.py)
CONSECUTIVE_FAILURES_FOR_REPAIR = 3

# Rango por defecto para el sensor de "hoy": el add-on ya agrega por día en dailyTotals.
