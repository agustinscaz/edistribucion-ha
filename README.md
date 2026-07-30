<div align="center">
  <h1>e-distribución para Home Assistant</h1>
  <p><strong>Consumo importado/exportado y potencia máxima demandada de tu contador de e-distribución, directamente en Home Assistant — corre por completo en tu propia instalación, sin depender de ningún servidor externo.</strong></p>

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
- Si lo único que necesitas es el consumo (no potencia máxima ni otros detalles del contador), valora también [Datadis](https://datadis.es), la API **oficial** española para datos de consumo eléctrico.
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
2. Indica el host y puerto del add-on (por defecto `localhost` y `8099`, correcto si el add-on corre en el mismo Home Assistant).

[![Add Integration to your Home Assistant instance.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=edistribucion)

Se crea automáticamente un dispositivo por cada punto de suministro (CUPS) de tu cuenta, más un dispositivo adicional **"e-distribución (add-on)"** (ver más abajo).

Para cambiar el intervalo de actualización (15 min por defecto): en la propia integración, botón **Configurar** (engranaje) junto a "e-distribución" en Ajustes → Dispositivos y servicios.

## Entidades

Por cada punto de suministro (CUPS), agrupadas bajo su propio dispositivo:

| Entidad | Descripción |
|---|---|
| `sensor.<cups>_energia_importada_hoy` | Energía importada de la red hoy (kWh) — compatible con el Dashboard de Energía |
| `sensor.<cups>_energia_exportada_hoy` | Energía exportada a la red hoy (kWh, si tienes autoconsumo con excedentes) |
| `sensor.<cups>_energia_importada_semana` / `_mes` | Total importado en los últimos ~7/30 días |
| `sensor.<cups>_energia_exportada_semana` / `_mes` | Total exportado en los últimos ~7/30 días |
| `sensor.<cups>_potencia_maxima_demandada` | Último valor de potencia máxima demandada (kW) — solo suministros en BT con telegestión y <50kW contratados, con fecha/hora y detalle por periodo (P1-P6) como atributos |

Los sensores de semana/mes llevan un atributo `daily_totals` con el desglose día a día (fecha + kWh), visible en Herramientas de desarrollo → Estados, o usable en una tarjeta de plantilla/tabla.

Además, agrupadas bajo el dispositivo **"e-distribución (add-on)"** (no ligado a ningún CUPS concreto):

| Entidad | Descripción |
|---|---|
| `binary_sensor.conectado` | ON si la última actualización pudo hablar con el add-on sin error |
| `sensor.ultima_actualizacion` | Marca de tiempo de la última actualización correcta |
| `button.actualizar_ahora` | Vuelve a pedir los datos ya, sin esperar al próximo ciclo |
| `button.forzar_reconexion` | Fuerza un login fresco en el add-on (por si la sesión "se queda rara") y actualiza |

**¿No ves el `binary_sensor`/los `button`?** Comprueba en **Ajustes → Dispositivos y servicios → e-distribución** que aparezca el dispositivo **"e-distribución (add-on)"** en la lista de dispositivos (no solo los dispositivos por CUPS) — a veces hace falta una **recarga completa** de la integración (menú ⋮ → Recargar) o un **reinicio de Home Assistant** tras actualizar por HACS para que se registren plataformas nuevas (`binary_sensor`/`button`) en una integración ya instalada previamente. Si tras recargar/reiniciar siguen sin aparecer, revisa **Ajustes → Sistema → Registros** filtrando por `edistribucion` para ver si hay algún error de configuración.

## Cómo funciona

1. Al arrancar el add-on (o cuando la sesión caduca), lanza Chromium en segundo plano, hace login real en `zonaprivada.edistribucion.com`, y captura la cookie de sesión (`sid`) y el token de la aplicación (`aura.token`) de una llamada real de la propia web — **después cierra el navegador por completo**.
2. Con ese token y esa cookie, las llamadas de datos siguientes (consumo, potencia máxima, suministros) se hacen por HTTP puro, sin navegador — típicamente en menos de 1-2 segundos en vez de los 10-15 que tarda un login completo.
3. Si una llamada falla porque la sesión ya caducó, el add-on lo detecta solo y repite el login automáticamente — no hace falta reiniciar nada a mano.
4. La sesión (cookies + token) se guarda en `/data/session.json` tras cada login, así que un reinicio normal del add-on (actualización, reinicio del host) **no obliga a volver a pasar por Chromium** — se reutiliza la sesión guardada y solo se relogin si de verdad ya caducó.
5. Un vigilante interno revisa cada minuto si ha quedado algún proceso de Chromium colgado (p.ej. por quedarse sin memoria a medio login) y lo cierra si lleva vivo más de 3 minutos — un login normal tarda ~15s, así que nunca debería afectar a uno legítimo.

Esto significa que el navegador (la parte pesada) solo se enciende ocasionalmente, no en cada consulta ni en cada reinicio — importante para que funcione bien en hardware modesto (Raspberry Pi 4/5 de 64 bits).

## Funcionalidades avanzadas de la integración

- **Opciones configurables** (botón "Configurar" junto a la integración): intervalo de actualización, y qué suministros seguir — puedes desmarcar los históricos que ya no te interesan, y ponerle un **alias** a cada uno (p.ej. "Casa" en vez del CUPS) para que el nombre del dispositivo sea más legible.
- **Diagnósticos descargables**: Ajustes → Dispositivos y servicios → e-distribución → menú (⋮) → Descargar diagnósticos. Útil para adjuntar a un issue sin tener que copiar nada a mano (la dirección postal se redacta automáticamente).
- **Reparaciones (Repairs)**: si el add-on falla varias veces seguidas, aparece un aviso en Ajustes → Sistema → Reparaciones en vez de solo marcar los sensores como "no disponible" en silencio.
- **Relleno de histórico en el Dashboard de Energía**: al configurar cada suministro, se intenta rellenar como estadística externa el consumo del último mes ya disponible, para no empezar con el gráfico completamente vacío.
- **Servicio `edistribucion.consultar_consumo`**: consulta bajo demanda un rango y fecha de referencia concretos (día/semana/mes de un mes anterior, por ejemplo), no solo el periodo actual que ya cachean los sensores — útil desde Herramientas de desarrollo → Acciones, o en tus propias automatizaciones/scripts.

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

## Soporte

Antes de abrir un [issue](https://github.com/agustinscaz/edistribucion-ha/issues):

1. Comprueba que el add-on está iniciado y respondiendo (`http://<host>:8099/health`).
2. Actualiza a la última versión y reinicia Home Assistant.
3. Revisa si el problema es del propio portal de e-distribución (caídas, mantenimiento).

## Proyecto relacionado

[`edistribucion-api`](https://github.com/agustinscaz/edistribucion-api) — servidor con Swagger UI usado para investigar y documentar la API no oficial de e-distribución (más llamadas, pensado para explorar a mano, no para Home Assistant).

## Licencia

[MIT](./LICENSE). e-distribución es marca de su respectivo propietario; este proyecto no tiene relación con ellos.
