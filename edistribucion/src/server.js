const fs = require("fs");
const express = require("express");
const { Bonjour } = require("bonjour-service");
const { EdistribucionSession } = require("./session");
const { getDefaultConsumption, getConsumptionByRange, getMaxPowerDemand } = require("./dataApi");
const { startChromiumWatchdog, getWatchdogStats } = require("./chromiumWatchdog");
const { InvalidCredentialsError } = require("./errors");

const OPTIONS_PATH = "/data/options.json";
let options = {};
try {
  options = JSON.parse(fs.readFileSync(OPTIONS_PATH, "utf8"));
} catch {
  /* fuera de Home Assistant (desarrollo local): se usan variables de entorno, ver README */
}

const DNI = options.dni || process.env.EDISTRIBUCION_DNI;
const PASSWORD = options.password || process.env.EDISTRIBUCION_PASSWORD;
const BASE_URL = options.base_url || process.env.EDISTRIBUCION_BASE_URL || "https://zonaprivada.edistribucion.com";
const PORT = Number(process.env.PORT || 8099);
const START_TIME = new Date();

if (!DNI || !PASSWORD) {
  console.error("Faltan credenciales: configura 'dni' y 'password' en las opciones del add-on.");
  process.exit(1);
}

const session = new EdistribucionSession({ dni: DNI, password: PASSWORD, baseUrl: BASE_URL });
const app = express();

/** Responde el error con el código adecuado: 401 + code distinguible si es de credenciales
 * (para que la integración de Home Assistant pueda levantar un aviso distinto al de "sin
 * conexión"), 502 para el resto (fallos de red/sesión genéricos). */
function respondError(res, e) {
  if (e instanceof InvalidCredentialsError) {
    return res.status(401).json({ error: e.message, code: "invalid_credentials" });
  }
  return res.status(502).json({ error: e.message });
}

app.get("/health", (req, res) => res.json({ ok: true }));

app.get("/info", async (req, res) => {
  try {
    res.json(await session.getInfo());
  } catch (e) {
    respondError(res, e);
  }
});

app.get("/supply-points", async (req, res) => {
  try {
    res.json(await session.getSupplyPoints({ forceRefresh: req.query.refresh === "1" }));
  } catch (e) {
    respondError(res, e);
  }
});

app.get("/consumption/:contId", async (req, res) => {
  const { contId } = req.params;
  const { range, date } = req.query;
  try {
    const result = await session.withSession((state) =>
      range ? getConsumptionByRange(state, contId, String(range), date) : getDefaultConsumption(state, contId)
    );
    res.json(result);
  } catch (e) {
    respondError(res, e);
  }
});

app.get("/max-power-demand/:cupsId", async (req, res) => {
  const { cupsId } = req.params;
  try {
    const result = await session.withSession((state) => getMaxPowerDemand(state, cupsId));
    res.json(result);
  } catch (e) {
    respondError(res, e);
  }
});

/** Potencia contratada real (punta/valle, kW) + metadatos del contrato, sacados de la propia
 * distribuidora — así la integración de Home Assistant no tiene que pedírselo al usuario. */
app.get("/contracted-power/:contId", async (req, res) => {
  const { contId } = req.params;
  try {
    const result = await session.getContractedPower(contId);
    res.json(result);
  } catch (e) {
    respondError(res, e);
  }
});

/** Fuerza un login fresco (aunque la sesión actual "parezca" válida) — útil para el botón
 * "Forzar reconexión" de la integración de Home Assistant. */
app.post("/relogin", async (req, res) => {
  try {
    await session.getSupplyPoints({ forceRefresh: true });
    res.json(await session.getInfo());
  } catch (e) {
    respondError(res, e);
  }
});

/** Mini panel de estado en HTML — útil para ver de un vistazo si todo va bien sin mirar logs ni
 * hacer peticiones JSON a mano. También accesible vía Ingress desde el propio menú de Home
 * Assistant (ver config.yaml). No expone ningún dato sensible (ni cookies, ni token, ni password). */
app.get("/", (req, res) => {
  const s = session;
  const watchdog = getWatchdogStats();
  const fmt = (d) => (d ? new Date(d).toLocaleString("es-ES") : "—");
  const loggedIn = !!s.state;
  const supplyCount = s.state?.supplyPoints?.length ?? "—";
  const credError = s.lastLoginError?.invalidCredentials;

  res.set("Content-Type", "text/html; charset=utf-8").send(`<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>e-distribución — estado</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body{font-family:system-ui,sans-serif;max-width:640px;margin:2rem auto;padding:0 1rem;background:#111;color:#eee}
  h1{font-size:1.3rem}
  table{width:100%;border-collapse:collapse;margin-top:1rem}
  td{padding:.4rem 0;border-bottom:1px solid #333}
  td:first-child{color:#9ab;width:55%}
  .ok{color:#4caf50}.bad{color:#f44336}.warn{color:#ffb300}
  code{background:#222;padding:.1rem .3rem;border-radius:3px}
</style></head>
<body>
<h1>e-distribución — estado del add-on</h1>
<table>
<tr><td>Arrancado desde</td><td>${fmt(START_TIME)}</td></tr>
<tr><td>Sesión activa</td><td class="${loggedIn ? "ok" : "warn"}">${loggedIn ? "sí" : "no (se logueará en la próxima petición)"}</td></tr>
<tr><td>Titular</td><td>${s.state?.name ?? "—"}</td></tr>
<tr><td>Suministros cacheados</td><td>${supplyCount}</td></tr>
<tr><td>Logins realizados desde el arranque</td><td>${s.loginCount}</td></tr>
<tr><td>Último login correcto</td><td>${fmt(s.lastLoginAt)}</td></tr>
<tr><td>Último error de login</td><td class="${credError ? "bad" : ""}">${
    s.lastLoginError ? (credError ? "⚠️ Credenciales incorrectas — revisa dni/password en la configuración del add-on" : s.lastLoginError.message) : "—"
  }</td></tr>
<tr><td>Procesos Chromium huérfanos eliminados</td><td>${watchdog.totalKilled}</td></tr>
<tr><td>Última revisión del watchdog</td><td>${fmt(watchdog.lastCheckAt)}</td></tr>
</table>
<p style="margin-top:1.5rem;color:#789;font-size:.85rem">Endpoints JSON: <code>/health</code> <code>/info</code> <code>/supply-points</code> <code>/consumption/:contId</code> <code>/max-power-demand/:cupsId</code> <code>/contracted-power/:contId</code> <code>POST /relogin</code></p>
</body></html>`);
});

app.listen(PORT, "0.0.0.0", () => {
  console.log(`API local de e-distribución escuchando en :${PORT}`);
});

startChromiumWatchdog();

/** Se anuncia por mDNS (_edistribucion._tcp.local) para que la integración de Home Assistant
 * pueda encontrarlo sola (config_flow con soporte de Zeroconf) sin tener que teclear host/puerto a
 * mano. Requiere que el tráfico multicast llegue (con host_network:true, comparte la red del host,
 * así que funciona igual que en cualquier otro dispositivo de la LAN). Si falla (p.ej. entorno sin
 * soporte multicast), no es crítico — el formulario manual de host/puerto sigue funcionando igual. */
try {
  new Bonjour().publish({ name: "e-distribución", type: "edistribucion", port: PORT });
} catch (e) {
  console.warn("No se pudo anunciar por mDNS (el descubrimiento automático no estará disponible, pero todo lo demás funciona igual):", e.message);
}
