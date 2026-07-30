const fs = require("fs");
const { loginAndCaptureSession } = require("./playwrightLogin");
const { AuthError } = require("./dataApi");

const SESSION_FILE = process.env.EDISTRIBUCION_SESSION_FILE || "/data/session.json";

/**
 * Gestiona UNA sesión (una cuenta e-distribución, la configurada en las opciones del add-on).
 * Hace login con Playwright solo cuando hace falta (arranque sin sesión guardada, sesión guardada
 * caducada, o cuando una llamada de datos falla por sesión caducada) — el resto de llamadas van por
 * HTTP puro (ver dataApi.js). Así el navegador no está nunca encendido salvo en el momento puntual
 * de loguear.
 *
 * La sesión (cookies + aura.token) se guarda en `/data/session.json` tras cada login para
 * sobrevivir a reinicios normales del add-on (actualización, reinicio del host) sin tener que
 * volver a pasar por Chromium — se valida sola en la primera llamada de datos real (si ya no sirve,
 * `withSession` lo detecta y hace un login fresco automáticamente).
 */
class EdistribucionSession {
  constructor({ dni, password, baseUrl }) {
    this.dni = dni;
    this.password = password;
    this.baseUrl = baseUrl;
    this.state = this._loadPersisted();
    this._loginPromise = null;
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
    try {
      fs.writeFileSync(SESSION_FILE, JSON.stringify({ ...state, dni: this.dni }));
    } catch (e) {
      console.warn("No se pudo guardar la sesión en disco (se seguirá funcionando, solo no sobrevivirá a un reinicio):", e.message);
    }
  }

  async _login() {
    if (!this._loginPromise) {
      this._loginPromise = loginAndCaptureSession({ dni: this.dni, password: this.password, baseUrl: this.baseUrl })
        .then((captured) => {
          this.state = { ...captured, baseUrl: this.baseUrl };
          this._persist(this.state);
          return this.state;
        })
        .finally(() => {
          this._loginPromise = null;
        });
    }
    return this._loginPromise;
  }

  async ensureLoggedIn() {
    if (!this.state) await this._login();
    return this.state;
  }

  /** Ejecuta `fn(state)`; si falla por sesión caducada, hace un login fresco y reintenta UNA vez. */
  async withSession(fn) {
    const state = await this.ensureLoggedIn();
    try {
      return await fn(state);
    } catch (e) {
      if (!(e instanceof AuthError)) throw e;
      this.state = null;
      const fresh = await this._login();
      return fn(fresh);
    }
  }

  async getInfo() {
    const state = await this.ensureLoggedIn();
    return { name: state.name, visId: state.visId };
  }

  async getSupplyPoints({ forceRefresh = false } = {}) {
    if (forceRefresh) this.state = null;
    const state = await this.ensureLoggedIn();
    return state.supplyPoints;
  }
}

module.exports = { EdistribucionSession };
