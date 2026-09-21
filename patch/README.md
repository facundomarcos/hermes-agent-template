# Telegram webhook ingress + scale-to-zero (Railway)

Objetivo: que el servicio pueda **dormirse cuando no se usa** (Railway serverless duerme
tras ~5-10 min **sin tráfico saliente**) y que **un mensaje de Telegram lo despierte**.

Con Telegram hay dos modos y sólo uno deja dormir:

| modo | tráfico | ¿duerme? |
|---|---|---|
| polling (`getUpdates` cada ~10 s) | saliente continuo | nunca |
| webhook (Telegram hace POST) | sólo cuando alguien escribe | sí |

Problema: en este template el único puerto público (`$PORT`) lo atiende `/app/server.py`,
que proxea todo al dashboard de Hermes **detrás de su cookie de admin**, y el listener
webhook del gateway vive en `127.0.0.1:8443` (no expuesto). Por eso
`POST https://<dominio>/telegram` devuelve 401 y el bot queda mudo si se activa el webhook.

## Qué hay acá

- `apply_telegram_route.py` — patcher idempotente que se corre en cada arranque del
  contenedor y le agrega a `/app/server.py`:
  1. ruta pública `POST /telegram` → `127.0.0.1:8443` (sin cookie; el gateway valida el
     `X-Telegram-Bot-Api-Secret-Token` por su cuenta),
  2. *wake*: si llega un POST con el gateway parado, lo arranca y espera el listener,
  3. *idle-stop*: para el gateway tras `TELEGRAM_IDLE_STOP_S` (900 s) sin webhooks. Recién
     ahí el contenedor deja de emitir paquetes y Railway puede dormirlo.
  Mantiene el invariante en ambos sentidos: parche aplicado ⇒ saca el override vacío de
  `.env` (modo webhook); parche fallado o ausente ⇒ escribe `TELEGRAM_WEBHOOK_URL=` vacío
  (vuelve a polling: bot funcionando, contenedor que no duerme).
- `boot.sh` — wrapper de arranque (lo invoca el start command de Railway). Corre el patcher
  y después `exec /app/start.sh`. Nunca frena el arranque.
- `selftest_route.py` — prueba funcional de la ruta inyectada (ASGI en proceso + listener
  falso en 8443). Correr: `cd /app && HERMES_HOME=/tmp/x python3 /data/.hermes/patch/selftest_route.py /app/<copia-patcheada>`.
- `server.py.orig` — copia pristina del server.py de la imagen (se guarda la primera vez).
- `last_run.json` — resultado de la última corrida del patcher.

## Cambio requerido en Railway (una línea, en un fork)

`railway.toml` de este template fija `startCommand`, y **la config en código siempre pisa
la del dashboard**, así que no alcanza con editarlo en la UI:

```toml
[deploy]
startCommand = "/usr/bin/tini -g -- /usr/bin/bash /data/.hermes/patch/boot.sh"
```

(pasamos por `/usr/bin/bash` para no depender del bit de ejecución del volumen).

### Ojo: Railway puede deployar el commit nuevo y seguir usando el start command viejo

Verificado el 2026-09-21: el servicio deployó el commit del fork cuyo `railway.toml` ya
apuntaba a `boot.sh`, y sin embargo el contenedor arrancó con
`/usr/bin/tini -g -- /app/start.sh` (`/proc/1/cmdline`) — la config anterior. Síntoma:
`grep -c hermes-telegram-webhook-route /app/server.py` = 0, no hay `last_run.json`, no hay
entrada nueva en `postdeploy_check.log`, `POST /telegram` = 401, y el gateway queda en
`polling` (nunca duerme). Diagnóstico de una línea:

```bash
tr '\0' ' ' < /proc/1/cmdline    # debe decir boot.sh, no /app/start.sh
```

En la UI de Railway, el deploy actual → *Details* muestra el start command efectivo y si
vino del archivo de config (ícono) o del panel; si no viene del archivo, se arregla en
Settings → Deploy → *Custom Start Command*.

## Plan B sin depender de Railway: `boot_hook.py` + stub en el user-site

`boot_hook.py` hace lo mismo que `boot.sh` pero se dispara desde el arranque del intérprete
Python, no desde el start command. `site.py` importa `usercustomize` desde el *user site* al
iniciar cualquier proceso Python, y con `HOME=/data` ese directorio es
`/data/.local/lib/python3.12/site-packages` — o sea, en el volumen y persistente. Ahí vive un
stub de 15 líneas que ejecuta `boot_hook.py`, que a su vez corre el patcher **sólo cuando el
proceso es `python /app/server.py`** (`sys.argv[0]`), antes de que CPython lea ese archivo:
verificado que la reescritura llega a tiempo (un script con `ORIGINAL` corrió como `PATCHED`).

- Idempotente (el patcher detecta su marca) y fail-open: nunca levanta excepción (una
  excepción en `usercustomize` afectaría a *todos* los procesos Python del contenedor).
- Si el patcher falla o la marca no está, fuerza polling (`TELEGRAM_WEBHOOK_URL=` vacío).
- Dispara `postdeploy_check.py --delay 150`, así el estado final llega por **Telegram**
  ~2,5 min después del arranque, sin depender de una sesión TUI.
- Log: `patch/boot_hook.log`. Off switch: `HERMES_BOOT_HOOK_OFF=1`.
- El directorio del stub lleva la versión de Python: si se sube `HERMES_REF` a una imagen con
  otro Python (3.13+), copiar el stub al `site-packages` de esa versión (hay copias en 3.11 y
  3.13 por las dudas).

Con el hook instalado, la línea de `railway.toml` es opcional (si `boot.sh` corre, el patcher
dice "already patched" y no pasa nada).

## Rollback

Volver `railway.toml` al valor original (`/usr/bin/tini -g -- /app/start.sh`) y redeploy.
Opcional: dejar `TELEGRAM_WEBHOOK_URL=` vacío en `.env` para forzar polling.

## Verificación

1. `POST https://<dominio>/telegram` → NO debe ser 401 (con el parche, el gateway responde).
2. `getWebhookInfo` → `url` = `https://<dominio>/telegram`, `pending_update_count` → 0.
3. Sin mensajes 15+ min → en logs: `[telegram] idle ... stopping the gateway`; el panel
   muestra el gateway en `stopped`.
4. Otros ~10 min después → Railway lo duerme. Mandar un mensaje → responde en ~30 s
   (cold boot; el primer POST puede recibir 502 y Telegram reintenta).

## Perillas

`TELEGRAM_IDLE_STOP_S` (900), `TELEGRAM_WAKE_WAIT_S` (45), `TELEGRAM_WEBHOOK_PORT` (8443).
Nota: con el gateway parado no corren cron jobs ni el dispatcher de kanban.
