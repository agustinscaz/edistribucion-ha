// Herramienta de diagnóstico (NO se usa en producción): lista los descriptores Apex que la propia
// SPA de e-distribución usa hoy al abrir las páginas de medidas y contrato, para averiguar el
// nombre nuevo de 'WP_Measure_v3_CTRL' / 'WP_ContractATRDetail_CTRL'.
//   EDISTRIBUCION_DNI=... EDISTRIBUCION_PASSWORD=... node tools/discover-actions.js <contId>
// Imprime solo descriptor, callingDescriptor, claves de params y estado; nunca token ni cookies.
const { chromium } = require("playwright");
const base = process.env.EDISTRIBUCION_BASE_URL || "https://zonaprivada.edistribucion.com";
const [contId, visId] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await (await browser.newContext()).newPage();
  page.on("response", async (res) => {
    if (res.request().method() !== "POST" || !res.url().includes("/sfsites/aura")) return;
    const msg = JSON.parse(new URLSearchParams(res.request().postData() ?? "").get("message") ?? "{}");
    let states = [];
    try { states = (await res.json()).actions?.map((a) => a.state) ?? []; } catch {}
    for (const a of msg.actions ?? []) console.log(a.descriptor, "|", a.callingDescriptor, "| params:", Object.keys(a.params ?? {}).join(","), "|", states.join(","));
  });
  await page.goto(`${base}/areaprivada/s/login/?language=es`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1200);
  await page.getByRole("button", { name: /aceptar todo/i }).click({ timeout: 3000 }).catch(() => {});
  await page.locator("#input-8").fill(process.env.EDISTRIBUCION_DNI);
  await page.locator("#input-9").fill(process.env.EDISTRIBUCION_PASSWORD);
  await page.getByRole("button", { name: /entrar/i }).click();
  await page.waitForTimeout(8000);
  for (const path of ["wp-measure-detail-v4", "wp-measure-detail", "wp-atrcontractdetail", "wp-measure"]) {
    console.log(`--- ${path}`);
    await page.goto(`${base}/areaprivada/s/${path}?aId=${contId ?? ""}&atrid=${contId ?? ""}${visId ? `&vis=${visId}` : ""}`, { waitUntil: "networkidle", timeout: 30000 }).catch(() => {});
    await page.waitForTimeout(3000);
  }
  await browser.close();
})();
