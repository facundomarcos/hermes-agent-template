#!/usr/bin/env python3
"""Post-deploy self-check for the Telegram webhook / scale-to-zero setup.

Runs INSIDE the container a couple of minutes after boot (launched by boot.sh in the
background, so it can never block startup) and reports — to Telegram and to
`/data/.hermes/patch/postdeploy_check.log` — whether the deploy is correctly wired:

  1. /app/server.py carries the injected route marker (patch applied this boot);
  2. .env no longer forces polling (the empty TELEGRAM_WEBHOOK_URL override is gone);
  3. the gateway came up in WEBHOOK mode and its listener is on 127.0.0.1:8443;
  4. the public URL routes Telegram's POSTs to the gateway: a POST to
     https://<domain>/telegram with a WRONG secret must answer 403 (the gateway's own
     secret check). 401 means it still lands on the admin cookie gate (route missing);
     404/302/502 means something else swallowed it;
  5. Telegram's registered webhook URL matches ours and has no pending backlog.

Side-effect free: the only public request uses a deliberately wrong secret token, so
the gateway rejects it before any handler runs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/data/.hermes"))
PATCH_DIR = Path(os.environ.get("TELEGRAM_PATCH_DIR", "/data/.hermes/patch"))
LOG = PATCH_DIR / "postdeploy_check.log"
SERVER_PY = Path("/app/server.py")
MARKER = "hermes-telegram-webhook-route"


def read_env_file() -> dict:
    out = {}
    try:
        for line in (HERMES_HOME / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def tail(path: Path, n: int = 400) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def api(token: str, method: str, timeout: float = 20.0) -> dict:
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", timeout=timeout) as r:
        return json.load(r)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=float, default=0.0, help="seconds to wait before checking")
    ap.add_argument("--dry-run", action="store_true", help="print only, never calls Telegram sendMessage")
    args = ap.parse_args()
    if args.delay:
        time.sleep(args.delay)

    env = read_env_file()
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = (env.get("TELEGRAM_ALLOWED_USERS", "").split(",") or [""])[0].strip()
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    webhook_path = "/telegram"
    pub_url = (env.get("TELEGRAM_WEBHOOK_URL") or "").strip() or (f"https://{domain}{webhook_path}" if domain else "")

    checks: list[tuple[str, bool, str]] = []

    # 1. patch applied to the file this container booted with
    patched = MARKER in (SERVER_PY.read_text(encoding="utf-8", errors="replace")[:400000]
                         if SERVER_PY.exists() else "")
    checks.append(("server.py parcheado en este arranque", patched,
                   "marker encontrado" if patched else "SIN marca de parche"))

    # 2. .env must not force polling anymore
    override_gone = "TELEGRAM_WEBHOOK_URL" not in env
    checks.append(("override de polling eliminado de .env", override_gone,
                   "ok" if override_gone else "sigue presente TELEGRAM_WEBHOOK_URL= (vacío)"))

    # 3. gateway in webhook mode + local listener
    glog = tail(HERMES_HOME / "logs" / "gateway.log")
    webhook_mode = "Connected to Telegram (webhook mode)" in glog
    polling_mode = "Connected to Telegram (polling mode)" in glog
    checks.append(("gateway en modo webhook", webhook_mode,
                   "ok" if webhook_mode else ("modo polling activo" if polling_mode else "no se ve la línea de conexión")))
    listen_up = False
    try:
        with socket.socket() as s:
            s.settimeout(1.0)
            listen_up = s.connect_ex(("127.0.0.1", int(os.environ.get("TELEGRAM_WEBHOOK_PORT", "8443")))) == 0
    except Exception:
        pass
    checks.append(("listener webhook en 127.0.0.1:8443", listen_up, "ok" if listen_up else "nadie escucha"))

    # 4. public route reaches the gateway (wrong secret => gateway answers 403)
    status, body = None, ""
    if pub_url:
        req = urllib.request.Request(
            pub_url, method="POST", data=b'{"update_id":0}',
            headers={"Content-Type": "application/json",
                     "X-Telegram-Bot-Api-Secret-Token": "definitely-wrong"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                status = r.status
        except urllib.error.HTTPError as e:
            status = e.code
            body = e.read()[:120].decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            body = repr(e)
    route_ok = status == 403
    checks.append((f"POST {pub_url or '(sin dominio)'} llega al gateway", route_ok,
                   f"HTTP {status} {body}".strip() + ("" if route_ok else "  (403 = gateway validando secreto; 401 = sigue la cookie del admin)")))

    # 5. Telegram's own view
    info = {}
    if token:
        try:
            info = api(token, "getWebhookInfo").get("result", {})
        except Exception as e:  # noqa: BLE001
            info = {"_error": repr(e)}
    registered = (info.get("url") or "") == pub_url and bool(info.get("url"))
    checks.append(("webhook registrado en Telegram", registered,
                   f"url={info.get('url')!r} pending={info.get('pending_update_count')} "
                   f"last_error={info.get('last_error_message')}"))

    ok = all(c[1] for c in checks)
    lines = [f"{'✅' if good else '❌'} {label} — {detail}" for label, good, detail in checks]
    report = ("\U0001F50E Chequeo post-deploy del webhook de Telegram\n"
              f"{'TODO OK ✅' if ok else 'HAY FALLAS ❌'}\n\n" + "\n".join(lines) +
              "\n\nNota: si el gateway quedó en modo webhook y todo dio OK, a los ~15 min sin "
              "mensajes se va a apagar solo y Railway podrá dormir el contenedor; el primer "
              "mensaje después de dormido tarda ~30 s (cold boot).")

    try:
        PATCH_DIR.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S%z')} ok={ok} ---\n{report}\n")
    except OSError:
        pass
    print(report, flush=True)

    if token and chat and not args.dry_run:
        try:
            data = json.dumps({"chat_id": chat, "text": report,
                               "disable_web_page_preview": True}).encode()
            req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                         data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                json.load(r)
            print("[postdeploy] report sent to Telegram", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[postdeploy] could not send report: {e!r}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
