const { AuthError, ActionError, ApexAccessError, UpstreamError } = require("./errors");

const UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.7922.34 Safari/537.36";

// Salesforce: el usuario está logueado pero su perfil no tiene la clase Apex (permiso retirado o
// clase renombrada). Se comprueba ANTES que la de sesión: el texto puede contener "/login" etc.
const APEX_ACCESS_RE = /No tiene acceso a la clase de Apex|do(es)? not have access to (the )?Apex class|INSUFFICIENT_ACCESS.*Apex/i;

// Señales de sesión caducada/inválida en las respuestas de Aura/Salesforce.
const SESSION_INVALID_RE = /invalidSession|clientOutOfSync|INVALID_SESSION_ID|session (has )?expired|sesi[oó]n (ha )?caducad|not logged in|\/login/i;

/**
 * Construye y ejecuta manualmente una acción Apex vía el endpoint Aura, reusando el `aura.token` +
 * cookies capturados en un login previo (ver playwrightLogin.js) — sin navegador. Si la sesión ya
 * no es válida (token/sid caducados), lanza AuthError para que quien llame decida hacer un login
 * fresco y reintentar.
 */
async function callAuraAction(session, { descriptor, callingDescriptor, params, pageURI, extra = {} }) {
  // `extra`: claves propias de la acción (p. ej. longRunning, que la SPA pone en los métodos Apex de tipo continuation).
  const message = {
    actions: [{ id: "1;a", descriptor, callingDescriptor, params, ...extra }],
  };
  const [, apexClass, apexMethod] = /^apex:\/\/([^/]+)\/ACTION\$(.+)$/.exec(descriptor) ?? [];
  const body = new URLSearchParams({
    message: JSON.stringify(message),
    "aura.context": JSON.stringify(session.auraContext),
    "aura.pageURI": pageURI,
    "aura.token": session.auraToken,
  });

  const res = await fetch(`${session.baseUrl}/areaprivada/s/sfsites/aura?r=1&${apexClass ? `other.${apexClass}.${apexMethod}=1` : "apex.action=1"}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
      Accept: "*/*",
      "User-Agent": UA,
      Origin: session.baseUrl,
      Referer: `${session.baseUrl}${pageURI}`,
      Cookie: session.cookieHeader,
    },
    body: body.toString(),
  });

  if (res.status === 401 || res.status === 403) throw new AuthError(`HTTP ${res.status} al llamar a ${descriptor}`);
  if (!res.ok) throw new UpstreamError(`HTTP ${res.status} al llamar a ${descriptor}`);
  const text = await res.text();
  let json;
  try {
    json = JSON.parse(text);
  } catch {
    // Sesión caducada: Salesforce redirige a la página de login (HTML) o devuelve el prefijo
    // "*/" de Aura con un evento de sesión inválida.
    throw new AuthError(`Respuesta no-JSON de ${descriptor} (sesión probablemente caducada)`);
  }
  if (APEX_ACCESS_RE.test(text.slice(0, 2000)) && !json.actions?.[0]?.returnValue) {
    throw new ApexAccessError(`Acceso denegado a ${descriptor}: ${text.slice(0, 300)}`, descriptor);
  }
  if (json.exceptionEvent || SESSION_INVALID_RE.test(JSON.stringify(json.event ?? ""))) {
    throw new AuthError(`Aura devolvió un evento de sesión en ${descriptor}: ${JSON.stringify(json.event ?? json).slice(0, 300)}`);
  }
  const action = json.actions?.[0];
  if (!action) throw new AuthError(`Respuesta sin acciones de ${descriptor}: ${text.slice(0, 300)}`);
  if (action.state !== "SUCCESS") {
    const detail = JSON.stringify(action?.error ?? json).slice(0, 400);
    if (APEX_ACCESS_RE.test(detail)) throw new ApexAccessError(`Acceso denegado a ${descriptor}: ${detail}`, descriptor);
    if (SESSION_INVALID_RE.test(detail)) throw new AuthError(`Acción ${descriptor} falló por sesión: ${detail}`);
    throw new ActionError(`Acción ${descriptor} falló: ${detail}`);
  }
  return action.returnValue;
}

/** Ruta nueva del portal (WP_Measure_CTRL): lstData solo trae un valor por hora (`value`, string con coma)
 * y NO el exportado. El importado (magnitude A1) y el exportado (A2) por hora están en `data.response`,
 * un string JSON: measureList[].dailyLoadProfileList[].sampleList[] {hour "HH:MM", consumption|null, obtainingMode}.
 * Devuelve Map "DD/MM/YYYY HH" -> { imported, exported, real } (consumption null = aún sin dato). */
function parseHourlyResponse(data) {
  const out = new Map();
  if (typeof data.response !== "string") return out;
  let parsed;
  try { parsed = JSON.parse(data.response); } catch { return out; }
  for (const m of parsed.measureList ?? []) {
    const field = m.magnitude === "A1" ? "imported" : m.magnitude === "A2" ? "exported" : null;
    if (!field) continue;
    for (const day of m.dailyLoadProfileList ?? []) {
      const [y, mo, d] = String(day.measureDate).slice(0, 10).split("-");
      for (const smp of day.sampleList ?? []) {
        const key = `${d}/${mo}/${y} ${String(smp.hour).slice(0, 2)}`;
        const cur = out.get(key) ?? { imported: null, exported: null, real: false };
        if (typeof smp.consumption === "number") {
          cur[field] = smp.consumption;
          if (smp.obtainingMode === "R") cur.real = true;
        }
        out.set(key, cur);
      }
    }
  }
  return out;
}

function normalizeConsumption(data) {
  const hourlyByDate = {};
  const dailyTotalsMap = new Map();
  const resp = parseHourlyResponse(data);
  let params = {};
  try { params = typeof data.mapParamsWS === "string" ? JSON.parse(data.mapParamsWS) : (data.mapParamsWS ?? {}); } catch {}
  for (const day of data.lstData ?? []) {
    for (const p of day) {
      const date = p.date;
      let imported = p.valueDouble ?? null; // ruta antigua (WP_Measure_v3_CTRL)
      let exported = p.a2Double ?? null;
      let real = p.real;
      if (resp.size) {
        const r = resp.get(`${date} ${String(p.hour).slice(0, 2)}`); // "24 - 25 h" (25.ª hora del cambio de hora) no tiene muestra
        if (!r || r.imported === null) continue; // hora sin dato todavía: no se inventa un 0
        imported = r.imported;
        exported = r.exported ?? 0;
        real = r.real;
      }
      if (!hourlyByDate[date]) hourlyByDate[date] = [];
      hourlyByDate[date].push({ hour: p.hour, importedKwh: imported, exportedKwh: exported, real, invoiced: p.invoiced });
      const acc = dailyTotalsMap.get(date) ?? { importedKwh: 0, exportedKwh: 0 };
      acc.importedKwh += imported ?? 0;
      acc.exportedKwh += exported ?? 0;
      dailyTotalsMap.set(date, acc);
    }
  }
  const round = (n) => Math.round(n * 1000) / 1000;
  return {
    cups: data.cupsName,
    startDate: data.mapParamsWS_v2?.startDate ?? params.startDate,
    endDate: data.mapParamsWS_v2?.endDate ?? params.endDate,
    totalImportedKwh: Number(String(data.totalValue).replace(",", ".")),
    totalExportedKwh: round([...dailyTotalsMap.values()].reduce((s, d) => s + d.exportedKwh, 0)),
    dailyTotals: [...dailyTotalsMap.entries()].map(([date, d]) => ({ date, importedKwh: round(d.importedKwh), exportedKwh: round(d.exportedKwh) })),
    hourlyByDate,
  };
}

async function getDefaultConsumption(session, contId) {
  const rv = await callAuraAction(session, {
    descriptor: "apex://WP_Measure_CTRL/ACTION$getInfo",
    callingDescriptor: "markup://c:WP_Measure_Detail",
    params: { contId },
    extra: { longRunning: true },
    pageURI: `/areaprivada/s/wp-measure-detail?aId=${contId}&vis=${session.visId}`,
  });
  return normalizeConsumption(rv.data);
}

/** rangeType: "1"=día, "2"=semana, "3"=mes. referenceDate: "YYYY-MM-DD" (por defecto, hoy). */
async function getConsumptionByRange(session, contId, rangeType, referenceDate) {
  const startDate = referenceDate ?? new Date().toISOString().slice(0, 10);
  const rv = await callAuraAction(session, {
    descriptor: "apex://WP_Measure_CTRL/ACTION$getChartPointsByRange",
    callingDescriptor: "markup://c:WP_Measure_Detail_Filter_By_Dates",
    params: { contId, type: String(rangeType), startDate },
    extra: { version: null, longRunning: true },
    pageURI: `/areaprivada/s/wp-measure-detail?aId=${contId}&vis=${session.visId}`,
  });
  return normalizeConsumption(rv.data);
}

async function getMaxPowerDemand(session, cupsId) {
  const now = new Date();
  const endDate = `${now.getMonth() + 1}/${now.getFullYear()}`;
  const start = new Date(now.getFullYear() - 1, now.getMonth(), 1);
  const startDate = `${start.getMonth() + 1}/${start.getFullYear()}`;

  const rv = await callAuraAction(session, {
    descriptor: "apex://WP_MaximeterHistogram_CTRL/ACTION$getHistogramPoints",
    callingDescriptor: "markup://c:WP_MaximeterHistogramDetail",
    params: { mapParams: { startDate, endDate, id: cupsId, sIdentificador: session.visId } },
    pageURI: `/areaprivada/s/wp-maximeterhistogramdetail?aId=${cupsId}&sId=${session.visId}`,
  });
  if (!rv?.data) throw new Error("Sin datos de potencia máxima (¿suministro sin telegestión o >50kW?)");
  const d = rv.data;
  return {
    cups: d.cups,
    address: d.address,
    hasData: d.hasData,
    maxValue: d.maxValue,
    points: (d.lstData ?? []).map((p) => ({
      date: p.date,
      hour: p.hour,
      valueKw: p.value,
      periods: Object.fromEntries(Object.entries(p.periodData ?? {}).map(([period, v]) => [period, { valueKw: v.val, date: v.fmtDate }])),
    })),
  };
}

function _parseSpanishFloat(value) {
  if (value === undefined || value === null || value === "") return null;
  const n = Number(String(value).replace(",", "."));
  return Number.isFinite(n) ? n : null;
}

/** Detalle real del contrato ATR de un suministro: potencia contratada 1/2 (kW) = punta/valle en
 * la 2.0TD, más metadatos del contrato — sacado de la propia distribuidora, no de lo que teclees
 * tú. OJO: el `contId` que ya usa el resto del add-on (de `getListCups`) ES el propio identificador
 * del contrato ATR (`atrId`) — confirmado en pruebas en vivo, no hace falta ningún mapeo aparte. */
async function getContractedPower(session, contId) {
  const rv = await callAuraAction(session, {
    descriptor: "apex://WP_ContractATRDetail_CTRL/ACTION$getATRDetail",
    callingDescriptor: "markup://c:WP_SuppliesATRDetailForm",
    params: { atrId: contId, visSelected: session.visId },
    pageURI: `/areaprivada/s/wp-atrcontractdetail?atrid=${contId}&vis=${session.visId}`,
  });
  const fields = Object.fromEntries((rv.data ?? []).map((f) => [f.title, f.value]));
  return {
    contractCode: fields["Código contrato"] || null,
    status: fields["Estado"] || null,
    marketer: fields["Comercializadora"] || null,
    tariff: fields["Tarifa"] || null,
    contractedPowerPuntaKw: _parseSpanishFloat(fields["Potencia contratada 1 (kW)"]),
    contractedPowerValleKw: _parseSpanishFloat(fields["Potencia contratada 2 (kW)"]),
  };
}

module.exports = { AuthError, ActionError, ApexAccessError, UpstreamError, getDefaultConsumption, getConsumptionByRange, getMaxPowerDemand, getContractedPower };
