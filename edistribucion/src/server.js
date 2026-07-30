const fs = require("fs");
const express = require("express");
const { EdistribucionSession } = require("./session");
const { getDefaultConsumption, getConsumptionByRange, getMaxPowerDemand } = require("./dataApi");

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

if (!DNI || !PASSWORD) {
  console.error("Faltan credenciales: configura 'dni' y 'password' en las opciones del add-on.");
  process.exit(1);
}

const session = new EdistribucionSession({ dni: DNI, password: PASSWORD, baseUrl: BASE_URL });
const app = express();

app.get("/health", (req, res) => res.json({ ok: true }));

app.get("/info", async (req, res) => {
  try {
    res.json(await session.getInfo());
  } catch (e) {
    res.status(502).json({ error: e.message });
  }
});

app.get("/supply-points", async (req, res) => {
  try {
    res.json(await session.getSupplyPoints({ forceRefresh: req.query.refresh === "1" }));
  } catch (e) {
    res.status(502).json({ error: e.message });
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
    res.status(502).json({ error: e.message });
  }
});

app.get("/max-power-demand/:cupsId", async (req, res) => {
  const { cupsId } = req.params;
  try {
    const result = await session.withSession((state) => getMaxPowerDemand(state, cupsId));
    res.json(result);
  } catch (e) {
    res.status(502).json({ error: e.message });
  }
});

app.listen(PORT, "0.0.0.0", () => {
  console.log(`API local de e-distribución escuchando en :${PORT}`);
});
