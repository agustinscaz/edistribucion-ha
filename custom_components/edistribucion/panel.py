"""Panel nuevo en el menú lateral de Home Assistant: un dashboard de solo lectura con coste,
potencia y comparativa por CUPS.

Deliberadamente NO es un iframe hacia una página servida por el add-on: el add-on no sabe nada de
tarifas/precios (eso vive solo en la integración, ver costs.py), así que reimplementar el cálculo
de coste en JS ahí sería duplicar lógica y una fuente más de bugs. En su lugar, esta vista la sirve
la propia integración (`hass.http.register_view`), reutilizando DIRECTAMENTE `costs.py` — el mismo
cálculo exacto que usan los sensores, en un único sitio.
"""

from __future__ import annotations

from aiohttp import web
from homeassistant.components import frontend
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .costs import estimate_energy_cost, power_cost, surplus_compensation_value
from .sensor import _latest_daily_total, _latest_day_hourly

PANEL_URL_PATH = "edistribucion-dashboard"
VIEW_URL = "/api/edistribucion/dashboard"
_PANEL_REGISTERED_KEY = f"{DOMAIN}_panel_registered"


def _fmt_eur(value) -> str:
    return f"{value:.2f} €" if isinstance(value, (int, float)) else "—"


def _fmt_kwh(value) -> str:
    return f"{value:.2f} kWh" if isinstance(value, (int, float)) else "—"


def _fmt_kw(value) -> str:
    return f"{value:.2f} kW" if isinstance(value, (int, float)) else "—"


def _card_html(bundle: dict, coordinator) -> str:
    sp = bundle.get("supply_point") or {}
    cups = sp.get("cups", "?")
    alias = sp.get("alias") or cups
    estado = "activo" if sp.get("active") else "histórico"

    today_total = _latest_daily_total(bundle.get("consumption"))
    imported_today = today_total["importedKwh"] if today_total else None
    month = bundle.get("month")
    imported_month = month.get("totalImportedKwh") if month else None
    exported_month = month.get("totalExportedKwh") if month else None

    today_hourly = _latest_day_hourly(bundle.get("consumption"))
    cost_today = estimate_energy_cost(sp, imported_today, today_hourly, coordinator.pvpc_prices)
    cost_month = estimate_energy_cost(sp, imported_month, month, coordinator.pvpc_prices)
    power_daily = power_cost(sp)
    surplus_month = surplus_compensation_value(sp, exported_month)
    contract = bundle.get("contract") or {}

    return f"""
    <div class="card">
      <h2>{alias} <span class="badge">{estado}</span></h2>
      <p class="cups">{cups} · tarifa {sp.get("tariff_type", "—")}</p>
      <div class="grid">
        <div><span class="label">Importada hoy</span><span class="value">{_fmt_kwh(imported_today)}</span></div>
        <div><span class="label">Importada mes</span><span class="value">{_fmt_kwh(imported_month)}</span></div>
        <div><span class="label">Coste estimado hoy</span><span class="value">{_fmt_eur(cost_today.get("total") if cost_today else None)}</span></div>
        <div><span class="label">Coste estimado mes</span><span class="value">{_fmt_eur(cost_month.get("total") if cost_month else None)}</span></div>
        <div><span class="label">Potencia contratada punta</span><span class="value">{_fmt_kw(contract.get("contractedPowerPuntaKw"))}</span></div>
        <div><span class="label">Potencia contratada valle</span><span class="value">{_fmt_kw(contract.get("contractedPowerValleKw"))}</span></div>
        <div><span class="label">Término de potencia (día)</span><span class="value">{_fmt_eur(power_daily)}</span></div>
        <div><span class="label">Compensación excedentes (mes)</span><span class="value">{_fmt_eur(surplus_month)}</span></div>
      </div>
    </div>
    """


def _dashboard_html(hass: HomeAssistant) -> str:
    cards = []
    for coordinator in hass.data.get(DOMAIN, {}).values():
        for bundle in coordinator.data.values():
            cards.append(_card_html(bundle, coordinator))
    body = "".join(cards) or "<p>Todavía no hay datos — espera a la próxima actualización.</p>"

    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>e-distribución</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<style>
  body{{font-family:system-ui,sans-serif;margin:0;padding:1rem;background:#111;color:#eee}}
  .card{{background:#1c1c1c;border-radius:8px;padding:1rem 1.2rem;margin-bottom:1rem}}
  h2{{margin:0 0 .2rem;font-size:1.1rem}}
  .cups{{color:#789;font-size:.8rem;margin:0 0 .8rem}}
  .badge{{font-size:.7rem;background:#333;padding:.1rem .5rem;border-radius:1rem;color:#9ab;margin-left:.4rem}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:.6rem}}
  .label{{display:block;color:#789;font-size:.75rem}}
  .value{{display:block;font-size:1.05rem;font-weight:600}}
</style></head>
<body>{body}</body></html>"""


class EdistribucionDashboardView(HomeAssistantView):
    """Sirve el HTML del panel — requiere sesión de HA (misma cookie que el resto del frontend,
    el iframe se carga en el mismo origen)."""

    url = VIEW_URL
    name = "api:edistribucion:dashboard"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        return web.Response(text=_dashboard_html(hass), content_type="text/html")


def async_register_panel(hass: HomeAssistant) -> None:
    """Registra el panel UNA sola vez por instancia de HA (aunque haya varias entradas de
    configuración de e-distribución, todas comparten el mismo panel)."""
    if hass.data.get(_PANEL_REGISTERED_KEY):
        return
    hass.http.register_view(EdistribucionDashboardView)
    frontend.async_register_built_in_panel(
        hass,
        component_name="iframe",
        sidebar_title="e-distribución",
        sidebar_icon="mdi:transmission-tower",
        frontend_url_path=PANEL_URL_PATH,
        config={"url": VIEW_URL},
        require_admin=False,
    )
    hass.data[_PANEL_REGISTERED_KEY] = True


def async_unregister_panel(hass: HomeAssistant) -> None:
    """Quita el panel cuando se descarga la ÚLTIMA entrada de configuración de e-distribución."""
    if not hass.data.get(_PANEL_REGISTERED_KEY):
        return
    frontend.async_remove_panel(hass, PANEL_URL_PATH)
    hass.data.pop(_PANEL_REGISTERED_KEY, None)
