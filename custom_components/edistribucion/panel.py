"""Panel nuevo en el menú lateral de Home Assistant: gráficas nativas de HA (history-graph,
statistics-graph) por CUPS, no cajas de texto hechas a mano.

No es un iframe: un iframe cargado por el navegador no puede llevar el token de autenticación de HA
(vive en JS/localStorage, no en cookies — confirmado contra el código real de `ha-panel-iframe`,
que usa la URL tal cual sin añadir nada), así que una vista con `requires_auth=True` ahí dentro
siempre da 401. Se usa `panel_custom`: un web component (JS plano, sin build) al que el propio
frontend le inyecta `hass` — con eso, `hass.callApi()` autentica de verdad, y `window.
loadCardHelpers()` (API pública del frontend, ver `custom-card-helpers.ts`) permite instanciar
tarjetas Lovelace NATIVAS (con sus gráficas, temas e interactividad reales) en vez de reinventarlas.

La vista Python solo resuelve qué `entity_id` corresponde a cada sensor de cada CUPS (vía el
registro de entidades) — todo el renderizado (incluidas las gráficas) lo hace el propio frontend de
HA con sus tarjetas de siempre.
"""

from __future__ import annotations

from aiohttp import web
from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN

PANEL_URL_PATH = "edistribucion-dashboard"
DATA_VIEW_URL = "/api/edistribucion/dashboard-data"
MODULE_VIEW_URL = "/api/edistribucion/panel.js"
WEBCOMPONENT_NAME = "edistribucion-panel"
_PANEL_REGISTERED_KEY = f"{DOMAIN}_panel_registered"

# unique_id de cada sensor es f"{cont_id}_{sufijo}" (ver sensor.py) — se resuelven a entity_id vía
# el registro de entidades, para no tener que adivinar el slug (que depende del alias/idioma).
_ENTITY_SUFFIXES = {
    "imported_today": "imported_energy_today",
    "exported_today": "exported_energy_today",
    "contracted_power": "contracted_power",
    "power_cost_today": "power_cost_today",
    "cost_today": "estimated_cost_today",
    "cost_month": "estimated_cost_month",
    "surplus_today": "surplus_compensation_today",
    "month_vs_last_year": "month_vs_last_year",
}

_PANEL_JS = """
class EdistribucionPanel extends HTMLElement {
  set hass(hass) {
    this._hass = hass;
    if (this._cards) {
      for (const card of this._cards) card.hass = hass;
    } else {
      this._maybeBuild();
    }
  }
  connectedCallback() {
    this._connected = true;
    this._maybeBuild();
  }
  _maybeBuild() {
    if (this._connected && this._hass && !this._building) {
      this._building = true;
      this._build();
    }
  }
  async _build() {
    let data;
    try {
      data = await this._hass.callApi("GET", "edistribucion/dashboard-data");
    } catch (err) {
      this.innerHTML = '<p style="padding:16px">Error cargando el panel de e-distribución: ' + err + "</p>";
      return;
    }
    const helpers = await window.loadCardHelpers();
    this.innerHTML = "";
    this.style.display = "block";
    this.style.padding = "16px";

    const grid = document.createElement("div");
    grid.style.display = "grid";
    grid.style.gap = "16px";
    grid.style.gridTemplateColumns = "repeat(auto-fit, minmax(360px, 1fr))";
    this.appendChild(grid);

    this._cards = [];
    const addCard = (container, config) => {
      const card = helpers.createCardElement(config);
      card.hass = this._hass;
      container.appendChild(card);
      this._cards.push(card);
    };

    if (!data.supply_points.length) {
      grid.innerHTML = "<p>Todavía no hay datos — espera a la próxima actualización.</p>";
      return;
    }

    for (const sp of data.supply_points) {
      const col = document.createElement("div");
      const e = sp.entities;

      addCard(col, {
        type: "entities",
        title: sp.alias + " (" + sp.cups + ")",
        entities: [e.contracted_power, e.power_cost_today, e.cost_today, e.cost_month, e.surplus_today, e.month_vs_last_year].filter(Boolean),
      });
      if (e.imported_today || e.exported_today) {
        addCard(col, {
          type: "history-graph",
          title: "Energía (últimas 48h)",
          hours_to_show: 48,
          entities: [e.imported_today, e.exported_today].filter(Boolean),
        });
      }
      if (e.cost_month) {
        addCard(col, {
          type: "statistics-graph",
          title: "Coste estimado por día (mes)",
          entities: [e.cost_month],
          stat_types: ["sum"],
          period: "day",
          days_to_show: 30,
        });
      }
      grid.appendChild(col);
    }
  }
}
customElements.define("edistribucion-panel", EdistribucionPanel);
"""


def _resolve_entity_id(hass: HomeAssistant, cont_id: str, suffix: str) -> str | None:
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{cont_id}_{suffix}")


def _dashboard_data(hass: HomeAssistant) -> dict:
    supply_points = []
    for coordinator in hass.data.get(DOMAIN, {}).values():
        for cont_id, bundle in coordinator.data.items():
            sp = bundle.get("supply_point") or {}
            supply_points.append(
                {
                    "cont_id": cont_id,
                    "alias": sp.get("alias") or sp.get("cups", cont_id),
                    "cups": sp.get("cups", "?"),
                    "entities": {key: _resolve_entity_id(hass, cont_id, suffix) for key, suffix in _ENTITY_SUFFIXES.items()},
                }
            )
    return {"supply_points": supply_points}


class EdistribucionDashboardDataView(HomeAssistantView):
    """Devuelve qué entity_id corresponde a cada sensor de cada CUPS — la llama `hass.callApi()`
    desde el propio componente del panel, así que SÍ lleva el token de autenticación."""

    url = DATA_VIEW_URL
    name = "api:edistribucion:dashboard-data"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        return web.json_response(_dashboard_data(hass))


class EdistribucionPanelModuleView(HomeAssistantView):
    """Sirve el módulo JS del panel — es solo código (nada sensible), público para que el
    frontend pueda cargarlo con una simple etiqueta <script type="module">."""

    url = MODULE_VIEW_URL
    name = "api:edistribucion:panel-js"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        return web.Response(text=_PANEL_JS, content_type="application/javascript")


async def async_register_panel(hass: HomeAssistant) -> None:
    """Registra el panel UNA sola vez por instancia de HA (aunque haya varias entradas de
    configuración de e-distribución, todas comparten el mismo panel)."""
    if hass.data.get(_PANEL_REGISTERED_KEY):
        return
    hass.http.register_view(EdistribucionDashboardDataView)
    hass.http.register_view(EdistribucionPanelModuleView)
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=WEBCOMPONENT_NAME,
        sidebar_title="e-distribución",
        sidebar_icon="mdi:transmission-tower",
        module_url=MODULE_VIEW_URL,
        embed_iframe=False,
        trust_external=False,
        require_admin=False,
    )
    hass.data[_PANEL_REGISTERED_KEY] = True


def async_unregister_panel(hass: HomeAssistant) -> None:
    """Quita el panel cuando se descarga la ÚLTIMA entrada de configuración de e-distribución."""
    if not hass.data.get(_PANEL_REGISTERED_KEY):
        return
    frontend.async_remove_panel(hass, PANEL_URL_PATH)
    hass.data.pop(_PANEL_REGISTERED_KEY, None)
