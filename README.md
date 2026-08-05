<div align="center">
  <h1>e-distribución para Home Assistant</h1>
  <p><strong>Consumo importado/exportado y potencia contratada de tu contador de e-distribución, directamente en Home Assistant — corre por completo en tu propia instalación, sin depender de ningún servidor externo.</strong></p>

  <p>
    <img src="https://img.shields.io/badge/Home%20Assistant-Add--on-41BDF5?logo=home-assistant&logoColor=white" alt="Home Assistant Add-on">
    <img src="https://img.shields.io/badge/HACS-Custom-orange" alt="HACS Custom">
    <img src="https://img.shields.io/badge/arch-amd64%20%7C%20aarch64-blue" alt="amd64 | aarch64">
    <a href="https://github.com/agustinscaz/edistribucion-ha/actions/workflows/validate.yml"><img src="https://github.com/agustinscaz/edistribucion-ha/actions/workflows/validate.yml/badge.svg" alt="Validación"></a>
    <a href="./LICENSE"><img src="https://img.shields.io/github/license/agustinscaz/edistribucion-ha" alt="MIT license"></a>
  </p>

  <p>
    <a href="#instalación">Instalación</a> ·
    <a href="#configuración">Configuración</a> ·
    <a href="#entidades">Entidades</a> ·
    <a href="#cómo-funciona">Cómo funciona</a> ·
    <a href="https://github.com/agustinscaz/edistribucion-ha/issues">Soporte</a>
  </p>
</div>

---

Este repositorio trae **todo lo necesario** en un único sitio: el add-on que hace el login real contra la Zona Privada de e-distribución, y la integración de Home Assistant (instalable vía HACS) que expone esos datos como sensores. Cada persona que lo instala usa sus propias credenciales, guardadas únicamente en su propia instancia — no hay ningún servidor intermedio de por medio.

## Aviso importante

- Proyecto **no oficial**, sin relación con e-distribución. Los datos se obtienen mediante ingeniería inversa de su portal privado (una app Salesforce Experience Cloud) — puede dejar de funcionar si cambian su web, sin previo aviso.
- El login usa un navegador Chromium real (vía Playwright) porque el portal tiene una comprobación anti-bot que un cliente HTTP simple no puede pasar. Ese navegador **solo se enciende para loguear** — el resto de consultas van por HTTP ligero, sin navegador (ver [Cómo funciona](#cómo-funciona)).
- Si lo único que necesitas es el consumo (no potencia contratada ni otros detalles del contador), valora también [Datadis](https://datadis.es), la API **oficial** española para datos de consumo eléctrico.
- La API que expone el add-on no lleva autenticación propia — pensada para tu red doméstica de confianza, no para exponerla a Internet.

## ¿Por qué un add-on Y una integración HACS en el mismo repo?

HACS instala integraciones en Python que corren dentro del propio proceso de Home Assistant — no puede instalar un contenedor Docker con un navegador Chromium dentro (demasiado pesado, ninguna integración HACS lo hace). Por eso este proyecto tiene dos partes que viven en el mismo repositorio, cada una en su carpeta, sin interferir entre sí:

| Carpeta | Qué es | Cómo se instala |
|---|---|---|
| `edistribucion/` | El **add-on** (Node + Playwright): hace el login real y expone una API HTTP local | Tienda de complementos de Home Assistant (Supervisor) |
| `custom_components/edistribucion/` | La **integración** (Python puro, sin navegador): habla con el add-on y crea los sensores | HACS |

## Instalación

### 1. El add-on (obligatorio, primero)

1. En Home Assistant: **Ajustes → Complementos → Tienda de complementos**.
2. Menú (⋮) → **Repositorios** → añade `https://github.com/agustinscaz/edistribucion-ha`.
3. Busca **"e-distribución"** en la tienda e instálalo.
4. En su pestaña **Configuración**, rellena `dni` y `password` (los mismos de tu Zona Privada).
5. Inícialo.

### 2. La integración (HACS)

[![Open e-distribución in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=agustinscaz&repository=edistribucion-ha&category=integration)

1. **HACS → Integraciones** → menú (⋮) → **Repositorios personalizados**.
2. Añade `https://github.com/agustinscaz/edistribucion-ha` con categoría **Integración**.
3. Busca **"e-distribución"**, descárgala, reinicia Home Assistant.

### Instalación manual de la integración (alternativa a HACS)

1. Descarga la [última release](https://github.com/agustinscaz/edistribucion-ha/releases/latest).
2. Copia `custom_components/edistribucion` a `/config/custom_components/edistribucion`.
3. Reinicia Home Assistant.

## Configuración

1. **Ajustes → Dispositivos y servicios → Añadir integración** → busca **e-distribución**.
2. Si el add-on ya está arrancado en la misma red, debería aparecer solo (descubrimiento automático por mDNS) y solo hará falta confirmar. Si no, indica el host y puerto a mano (por defecto `localhost` y `8099`, correcto si el add-on corre en el mismo Home Assistant).

[![Add Integration to your Home Assistant instance.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=edistribucion)

Se crea automáticamente un dispositivo por cada punto de suministro (CUPS) de tu cuenta, más un dispositivo adicional **"e-distribución (add-on)"** (ver más abajo).

**IMPORTANTE — dónde está la configuración**: no está en la página de cada dispositivo/CUPS, sino en la propia integración. Ve a **Ajustes → Dispositivos y servicios → pestaña "Integraciones"**, busca la tarjeta **"e-distribución"** (la integración en sí, no un CUPS concreto) y pulsa **CONFIGURAR** (o los 3 puntos → Opciones). Ahí el asistente va **paso a paso**: primero un paso de ajustes generales, y luego un paso por cada uno de tus suministros (CUPS), uno detrás de otro — hay que ir pasando por todos hasta el final para que se guarde.

**Paso 1 — ajustes generales:**
- Solo el **intervalo de actualización** (15 min por defecto). Todo lo demás es por CUPS (paso siguiente), porque cada contrato puede tener potencia/tarifa distinta.

**Un paso por cada suministro:**
- Si **seguirlo** (puedes desmarcar los históricos) y un **alias**.
- El **tipo de tarifa de energía**:
  - `fija`: un único precio €/kWh.
  - `tramos`: precio €/kWh en 3 campos separados (punta/llano/valle), calculado hora a hora con tu consumo real. Opcionalmente, elige tu **región de festivos** (comunidad autónoma) para que un festivo entre semana cuente como valle todo el día, igual que el fin de semana — si no eliges ninguna, un festivo cuenta como día laborable normal.
  - `pvpc`: usa el precio real hora a hora del archivo público de PVPC de ESIOS/REE, según la **zona PVPC** que elijas para este CUPS (Península/Baleares/Canarias, o Ceuta y Melilla — **no hace falta clave ni registro**) — **los precios de mañana se publican sobre las 20:15h del día anterior**, así que las horas de después de esa hora ya usan el precio real del día siguiente, y las de un rato antes puede que aún no tengan precio publicado (se reflejan como "horas sin precio" hasta que ESIOS los publique).
- El **IEE** (Impuesto Especial sobre la Electricidad, %) y el **IVA a aplicar (%)** de este CUPS, EN ESE ORDEN — igual que en una factura real: el IEE es la base imponible del IVA, no un recargo aparte que se suma al final (`total = base × (1 + IEE%) × (1 + IVA%)`). Se aplican sobre el coste de energía (los tres tipos de tarifa) y sobre el término de potencia, para que los sensores de coste se acerquen más a lo que pagas de verdad. Por defecto 5,11269632% (el tipo estatal fijo del IEE) y 21% (el tipo general de IVA vigente) — ajústalos si tu contrato tiene tipos distintos. **Ninguno** se aplica a la compensación de excedentes (es una bonificación que se resta, no un consumo). Instalaciones que actualizan desde una versión anterior reciben estos valores por defecto automáticamente en el primer arranque tras actualizar (sin tener que abrir Opciones) — si ya habías puesto un 0% a propósito para alguno de los dos, ese 0% se respeta.
- El **precio del término de potencia** de este CUPS (€/kW/día para los periodos punta y valle) — se factura siempre, sea cual sea la tarifa de energía elegida. La **potencia contratada (kW)** no se pide: se lee automáticamente de tu contrato con e-distribución (sensor `Potencia contratada`), así que si la cambias, se actualiza sola sin tener que tocar nada aquí.
- La **compensación de excedentes**: si tu comercializadora te paga por lo exportado, actívala y pon el precio €/kWh para tener un sensor de esa compensación estimada.

## Entidades

Por cada punto de suministro (CUPS), agrupadas bajo su propio dispositivo:

| Entidad | Descripción |
|---|---|
| `sensor.<cups>_energia_importada_hoy` | Energía importada de la red hoy (kWh) — compatible con el Dashboard de Energía. Atributos `ultimo_cambio`/`minutos_sin_cambiar` (nota de frescura): la curva horaria de e-distribución se publica con retraso, así que el valor puede quedarse igual varias horas sin que sea un fallo — estos atributos permiten distinguir "sin consumo" de "dato atascado" sin mirar el histórico a mano. **Importante:** `ultimo_cambio` es cuándo la integración detectó por primera vez ese valor (con el margen de error del intervalo de actualización configurado), no el instante exacto en que cambió del lado de e-distribución — es una cota inferior de antigüedad, no una medición exacta |
| `sensor.<cups>_energia_exportada_hoy` | Energía exportada a la red hoy (kWh, si tienes autoconsumo con excedentes) — mismos atributos `ultimo_cambio`/`minutos_sin_cambiar` |
| `sensor.<cups>_energia_importada_semana` / `_mes` | Total importado en los últimos ~7/30 días |
| `sensor.<cups>_energia_exportada_semana` / `_mes` | Total exportado en los últimos ~7/30 días |
| `sensor.<cups>_comparativa_con_el_mismo_mes_del_año_anterior` | % de cambio del consumo importado de este mes frente al mismo mes de hace un año (sin valor si el contrato es más nuevo que un año) |
| `sensor.<cups>_coste_estimado_hoy` / `_mes` | **ESTIMACIÓN**, no el importe real de la factura: coste de energía (kWh × precio, según fija/tramos/pvpc) CON el IEE y el IVA de este CUPS aplicados EN ESE ORDEN — sigue sin alquiler de equipos de medida NI término de potencia (ver el siguiente sensor para incluirlo). Solo se crea si has configurado precio de energía para este suministro. Atributos `total_sin_impuestos`, `total_con_iee`, `iee_percent` e `iva_percent` con el desglose completo. El de "hoy" incluye además `ultimo_cambio`/`minutos_sin_cambiar` (ver nota de frescura más abajo) |
| `sensor.<cups>_coste_hoy_con_potencia` / `sensor.<cups>_coste_mes_con_potencia` | Como el sensor anterior, pero sumando también el término de potencia del mismo periodo (`termino_de_potencia_dia`/`_mes`, que también lleva el IEE y el IVA aplicados) — una única cifra de "coste de energía + potencia, con impuestos". Sigue sin alquiler de equipos de medida. Solo se crea si tienes precio de energía Y de potencia configurados; atributos `coste_energia`/`termino_potencia` con el desglose |
| `sensor.<cups>_kwh_en_punta` / `_llano` / `_valle` (`_hoy` / `_mes`) y `sensor.<cups>_coste_en_punta` / `_llano` / `_valle` (`_hoy` / `_mes`) | Solo con tarifa `tramos` y algún precio de tramo configurado — el mismo desglose kWh/€ por periodo que ya trae `coste_estimado` como atributo anidado, pero como 12 sensores propios (3 tramos × kWh/coste × hoy/mes) para poder graficarlos o usarlos en automatizaciones sin tener que leer un atributo |
| `sensor.<cups>_kwh_exportados_en_punta` / `_llano` / `_valle` (`_hoy` / `_mes`) y `sensor.<cups>_compensacion_en_punta` / `_llano` / `_valle` (`_hoy` / `_mes`) | Solo si has activado la compensación de excedentes — mismo bucketing horario que arriba, pero sobre kWh EXPORTADOS. El precio de compensación es plano (mismo €/kWh todo el día): el € de cada tramo es solo proporcional al kWh exportado en ese tramo, no hay un precio distinto por tramo |
| `sensor.<cups>_precio_medio_del_kwh_mes` | Coste total del mes ÷ kWh importados — para comparar contra otras ofertas del mercado |
| `sensor.<cups>_coste_acumulado_del_año` | Coste estimado en lo que va de año (meses completados, recalculados una vez al día, + el mes en curso en vivo); atributos con los kWh importados/exportados del año. Con tarifa `pvpc`, los meses anteriores al actual pueden salir con coste incompleto (no se re-piden precios PVPC históricos a ESIOS día a día) |
| `sensor.<cups>_coste_simulado_con_tarifa_x_mes` | Uno por cada tarifa DISTINTA a la que tengas activa (si hay datos para simularla): cuánto habría costado este mes con esa otra tarifa, sobre tu mismo consumo real — para comparar sin cambiar de tarifa de verdad |
| `sensor.<cups>_potencia_contratada` | Potencia contratada real (punta, kW), leída de tu contrato con e-distribución — potencia de valle, código de contrato, estado, comercializadora y tarifa como atributos |
| `sensor.<cups>_termino_de_potencia_dia` / `_mes` | Solo si has puesto precio de potencia en las opciones de este CUPS — kW contratados reales (punta/valle) × precio €/kW/día CON el IEE y el IVA de este CUPS aplicados EN ESE ORDEN, un coste fijo que no depende del consumo |
| `sensor.<cups>_compensacion_por_excedentes_hoy` / `_semana` / `_mes` | Solo si has activado la compensación de excedentes para este suministro — kWh exportados × precio configurado. El de "hoy" incluye atributos `ultimo_cambio`/`minutos_sin_cambiar` (ver nota de frescura más abajo) |
| `sensor.<cups>_precio_pvpc_actual` | Solo con tarifa `pvpc` — precio de la hora en curso (€/kWh) CON el IEE y el IVA de este CUPS aplicados EN ESE ORDEN; atributos `precio_sin_impuestos`, `iee_percent`, `iva_percent`, y `precios_hoy`/`precios_manana` con las 24h del día ya con impuestos incluidos (ver más abajo) |
| `sensor.<cups>_precio_actual_tramos` | Solo con tarifa `tramos` y algún precio de tramo configurado — equivalente al anterior pero para tramos: precio del periodo VIGENTE ahora mismo (€/kWh, con IEE+IVA aplicados). Atributos `periodo_actual` (`punta`/`llano`/`valle`, para un badge en el dashboard sin reimplementar el horario en una plantilla) y `precio_minimo_hoy`/`precio_medio_hoy`/`precio_maximo_hoy` (con impuestos) para una tarjeta resumen del día |
| `sensor.<cups>_proximo_cambio_de_tramo` | Igual condición que el anterior — cuándo cambia el periodo tarifario respecto al vigente ahora (timestamp), para disparar automatizaciones EXACTAMENTE en ese instante en vez de a una hora fija a ciegas (ver ejemplo más abajo). Atributo `siguiente_periodo` con el periodo al que se cambia |
| `sensor.<cups>_autosuficiencia_aproximada_hoy` / `_mes` | Solo si el suministro ha exportado algo — **% aproximado**, calculado ÚNICAMENTE con importado/exportado del contador de e-distribución (no con tu generación solar real, que la distribuidora no reporta): qué parte de la energía intercambiada con la red fue exportada en vez de importada. Si no importas nada de la red, sale 100% (autosuficiente del todo); si nunca exportas nada, sale 0%. Los valores intermedios son solo una estimación, no un % exacto sobre tu generación real. |
| `calendar.<cups>_calendario_de_consumo` | Un evento por día con datos (importado/exportado en el título) — navegable día a día y mes a mes con la tarjeta **Calendario** de Home Assistant, pidiendo al add-on el mes que estés mirando cada vez (no solo el actual) |

Los sensores de semana/mes llevan un atributo `daily_totals` con el desglose día a día (fecha + kWh), visible en Herramientas de desarrollo → Estados, o usable en una tarjeta de plantilla/tabla.

Además, agrupadas bajo el dispositivo **"e-distribución (add-on)"** (no ligado a ningún CUPS concreto):

| Entidad | Descripción |
|---|---|
| `binary_sensor.conectado` | ON si la última actualización pudo hablar con el add-on sin error |
| `sensor.ultima_actualizacion` | Marca de tiempo de la última actualización correcta |
| `sensor.proxima_actualizacion` | Estimación de cuándo tocará la siguiente (última correcta + intervalo configurado) |
| `button.actualizar_ahora` | Vuelve a pedir los datos ya, sin esperar al próximo ciclo |
| `button.forzar_reconexion` | Fuerza un login fresco en el add-on (por si la sesión "se queda rara") y actualiza |

**¿No ves el `binary_sensor`/los `button`/el `calendar`?** Comprueba en **Ajustes → Dispositivos y servicios → e-distribución** que aparezca el dispositivo correspondiente en la lista de dispositivos — a veces hace falta una **recarga completa** de la integración (menú ⋮ → Recargar) o un **reinicio de Home Assistant** tras actualizar por HACS para que se registren plataformas nuevas en una integración ya instalada previamente. Si tras recargar/reiniciar siguen sin aparecer, revisa **Ajustes → Sistema → Registros** filtrando por `edistribucion` para ver si hay algún error de configuración.

## Ejemplo de dashboard

La integración por sí sola no crea ninguna tarjeta — como cualquier integración de HA, las entidades quedan disponibles pero el dashboard hay que montarlo. Esto es un punto de partida razonable (ajusta `<cups>` a tu CUPS real, con guiones bajos en vez de letras especiales, tal como lo genere Home Assistant):

```yaml
type: vertical-stack
cards:
  - type: calendar
    entities:
      - calendar.<cups>_calendario_de_consumo
    initial_view: dayGridMonth
  - type: statistics-graph
    title: Importado vs. exportado (mes)
    entities:
      - sensor.<cups>_energia_importada_mes
      - sensor.<cups>_energia_exportada_mes
    stat_types:
      - sum
  - type: history-graph
    title: Últimas 48h (hoy)
    entities:
      - sensor.<cups>_energia_importada_hoy
      - sensor.<cups>_energia_exportada_hoy
  - type: gauge
    entity: sensor.<cups>_potencia_contratada
    name: Potencia contratada
  - type: entities
    title: Estado del add-on
    entities:
      - binary_sensor.conectado
      - sensor.ultima_actualizacion
      - button.actualizar_ahora
      - button.forzar_reconexion
```

Si además añades los sensores de energía al **Dashboard de Energía** (Ajustes → Dashboards → Energía → Añadir consumo de la red), tienes gráficas ya hechas sin montar nada a mano.

## Cómo funciona

1. Al arrancar el add-on (o cuando la sesión caduca), lanza Chromium en segundo plano, hace login real en `zonaprivada.edistribucion.com`, y captura la cookie de sesión (`sid`) y el token de la aplicación (`aura.token`) de una llamada real de la propia web — **después cierra el navegador por completo**.
2. Con ese token y esa cookie, las llamadas de datos siguientes (consumo, potencia máxima, suministros) se hacen por HTTP puro, sin navegador — típicamente en menos de 1-2 segundos en vez de los 10-15 que tarda un login completo.
3. Si una llamada falla porque la sesión ya caducó, el add-on lo detecta solo y repite el login automáticamente — no hace falta reiniciar nada a mano.
4. La sesión (cookies + token) se guarda en `/data/session.json` tras cada login, así que un reinicio normal del add-on (actualización, reinicio del host) **no obliga a volver a pasar por Chromium** — se reutiliza la sesión guardada y solo se relogin si de verdad ya caducó.
5. Un vigilante interno revisa cada minuto si ha quedado algún proceso de Chromium colgado (p.ej. por quedarse sin memoria a medio login) y lo cierra si lleva vivo más de 3 minutos — un login normal tarda ~15s, así que nunca debería afectar a uno legítimo.
6. El add-on se anuncia por mDNS (`_edistribucion._tcp.local`) para que la integración pueda encontrarlo sola en la configuración inicial.

Esto significa que el navegador (la parte pesada) solo se enciende ocasionalmente, no en cada consulta ni en cada reinicio — importante para que funcione bien en hardware modesto (Raspberry Pi 4/5 de 64 bits).

### Panel de estado y acceso vía Ingress

El add-on tiene un panel de estado en HTML (`GET /`, en vez de JSON) con: si hay sesión activa, cuántos logins se han hecho, el último error si lo hay (y si es de credenciales incorrectas, te lo dice explícitamente), y estadísticas del vigilante de Chromium. Puedes abrirlo directamente en `http://<host>:8099`, o desde el propio menú lateral de Home Assistant (el add-on usa **Ingress**, así que aparece ahí sin tener que exponer ni recordar ningún puerto).

## Funcionalidades avanzadas de la integración

- **Opciones configurables** (botón "Configurar" junto a la integración): intervalo de actualización, y qué suministros seguir — puedes desmarcar los históricos que ya no te interesan, y ponerle un **alias** a cada uno (p.ej. "Casa" en vez del CUPS) para que el nombre del dispositivo sea más legible.
- **Diagnósticos descargables**: Ajustes → Dispositivos y servicios → e-distribución → menú (⋮) → Descargar diagnósticos. Útil para adjuntar a un issue sin tener que copiar nada a mano (la dirección postal se redacta automáticamente).
- **Reparaciones (Repairs)**: si el add-on falla varias veces seguidas, aparece un aviso en Ajustes → Sistema → Reparaciones en vez de solo marcar los sensores como "no disponible" en silencio. Si el fallo es concretamente por **credenciales incorrectas**, el aviso lo dice explícitamente en vez de un genérico "no se puede conectar".
- **Reintentos automáticos**: un fallo de red puntual entre la integración y el add-on se reintenta un par de veces (con espera creciente) antes de darse por vencido.
- **Relleno de histórico en el Dashboard de Energía**: al configurar cada suministro (y una vez al día en adelante, no solo al arrancar) se rellena como estadística externa el consumo del mes en curso, con un punto por HORA si el add-on trae datos horarios (`hourlyByDate`) o por día si no — para no empezar con el gráfico vacío, y para que los meses nuevos se rellenen solos aunque Home Assistant lleve semanas sin reiniciarse.
- **Servicio `edistribucion.consultar_consumo`**: consulta bajo demanda un rango y fecha de referencia concretos (día/semana/mes de un mes anterior, por ejemplo), no solo el periodo actual que ya cachean los sensores — útil desde Herramientas de desarrollo → Acciones, o en tus propias automatizaciones/scripts.
- **Servicio `edistribucion.exportar_precios_pvpc`**: vuelca a CSV (en la respuesta del servicio) los precios PVPC ya cacheados del mes en curso, opcionalmente filtrados por zona — útil para analizarlos fuera de Home Assistant.
- **Servicio `edistribucion.horas_mas_baratas_pvpc`**: busca la ventana de N horas CONSECUTIVAS más barata a partir de ahora, con los precios PVPC ya cacheados — para automatizar cargas oportunistas (coche eléctrico, batería) sin mirar los precios a mano.
- **Servicio `edistribucion.resumen_mensual`**: genera un resumen en CSV del coste estimado del mes en curso de un suministro (desglose por periodo, término de potencia, excedentes) — una estimación propia con los precios que tengas configurados, no la factura real.
- **Servicio `edistribucion.rellenar_historico`**: rellena el histórico de estadísticas del Dashboard de Energía para meses ANTERIORES al actual (el relleno automático solo cubre el mes en curso) — útil recién instalada la integración, o si vienes de otra solución y quieres recuperar el histórico completo (hasta 36 meses hacia atrás).
- **Botón "Actualizar precios PVPC"** (solo si algún suministro usa tarifa `pvpc`): fuerza la petición a ESIOS ya, sin esperar al ciclo diario — útil si falló antes, o para comprobar si ya han publicado los precios de mañana.

### Ver los precios PVPC del día (sin crear 24 sensores)

El sensor `sensor.<cups>_precio_pvpc_actual` tiene como **valor** el precio de la hora en curso, y como **atributos** `precios_hoy` y `precios_manana` con las 24 horas del día (formato `{"00h": 0.123, "01h": 0.118, ...}`). Para verlos en una tabla en vez de en Herramientas de desarrollo → Estados, una tarjeta Markdown como esta funciona sin instalar nada:

```yaml
type: markdown
content: >
  {% set precios = state_attr('sensor.<cups>_precio_pvpc_actual', 'precios_hoy') %}
  | Hora | €/kWh |
  |---|---|
  {% for hora, precio in precios.items() -%}
  | {{ hora }} | {{ '%.4f'|format(precio) }} |
  {% endfor %}
```

### Automatizar por cambio de tramo, no a una hora fija (tarifa `tramos`)

`sensor.<cups>_proximo_cambio_de_tramo` es un sensor de tipo `timestamp` — Home Assistant permite
usar el **estado** de un sensor así directamente como hora de disparo (`at:`), así que la
automatización se dispara EXACTAMENTE cuando cambia el periodo, sin tener que hardcodear "a las
22h empieza el llano" (que deja de ser cierto si cambias de zona o de tarifa):

```yaml
triggers:
  - trigger: time
    at: sensor.<cups>_proximo_cambio_de_tramo
condition:
  - condition: template
    value_template: "{{ state_attr('sensor.<cups>_proximo_cambio_de_tramo', 'siguiente_periodo') == 'valle' }}"
actions:
  - action: switch.turn_on
    target:
      entity_id: switch.cargador_coche
```

Para un badge/chip con el periodo actual sin plantillas horarias propias, usa el atributo
`periodo_actual` de `sensor.<cups>_precio_actual_tramos` (`state_attr('sensor.<cups>_precio_actual_tramos', 'periodo_actual')`).

## Endpoints de la API del add-on

Por si quieres consultarla directamente (`http://<host>:8099`), sin pasar por la integración:

| Método y ruta | Descripción |
|---|---|
| `GET /health` | Comprobación de vida |
| `GET /info` | Nombre del titular y `visId` de la sesión activa |
| `GET /supply-points` | Lista de puntos de suministro (CUPS), activos e históricos |
| `GET /consumption/:contId` | Consumo importado/exportado del periodo por defecto (~2 días) |
| `GET /consumption/:contId?range=1\|2\|3` | Consumo por rango: 1=día, 2=semana, 3=mes |
| `GET /max-power-demand/:cupsId` | Histórico de potencia máxima demandada (~12 meses) |
| `GET /contracted-power/:contId` | Potencia contratada real (punta/valle, kW) + metadatos del contrato |
| `POST /relogin` | Fuerza un login fresco (ignora la sesión cacheada) |

`contId` y `cupsId` salen de `/supply-points`.

## Arquitecturas soportadas

`amd64` y `aarch64` (Raspberry Pi 4/5 de 64 bits, Intel/AMD). **No soporta `armv7`** (32 bits): el Chromium que usa Playwright no tiene build para esa arquitectura.

## Datos y privacidad

- La integración nunca ve tu contraseña — se introduce directamente en la configuración del add-on.
- La comunicación integración ↔ add-on es HTTP simple dentro de tu propia red; no sale de tu Home Assistant.
- No compartas CUPS, direcciones ni diagnósticos sin anonimizar en issues públicos.

## Limitaciones conocidas

- Facturas directas de e-distribución no están implementadas — solo aplican a una minoría de clientes con factura directa de la distribuidora (la mayoría paga a su comercializadora).
- Sin autenticación propia en la API del add-on — no expongas el puerto 8099 a Internet.
- El relleno de histórico en el Dashboard de Energía usa la Statistics API del `recorder`, una parte más avanzada y menos estable de Home Assistant — está pensado como "mejor esfuerzo": si falla, se registra un aviso en el log y el resto de la integración sigue funcionando con normalidad (solo te quedas sin el relleno retroactivo).
- Con tarifa `tramos`, se usa el horario de punta/llano/valle de la 2.0TD según la **zona PVPC** configurada para el CUPS (aunque la tarifa activa sea `tramos`, no `pvpc` — es la misma zona física): PCB (Península/Baleares/Canarias, por defecto) usa punta 10-14h y 18-22h entre semana, llano 8-10h/14-18h/22-24h entre semana, valle el resto y todo el fin de semana; CYM (Ceuta y Melilla) usa el mismo horario desplazado +1h (punta 11-15h/19-23h, llano 9-11h/15-19h/23-01h). Si no eliges una **región de festivos** para el CUPS, los festivos entre semana cuentan como día laborable normal (no como valle, que es como los factura realmente tu comercializadora) — para evitarlo, elige tu comunidad autónoma en las opciones de ese suministro.
- Con tarifa `pvpc`, se usa el precio real hora a hora del archivo público de PVPC de ESIOS/REE para la zona elegida — se piden los precios del mes en curso una vez al día (no en cada actualización, para no saturar la API pública), un día por petición. Las horas para las que ESIOS aún no haya publicado precio (p.ej. las últimas del día siguiente antes de las ~20:15h) quedan sin coste y se cuentan como "horas sin precio" hasta que se publiquen. Este caché se persiste a disco (mes en curso únicamente) para no tener que volver a pedirlo todo de cero tras un reinicio de Home Assistant.
- El término de potencia usa periodos punta/valle, que en la 2.0TD tienen un horario **distinto** al de punta/llano/valle de energía (la potencia punta cubre de día entre semana, la valle noches+fin de semana) — no se cruzan las franjas de un término con el otro.
- La potencia contratada real se lee de un endpoint de e-distribución no documentado oficialmente (ingeniería inversa, como el resto del add-on) — si en el futuro cambian esa página, el sensor `Potencia contratada` podría dejar de actualizarse (se registrará un aviso en el log; el resto de la integración sigue funcionando igual).
- El sensor de **autosuficiencia aproximada** NO es un % sobre tu generación solar real (e-distribución no la reporta, solo ve el intercambio con la red) — es únicamente exportado/(importado+exportado). Los casos límite son correctos (0% importado → 100%; nunca exportas → 0%), pero los valores intermedios son solo una estimación, no sustituyen a un medidor de generación real si necesitas precisión.

## Tests

La lógica de negocio (tarifas, festivos, PVPC, autosuficiencia, comparador de tarifas) tiene tests unitarios que no dependen de Home Assistant — se ejecutan con `pytest` normal:

```bash
pip install pytest pytest-asyncio pytest-cov pytest-aiohttp holidays
pytest tests/test_costs.py tests/test_esios.py tests/test_api.py tests/test_migration.py tests/test_statistics.py --cov=custom_components.edistribucion
```

El resto (config_flow, coordinator, sensor, `__init__`, botones...) necesita objetos reales de Home Assistant (`pytest-homeassistant-custom-component`) y se verifica en CI (ver `.github/workflows/validate.yml`), no en un entorno de desarrollo mínimo:

```bash
pip install -r requirements_test.txt
pytest tests/
```

## Soporte

Antes de abrir un [issue](https://github.com/agustinscaz/edistribucion-ha/issues):

1. Comprueba que el add-on está iniciado y respondiendo (`http://<host>:8099/health`).
2. Actualiza a la última versión y reinicia Home Assistant.
3. Revisa si el problema es del propio portal de e-distribución (caídas, mantenimiento).

## Proyecto relacionado

[`edistribucion-api`](https://github.com/agustinscaz/edistribucion-api) — servidor con Swagger UI usado para investigar y documentar la API no oficial de e-distribución (más llamadas, pensado para explorar a mano, no para Home Assistant).

## Licencia

[MIT](./LICENSE). e-distribución es marca de su respectivo propietario; este proyecto no tiene relación con ellos.
