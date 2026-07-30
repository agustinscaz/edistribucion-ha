const { loginAndCaptureSession } = require("./playwrightLogin");
const { AuthError } = require("./dataApi");

/**
 * Gestiona UNA sesión (una cuenta e-distribución, la configurada en las opciones del add-on).
 * Hace login con Playwright solo cuando hace falta (arranque, o cuando una llamada de datos falla
 * por sesión caducada) — el resto de llamadas van por HTTP puro (ver dataApi.js). Así el navegador
 * no está nunca encendido salvo en el momento puntual de loguear.
 */
class EdistribucionSession {
  constructor({ dni, password, baseUrl }) {
    this.dni = dni;
    this.password = password;
    this.baseUrl = baseUrl;
    this.state = null; // { cookieHeader, auraToken, auraContext, visId, name, supplyPoints, baseUrl }
    this._loginPromise = null;
  }

  async _login() {
    if (!this._loginPromise) {
      this._loginPromise = loginAndCaptureSession({ dni: this.dni, password: this.password, baseUrl: this.baseUrl })
        .then((captured) => {
          this.state = { ...captured, baseUrl: this.baseUrl };
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
