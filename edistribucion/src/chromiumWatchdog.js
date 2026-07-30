const fs = require("fs");

const CHECK_INTERVAL_MS = 60_000; // cada minuto
const MAX_AGE_MS = 3 * 60_000; // un login normal tarda ~15s; 3 min es margen de sobra antes de considerarlo huérfano

/**
 * Vigilante de procesos Chromium huérfanos. `playwrightLogin.js` siempre cierra el navegador en un
 * `finally`, así que en condiciones normales no debería quedar ninguno vivo entre logins — pero si
 * el proceso se cae a medias (p.ej. sin memoria en una Raspberry Pi), puede quedar un
 * `chrome-headless-shell` colgado consumiendo CPU/RAM indefinidamente. Este vigilante los detecta
 * (leyendo /proc directamente, sin depender de `ps`/`procps`) y los mata si llevan vivos más de
 * `MAX_AGE_MS`.
 */
function isChromiumProcess(pid) {
  try {
    const cmdline = fs.readFileSync(`/proc/${pid}/cmdline`, "utf8");
    return cmdline.includes("chrome-headless-shell") || cmdline.includes("headless_shell");
  } catch {
    return false;
  }
}

function processAgeMs(pid) {
  try {
    return Date.now() - fs.statSync(`/proc/${pid}`).mtimeMs;
  } catch {
    return 0;
  }
}

function sweepOrphanChromiumProcesses() {
  let pids;
  try {
    pids = fs.readdirSync("/proc").filter((name) => /^\d+$/.test(name));
  } catch {
    return; // no estamos en Linux (desarrollo local en otro SO) — no hay nada que vigilar
  }

  let killed = 0;
  for (const pid of pids) {
    if (!isChromiumProcess(pid)) continue;
    if (processAgeMs(pid) < MAX_AGE_MS) continue;
    try {
      process.kill(Number(pid), "SIGKILL");
      killed++;
    } catch {
      /* puede que ya no exista, o no tengamos permiso — no pasa nada */
    }
  }
  if (killed > 0) {
    console.warn(`Watchdog: ${killed} proceso(s) de Chromium huérfano(s) (>${MAX_AGE_MS / 60_000} min vivos) eliminado(s).`);
  }
}

function startChromiumWatchdog() {
  setInterval(sweepOrphanChromiumProcesses, CHECK_INTERVAL_MS).unref();
}

module.exports = { startChromiumWatchdog, sweepOrphanChromiumProcesses };
