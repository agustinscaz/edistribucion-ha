const { chromium } = require("playwright");

/**
 * Hace login real contra la Zona Privada de e-distribución usando un navegador Chromium real
 * (Playwright) y devuelve todo lo necesario para hacer las llamadas de datos SIGUIENTES por HTTP
 * puro, sin navegador — ver dataApi.js.
 *
 * Por qué hace falta un navegador para el login pero no para lo demás: el login pasa por una
 * verificación de "sesión terminada correctamente" (`__Host-ERIC_PROD-*`, una cookie de riesgo/
 * antibot de 60s de vida) que solo se concede en peticiones de NAVEGACIÓN completas (document),
 * no en las llamadas XHR/fetch que la propia SPA hace después. Una vez logueado, el `aura.token` +
 * cookies de sesión (`sid`, etc.) capturados de una llamada real de la página siguen siendo válidos
 * para construir manualmente otras acciones Apex por fetch — confirmado en pruebas (ver
 * investigación del proyecto hermano `edistribucion-api`).
 *
 * @returns {Promise<{ cookieHeader: string, auraToken: string, auraContext: object, visId: string,
 *   name: string, supplyPoints: Array<object> }>}
 */
async function loginAndCaptureSession({ dni, password, baseUrl }) {
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext();
    const page = await context.newPage();

    await page.goto(`${baseUrl}/areaprivada/s/login/?language=es`, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForTimeout(1200);
    try {
      await page.getByRole("button", { name: /aceptar todo/i }).click({ timeout: 3000 });
    } catch {
      /* banner de cookies no siempre aparece */
    }

    await page.locator("#input-8").fill(dni);
    await page.locator("#input-9").fill(password);

    const loginInfoPromise = page.waitForResponse(
      (res) => res.request().method() === "POST" && res.url().includes("WP_Monitor_CTRL.getLoginInfo"),
      { timeout: 20000 }
    );
    await page.getByRole("button", { name: /entrar/i }).click();
    const loginRes = await loginInfoPromise;
    const loginJson = await loginRes.json();
    const loginAction = loginJson.actions?.[0];
    if (!loginAction || loginAction.state !== "SUCCESS") {
      throw new Error(`Login fallido: ${JSON.stringify(loginAction?.error ?? loginJson).slice(0, 400)}`);
    }
    const loginInfo = loginAction.returnValue;
    const visId = loginInfo?.WP_LastLoginAs__c ?? loginInfo?.authList?.[0]?.value ?? null;
    if (!visId) throw new Error("Login OK pero no se pudo determinar 'visId' (¿cambió la respuesta de getLoginInfo?)");

    await page.waitForTimeout(500);
    if (page.url().includes("/login")) {
      throw new Error("Login fallido: credenciales incorrectas o la web cambió de formato");
    }

    // Capturamos el aura.token/aura.context reales interceptando el POST que la propia página hace
    // al pedir la lista de suministros — así de paso obtenemos supplyPoints gratis.
    let capturedPostData = null;
    page.on("request", (req) => {
      if (!capturedPostData && req.method() === "POST" && req.url().includes("/sfsites/aura")) {
        capturedPostData = req.postData();
      }
    });

    const supplyResPromise = page.waitForResponse(
      (res) => res.request().method() === "POST" && res.url().includes("WP_DescargaCertificadosLectura_CTRL.getListCups"),
      { timeout: 20000 }
    );
    await page.goto(`${baseUrl}/areaprivada/s/wp-downloadcertificates`, { waitUntil: "networkidle", timeout: 30000 }).catch(() => {});
    const supplyRes = await supplyResPromise;
    const supplyJson = await supplyRes.json();
    const supplyAction = supplyJson.actions?.[0];
    if (!supplyAction || supplyAction.state !== "SUCCESS") {
      throw new Error(`No se pudo listar suministros tras login: ${JSON.stringify(supplyAction?.error ?? supplyJson).slice(0, 400)}`);
    }
    const supplyPoints = (supplyAction.returnValue?.data?.lstCups ?? []).map((c) => ({
      cups: c.Name,
      cupsId: c.CUPs__c,
      contId: c.Id,
      address: c.Provisioning_address__c,
      tariff: c.rate,
      active: !c.Version_end_date__c,
      startDate: c.Version_start_date__c ?? null,
      endDate: c.Version_end_date__c ?? null,
    }));

    if (!capturedPostData) throw new Error("No se pudo capturar ningún POST real para robar el aura.token");
    const params = new URLSearchParams(capturedPostData);
    const auraToken = params.get("aura.token");
    const auraContext = JSON.parse(params.get("aura.context"));
    if (!auraToken || !auraContext) throw new Error("No se pudo extraer aura.token/aura.context del POST capturado");

    const cookies = await context.cookies();
    const cookieHeader = cookies.map((c) => `${c.name}=${c.value}`).join("; ");

    return { cookieHeader, auraToken, auraContext, visId, name: loginInfo.Name, supplyPoints };
  } finally {
    await browser.close().catch(() => {});
  }
}

module.exports = { loginAndCaptureSession };
