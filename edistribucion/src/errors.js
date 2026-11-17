/** La sesión (cookies/token) ya no es válida — quien llame debe reloguear y reintentar. */
class AuthError extends Error {}

/** La acción Apex respondió, pero con error de la propia acción (p.ej. sin datos para esa fecha o
 * ese contrato) y sin ninguna señal de sesión caducada. NO se debe reloguear por esto: antes se
 * trataba como AuthError y cada error "de negocio" lanzaba un Chromium nuevo (60-90 MB escritos en
 * disco por login). `session.withSession` solo reloguea por esto si la sesión ya tenía tiempo y
 * nadie la ha usado con éxito hace poco, y como mucho una vez cada 30 min. */
class ActionError extends Error {}

/** Error HTTP 5xx/red del lado de e-distribución: transitorio, reloguear no lo arregla. */
class UpstreamError extends Error {}

/** El login en sí ha sido rechazado por credenciales incorrectas (no un problema de red/sesión) —
 * reintentar con las MISMAS credenciales no sirve de nada, hace falta corregirlas en la
 * configuración del add-on. Se distingue explícitamente para poder avisar de forma clara en vez de
 * un error genérico de conexión. */
class InvalidCredentialsError extends Error {}

/** e-distribución exige cambiar la contraseña de la cuenta (redirige a la pantalla de Salesforce
 * ChangePassword tras el login) — no son credenciales incorrectas, pero tampoco sirve reintentar:
 * hace falta cambiarla a mano en la Zona Privada y actualizar la nueva en el add-on. Confirmado en
 * vivo el 05-sep-2026: antes de esto se veía como un timeout genérico de 20s sin pista de la causa. */
class PasswordChangeRequiredError extends Error {}

module.exports = { AuthError, ActionError, UpstreamError, InvalidCredentialsError, PasswordChangeRequiredError };
