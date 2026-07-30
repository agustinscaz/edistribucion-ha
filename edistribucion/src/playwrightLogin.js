const { chromium } = require("playwright");
const { InvalidCredentialsError } = require("./errors");

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

    // La acción de login, si tiene éxito, dispara casi al instante una navegación (frontdoor.jsp)
    // que descarta el cuerpo de la respuesta antes de poder leerlo con la API normal de Playwright
    // (`response.json()` falla con "Response body is not available for a response that was
    // navigated away from"). Por eso se intercepta por CDP (`Fetch` domain, pausando la respuesta
    // ANTES de que la página pueda navegar), igual que se resolvió este mismo problema durante la
    // investigación inicial — ver README, sección "Cómo funciona".
    const client = await context.newCDPSession(page);
    await client.send("Network.enable");
    await client.send("Fetch.enable", { patterns: [{ urlPattern: "*LightningLoginForm.login*", requestStage: "Response" }] });

    let resolveLoginCapture;
    const loginCapturePromise = new Promise((resolve) => {
      resolveLoginCapture = resolve;
    });
    client.on("Fetch.requestPaused", async (e) => {
      try {
        const body = await client.send("Fetch.getResponseBody", { requestId: e.requestId });
        const text = body.base64Encoded ? Buffer.from(body.body, "base64").toString() : body.body;
        resolveLoginCapture({ json: JSON.parse(text) });
      } catch (err) {
        resolveLoginCapture({ error: err });
      } finally {
        await client.send("Fetch.continueRequest", { requestId: e.requestId }).catch(() => {});
      }
    });

    // getLoginInfo solo se dispara DESPUÉS de un login realmente correcto (la propia página
    // autenticada la pide) — con credenciales incorrectas nunca llega, así que se convierte el
    // rechazo en una resolución con el error para no dejar una unhandled rejection si nunca la
    // llegamos a esperar (login rechazado antes de llegar a este punto).
    const loginInfoPromise = page
      .waitForResponse((res) => res.request().method() === "POST" && res.url().includes("WP_Monitor_CTRL.getLoginInfo"), {
        timeout: 20000,
      })
      .catch((e) => e);

    await page.getByRole("button", { name: /entrar/i }).click();

    const loginCapture = await loginCapturePromise;
    if (loginCapture.error) throw loginCapture.error;
    const loginActionResult = loginCapture.json.actions?.[0];
    if (!loginActionResult || loginActionResult.state !== "SUCCESS") {
      throw new Error(`Login rechazado: ${JSON.stringify(loginActionResult?.error ?? loginCapture.json).slice(0, 400)}`);
    }
    // Con credenciales incorrectas, e-distribución responde state:"SUCCESS" pero con returnValue
    // como STRING de error (p.ej. "Usuario o contraseña no válidos"), no como el objeto de datos
    // esperado — se distingue explícitamente para poder avisar del motivo real.
    if (typeof loginActionResult.returnValue === "string" && loginActionResult.returnValue) {
      throw new InvalidCredentialsError(loginActionResult.returnValue);
    }

    const loginInfoRes = await loginInfoPromise;
    if (loginInfoRes instanceof Error) throw loginInfoRes;
    const loginInfoJson = await loginInfoRes.json();
    const loginInfoAction = loginInfoJson.actions?.[0];
    if (!loginInfoAction || loginInfoAction.state !== "SUCCESS") {
      throw new Error(`No se pudo leer la info de cuenta tras el login: ${JSON.stringify(loginInfoAction?.error ?? loginInfoJson).slice(0, 400)}`);
    }
    const loginInfo = loginInfoAction.returnValue;
    const visId = loginInfo?.WP_LastLoginAs__c ?? loginInfo?.authList?.[0]?.value ?? null;
    if (!visId) throw new Error("Login OK pero no se pudo determinar 'visId' (¿cambió la respuesta de getLoginInfo?)");

    await page.waitForTimeout(500);
    if (page.url().includes("/login")) {
      throw new InvalidCredentialsError("Credenciales incorrectas (sigue en la página de login tras enviar el formulario)");
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
