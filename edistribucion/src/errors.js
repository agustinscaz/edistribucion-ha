/** La sesión (cookies/token) ya no es válida — quien llame debe reloguear y reintentar. */
class AuthError extends Error {}

/** El login en sí ha sido rechazado por credenciales incorrectas (no un problema de red/sesión) —
 * reintentar con las MISMAS credenciales no sirve de nada, hace falta corregirlas en la
 * configuración del add-on. Se distingue explícitamente para poder avisar de forma clara en vez de
 * un error genérico de conexión. */
class InvalidCredentialsError extends Error {}

module.exports = { AuthError, InvalidCredentialsError };
