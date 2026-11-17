const fs = require("fs");
const { loginAndCaptureSession } = require("./playwrightLogin");
const { getContractedPower: fetchContractedPower } = require("./dataApi");
const { AuthError, ActionError, InvalidCredentialsError } = require("./errors");

const SESSION_FILE = process.env.EDISTRIBUCION_SESSION_FILE || "/data/session.json";

// Un ActionError (la acción respondió con error, sin señal de sesión caducada) solo se considera
// "quizá es la sesión" si la sesión ya tiene cierta edad y nadie la ha usado con éxito hace poco —
// y aun así, como mucho un relogin por este motivo cada AMBIGUOUS_RELOGIN_MIN_INTERVAL_MS. Antes
// cualquier error de una acción relogueaba con Chromium (60-90 MB escritos en disco cada vez).
const RECENT_SUCCESS_MS = 10 * 60_000;
const AMBIGUOUS_RELOGIN_MIN_INTERVAL_MS = 30 * 60_000;

// Mantener viva la sesión por HTTP (sin navegador): si nadie la usa en `keepalive_minutes`, se hace
// una llamada Aura ligera (potencia contratada) para que la sesión de Salesforce no caduque por
// inactividad entre sondeos. 0 = desactivado.
const KEEPALIVE_CHECK_MS = 60_000;

/**
 * Gestiona UNA sesión (una cuenta e-distribución, la configurada en las opciones del add-on).
 * Hace login con Playwright solo cuando hace falta (arranque sin sesión guardada, sesión guardada
 * caducada, o cuando una llamada de datos falla por sesión caducada) — el resto de llamadas van por
 * HTTP puro (ver dataApi.js). Así el navegador no está nunca encendido salvo en el momento puntual
 * de loguear.
 *
 * La sesión (cookies + aura.token, unos pocos KB) se guarda en `/data/session.json` solo cuando
 * cambia (tras un login) para sobrevivir a reinicios normales del add-on sin volver a pasar por
 * Chromium — se valida sola en la primera llamada de datos real (si ya no sirve, `withSession` lo
 * detecta y hace un login fresco automáticamente).
 */
class EdistribucionSession {
  constructor({ dni, password, baseUrl, keepaliveMinutes = 0 }) {
    this.dni = dni;
    this.password = password;
    this.baseUrl = baseUrl;
    this.state = this._loadPersisted();
    this._loginPromise = null;
    this.loginCount = 0;
    this.lastLoginAt = null;
    this.lastLoginReason = null;
    this.lastLoginError = null; // { message, invalidCredentials } — para el panel de estado
    this.lastSuccessAt = null; // última llamada de datos correcta con la sesión actual
    this.lastAmbiguousReloginAt = 0;
    this.keepaliveMs = Math.max(0, Number(keepaliveMinutes) || 0) * 60_000;
    this.keepaliveCount = 0;
    this.lastKeepaliveError = null;
    this._lastPersistedJson = this.state ? JSON.stringify({ ...this.state, dni: this.dni }) : null;
  }

  _loadPersisted() {
    try {
      const saved = JSON.parse(fs.readFileSync(SESSION_FILE, "utf8"));
      if (saved?.baseUrl === this.baseUrl && saved?.dni === this.dni) {
        console.log("Sesión previa cargada desde disco (se validará en la próxima llamada de datos).");
        return saved;
      }
    } catch {
      /* no hay sesión guardada, o no es de esta cuenta/URL — se hará login normal */
    }
    return null;
  }

  _persist(state) {
    const json = JSON.stringify({ ...state, dni: this.dni });
    if (json === this._lastPersistedJson) return; // nada nuevo que escribir
    try {
      // Escritura atómica (tmp + rename en el mismo directorio) para no dejar un JSON a medias.
      const tmp = `${SESSION_FILE}.tmp`;
      fs.writeFileSync(tmp, json, { mode: 0o600 });
      fs.renameSync(tmp, SESSION_FILE);
      this._lastPersistedJson = json;
    } catch (e) {
      console.warn("No se pudo guardar la sesión en disco (se seguirá funcionando, solo no sobrevivirá a un reinicio):", e.message);
    }
  }

  async _login(reason) {
    if (!this._loginPromise) {
      console.log(`Login con Chromium (motivo: ${reason}).`);
      this._loginPromise = loginAndCaptureSession({ dni: this.dni, password: this.password, baseUrl: this.baseUrl })
        .then((captured) => {
          this.state = { ...captured, baseUrl: this.baseUrl };
          this._persist(this.state);
          this.loginCount += 1;
          this.lastLoginAt = new Date();
          this.lastLoginReason = reason;
          this.lastLoginError = null;
          this.lastSuccessAt = null;
          return this.state;
        })
        .catch((err) => {
          this.lastLoginError = { message: err.message, invalidCredentials: err instanceof InvalidCredentialsError };
          throw err;
        })
        .finally(() => {
          this._loginPromise = null;
        });
    }
    return this._loginPromise;
  }

  async ensureLoggedIn() {
    if (this._loginPromise) return this._loginPromise;
    if (!this.state) await this._login("sin sesión");
    return this.state;
  }

  _sessionLooksValid() {
    const now = Date.now();
    const loginAge = this.lastLoginAt ? now - this.lastLoginAt.getTime() : Infinity;
    const sinceSuccess = this.lastSuccessAt ? now - this.lastSuccessAt.getTime() : Infinity;
    return loginAge < RECENT_SUCCESS_MS || sinceSuccess < RECENT_SUCCESS_MS;
  }

  /** Ejecuta `fn(state)`; si falla por sesión caducada, hace un login fresco y reintenta UNA vez. */
  async withSession(fn) {
    const state = await this.ensureLoggedIn();
    try {
      const result = await fn(state);
      if (state === this.state) this.lastSuccessAt = new Date();
      return result;
    } catch (e) {
      let reason;
      if (e instanceof AuthError) {
        reason = `sesión caducada: ${e.message.slice(0, 160)}`;
      } else if (e instanceof ActionError) {
        // Error de la propia acción (p.ej. sin datos para esa fecha): casi nunca es la sesión.
        if (this._sessionLooksValid()) throw e;
        if (Date.now() - this.lastAmbiguousReloginAt < AMBIGUOUS_RELOGIN_MIN_INTERVAL_MS) throw e;
        this.lastAmbiguousReloginAt = Date.now();
        reason = `error ambiguo con sesión inactiva: ${e.message.slice(0, 160)}`;
      } else {
        throw e;
      }
      const fresh = await this._relogin(state, reason);
      const result = await fn(fresh);
      if (fresh === this.state) this.lastSuccessAt = new Date();
      return result;
    }
  }

  /** Login fresco, salvo que otra petición concurrente ya lo esté haciendo o lo haya hecho mientras
   * esta fallaba (en ese caso se reutiliza, no se abre un segundo Chromium). */
  async _relogin(staleState, reason) {
    if (this._loginPromise) return this._loginPromise;
    if (this.state && this.state !== staleState) return this.state;
    this.state = null;
    return this._login(reason);
  }

  async getInfo() {
    const state = await this.ensureLoggedIn();
    return { name: state.name, visId: state.visId };
  }

  async getSupplyPoints({ forceRefresh = false } = {}) {
    if (forceRefresh) {
      this.state = null;
      await this._login("forzado (relogin manual)");
    }
    const state = await this.ensureLoggedIn();
    return state.supplyPoints;
  }

  /** Potencia contratada real (punta/valle, kW) + metadatos del contrato, sacados directamente de
   * la distribuidora — no hace falta que el usuario los teclee. */
  async getContractedPower(contId) {
    return this.withSession((state) => fetchContractedPower(state, contId));
  }

  /** Llamada HTTP ligera para que la sesión no caduque por inactividad. Nunca abre el navegador:
   * si la sesión ya no vale, lo dirá la próxima petición real, que es la que reloguea. */
  async _keepaliveTick() {
    if (!this.keepaliveMs || !this.state || this._loginPromise) return;
    const last = Math.max(this.lastSuccessAt?.getTime() ?? 0, this.lastLoginAt?.getTime() ?? 0);
    if (!last || Date.now() - last < this.keepaliveMs) return;
    const sp = (this.state.supplyPoints ?? []).find((s) => s.active) ?? this.state.supplyPoints?.[0];
    if (!sp) return;
    const state = this.state;
    try {
      await fetchContractedPower(state, sp.contId);
      if (state === this.state) this.lastSuccessAt = new Date();
      this.keepaliveCount += 1;
      this.lastKeepaliveError = null;
    } catch (e) {
      this.lastKeepaliveError = e.message;
      if (e instanceof AuthError) {
        console.warn(`Keepalive: la sesión ya no es válida (${e.message.slice(0, 160)}); se relogueará en la próxima petición.`);
        if (state === this.state) this.state = null;
      }
    }
  }

  startKeepalive() {
    if (!this.keepaliveMs) return;
    setInterval(() => {
      this._keepaliveTick().catch(() => {});
    }, KEEPALIVE_CHECK_MS).unref();
  }
}

module.exports = { EdistribucionSession };
