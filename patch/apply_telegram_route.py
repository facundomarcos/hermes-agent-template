#!/usr/bin/env python3
"""Boot-time patcher: give the Railway template a public Telegram webhook route.

Why this exists
---------------
Telegram talks to Hermes in one of two ways:

* polling  -> `getUpdates` every ~10s: continuous OUTBOUND packets, so Railway's
  serverless (sleep after ~5-10 min without outbound) can never stop the container.
* webhook  -> Telegram POSTs updates; the container goes quiet when idle and Railway
  can sleep it. But this template's only public port ($PORT) is served by
  `/app/server.py`, which proxies everything to the Hermes dashboard behind its own
  admin cookie gate -- and the gateway's Telegram webhook listener sits on
  127.0.0.1:8443, which is not exposed. So `POST https://<domain>/telegram` returns
  401 and the bot goes mute.

This patcher adds, idempotently, to /app/server.py:

1. an unguarded `POST /telegram` route that forwards the raw body + Telegram's
   secret header to 127.0.0.1:8443 (the gateway validates the secret itself);
2. a wake path: if that POST arrives while the gateway is down, start it and wait
   for the listener before forwarding (Telegram tolerates ~60s; a 10-30s wait is fine);
3. an idle-stop watcher: stop the gateway after TELEGRAM_IDLE_STOP_S without webhook
   traffic, which is what finally lets the container stop sending packets and sleep.

Invariants it maintains (both directions):

* route present  -> the empty `TELEGRAM_WEBHOOK_URL=` override in .env is REMOVED, so
  the Railway variable takes effect and the adapter runs in webhook mode;
* patch failed / route missing -> `TELEGRAM_WEBHOOK_URL=` (empty) is written to .env so
  the adapter falls back to POLLING. A sleeping container is better than a mute bot.

The script never exits non-zero and never leaves a broken server.py: the wrapper in
railway.toml runs it before `exec /app/start.sh`, and a failure here must not stop the
container from booting.
"""

from __future__ import annotations

import argparse
import json
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
import time
import hashlib
from pathlib import Path

MARKER = "# ── hermes-telegram-webhook-route (patched) ─────────────────────────────────"
DEFAULT_TARGET = "/app/server.py"
PATCH_DIR = Path(os.environ.get("TELEGRAM_PATCH_DIR", "/data/.hermes/patch"))
BACKUP = PATCH_DIR / "server.py.orig"
STATE = PATCH_DIR / "last_run.json"


def log(msg: str) -> None:
    print(f"[patch] {msg}", flush=True)


# ── the code we inject ────────────────────────────────────────────────────────
# NOTE: plain str.replace, never .format() — this block is full of braces.
_MARKER_TOKEN = "__PATCH_MARKER__"

# The two insertions that live OUTSIDE the handler block, kept as constants so
# strip_previous_patch() removes exactly what build_patched() adds.
INJECTED_ROUTE = (
    '    # Public Telegram webhook — see the injected block above (no cookie gate).\n'
    '    Route("/telegram",                         route_telegram_webhook, methods=["POST"]),\n'
)
INJECTED_WATCHER = "    asyncio.create_task(idle_stop_watcher())\n"

# Signature of the CURRENT injected block. A server.py that carries the route
# marker but NOT this string is running a STALE injected block and must be
# rebuilt — see strip_previous_patch(). (The prebuilt image ships a patched
# server.py, so this is the normal path on a fresh container, not an edge case.)
FIX_SIG = "Do NOT trust agent.log mtime alone"

INJECTED = '''__PATCH_MARKER__
# Injected by /data/.hermes/patch/apply_telegram_route.py at container boot.
# Telegram POSTs https://<domain>/telegram -> gateway webhook listener (127.0.0.1:8443).
# The listener validates X-Telegram-Bot-Api-Secret-Token itself, so this route is
# deliberately NOT behind the admin cookie gate (Telegram has no cookie).
TELEGRAM_WEBHOOK_PATH = "/telegram"
TELEGRAM_WEBHOOK_PORT = int(os.environ.get("TELEGRAM_WEBHOOK_PORT", "8443"))
TELEGRAM_WEBHOOK_TARGET = f"http://127.0.0.1:{TELEGRAM_WEBHOOK_PORT}"
TELEGRAM_IDLE_STOP_S = float(os.environ.get("TELEGRAM_IDLE_STOP_S", "900"))
TELEGRAM_WAKE_WAIT_S = float(os.environ.get("TELEGRAM_WAKE_WAIT_S", "45"))

_last_telegram_activity = 0.0


def _telegram_listener_up() -> bool:
    import socket as _socket
    try:
        with _socket.socket() as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", TELEGRAM_WEBHOOK_PORT)) == 0
    except Exception:
        return False


def _telegram_turn_active() -> bool:
    """True while a turn is likely in flight.

    Do NOT trust agent.log mtime alone. hermes' gateway housekeeping appends a
    `hermes_cli.mem_trim: memory trim: reason=messaging gateway housekeeping`
    line to agent.log every ~60s even when nothing at all is happening, so a bare
    mtime check is ALWAYS "fresh" (within 120s) and the idle stop can never fire:
    the gateway keeps running, the container keeps emitting packets, and Railway
    never gets to sleep it. (Observed: gateway up 17+ min with zero turns and the
    idle stop never triggered.) Require a fresh mtime AND at least one recent
    non-housekeeping log line.
    Conservative on purpose: an active turn must never be killed by the idle stop.
    """
    noise = ("hermes_cli.mem_trim", "memory trim:", "Cleaned up inactive environment")
    p = Path(HERMES_HOME) / "logs" / "agent.log"
    try:
        if (time.time() - p.stat().st_mtime) >= 120:
            return False
        with open(p, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 16384))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return False
    recent = [ln for ln in tail.splitlines() if ln.strip()][-25:]
    for line in reversed(recent):
        if any(tok in line for tok in noise):
            continue
        return True
    return False


async def _telegram_start_gateway() -> None:
    global _last_telegram_activity
    if gw.state == "running":
        return
    log_line = "[telegram] inbound update with the gateway down — starting it"
    print(log_line, flush=True)
    try:
        await gw.start()
    except Exception as e:
        print(f"[telegram] gateway start failed: {e!r}", flush=True)
        return
    deadline = time.time() + TELEGRAM_WAKE_WAIT_S
    while time.time() < deadline:
        if _telegram_listener_up():
            print("[telegram] gateway webhook listener is up", flush=True)
            return
        await asyncio.sleep(0.5)
    print("[telegram] timed out waiting for the gateway listener", flush=True)


async def route_telegram_webhook(request: Request) -> Response:
    """Unguarded public ingress: forward Telegram updates to the gateway."""
    global _last_telegram_activity
    _last_telegram_activity = time.time()
    body = await request.body()
    await _telegram_start_gateway()
    headers = {"content-type": request.headers.get("content-type", "application/json")}
    secret = request.headers.get("x-telegram-bot-api-secret-token")
    if secret:
        headers["x-telegram-bot-api-secret-token"] = secret
    try:
        resp = await get_http_client().post(
            TELEGRAM_WEBHOOK_TARGET + TELEGRAM_WEBHOOK_PATH,
            content=body, headers=headers, timeout=httpx.Timeout(30.0, connect=5.0),
        )
        return Response(resp.content, status_code=resp.status_code,
                        media_type=resp.headers.get("content-type", "application/json"))
    except Exception as e:
        print(f"[telegram] forward failed: {e!r}", flush=True)
        return JSONResponse({"ok": False}, status_code=503)  # non-2xx => Telegram retries


async def idle_stop_watcher(interval: float = 60.0) -> None:
    """Stop the gateway after TELEGRAM_IDLE_STOP_S without Telegram traffic.

    Only then does the container stop emitting outbound packets, which is what lets
    Railway's serverless put it to sleep. Panel/TUI work does not need the gateway.
    """
    await asyncio.sleep(min(interval, 60.0))  # let startup settle
    while True:
        try:
            await asyncio.sleep(interval)
            if gw.state != "running":
                continue
            idle_s = time.time() - _last_telegram_activity
            if idle_s < TELEGRAM_IDLE_STOP_S or _telegram_turn_active():
                continue
            print(f"[telegram] idle {int(idle_s)}s — stopping the gateway so the "
                  f"platform can sleep", flush=True)
            await gw.stop()
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[telegram] idle watcher error: {e!r}", flush=True)
'''


def build_patched(src: str) -> str:
    injected = INJECTED.replace(_MARKER_TOKEN, MARKER)
    # 1. the handler block goes just above the route table
    anchor_a = "\nroutes = [\n"
    # 2. the route itself, in the public (unguarded) section
    anchor_b = '    Route("/health",                            route_health),\n'
    # 3. the idle watcher starts with the rest of the app lifespan
    anchor_c = "    asyncio.create_task(dash.start())\n    await auto_start()\n"

    for name, anchor in (("routes table", anchor_a), ("health route", anchor_b), ("lifespan", anchor_c)):
        if src.count(anchor) != 1:
            raise SystemExit(f"anchor not unique ({name}): {src.count(anchor)} matches")

    out = src.replace(anchor_a, injected + anchor_a, 1)
    out = out.replace(anchor_b, anchor_b + INJECTED_ROUTE, 1)
    out = out.replace(anchor_c, anchor_c + INJECTED_WATCHER, 1)
    return out


def verify_import(path: Path) -> str:
    """Import the patched module in a subprocess and confirm /telegram is routed."""
    code = (
        "import importlib.machinery, importlib.util, sys;"
        f"loader = importlib.machinery.SourceFileLoader('patched_server', {str(path)!r});"
        "spec = importlib.util.spec_from_loader(loader.name, loader);"
        "mod = importlib.util.module_from_spec(spec);"
        "loader.exec_module(mod);"
        "paths = [getattr(r, 'path', None) for r in mod.routes];"
        "sys.exit(0 if '/telegram' in paths else 3)"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd="/app",
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-6:])
        raise RuntimeError(f"route verification failed (rc={proc.returncode}): {tail}")
    return "ok"


# ── .env invariant ────────────────────────────────────────────────────────────
def env_path() -> Path:
    return Path(os.environ.get("HERMES_HOME", "/data/.hermes")) / ".env"


def set_webhook_override(enabled: bool) -> str:
    """Force polling (empty override) or let the Railway URL through (no override)."""
    path = env_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    kept = [ln for ln in lines if not ln.strip().startswith("TELEGRAM_WEBHOOK_URL=")]
    if not enabled:
        kept.append("TELEGRAM_WEBHOOK_URL=")
    if kept == lines:
        return "unchanged"
    try:
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    except OSError as e:
        return f"write failed: {e!r}"
    return "polling forced" if not enabled else "override removed (webhook mode)"


def fail_safe(reason: str) -> None:
    log(f"FAILED: {reason}")
    log(f"telegram mode: {set_webhook_override(False)} — the bot keeps working on polling")
    write_state({"status": "failed", "reason": reason, "ts": time.time()})


def write_state(state: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def strip_previous_patch(src: str) -> str:
    """Remove a previously injected block so the CURRENT INJECTED can replace it.

    The prebuilt image ships /app/server.py ALREADY carrying an older injected
    block, so `MARKER in src` is true on a fresh container. Without this, main()
    takes the "already-patched" early return and a fixed INJECTED can never reach
    the file: observed after a restart — /app/server.py still held the old
    _telegram_turn_active, the idle stop never fired, and the container never slept.
    """
    out = src
    idx = out.find(MARKER)
    if idx != -1:
        start = out.rfind("\n", 0, idx) + 1
        end = out.find("\nroutes = [\n", idx)
        if end != -1:
            out = out[:start] + out[end + 1:]
    out = out.replace(INJECTED_ROUTE, "", 1)
    out = out.replace(INJECTED_WATCHER, "", 1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=DEFAULT_TARGET)
    ap.add_argument("--no-failsafe", action="store_true", help="tests only")
    args = ap.parse_args()
    target = Path(args.target)

    try:
        src = target.read_text(encoding="utf-8")
    except OSError as e:
        if not args.no_failsafe:
            fail_safe(f"cannot read {target}: {e!r}")
        return 0

    if MARKER in src and FIX_SIG in src:
        log(f"already patched ({target}) — telegram mode: {set_webhook_override(True)}")
        write_state({"status": "already-patched", "ts": time.time(),
                     "sha256": hashlib.sha256(src.encode()).hexdigest()[:16]})
        return 0

    if MARKER in src:
        # Stale injected block (older patcher / prebuilt image): strip it so the
        # current INJECTED can be re-applied. Without this the fix never lands.
        log("stale injected block found — stripping it and rebuilding")
        src = strip_previous_patch(src)

    try:
        patched = build_patched(src)
    except SystemExit as e:
        if not args.no_failsafe:
            fail_safe(str(e))
        return 0

    # Keep one pristine copy of the image's server.py for diffing/rollback.
    try:
        PATCH_DIR.mkdir(parents=True, exist_ok=True)
        if not BACKUP.exists():
            shutil.copy2(target, BACKUP)
    except OSError as e:
        log(f"could not keep a backup: {e!r} (continuing)")

    tmp = Path(tempfile.mkstemp(dir=str(target.parent), prefix=".server_patched_", suffix=".py")[1])
    try:
        tmp.write_text(patched, encoding="utf-8")
        py_compile.compile(str(tmp), cfile=tempfile.mktemp(), doraise=True)
        check = verify_import(tmp)
        os.replace(tmp, target)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        try:
            tmp2 = Path(str(target) + ".broken")
            tmp2.write_text(patched, encoding="utf-8")
            log(f"patched copy kept at {tmp2} for inspection")
        except OSError:
            pass
        if not args.no_failsafe:
            fail_safe(f"patch or verification failed: {e!r}")
        else:
            raise
        return 0

    sha = hashlib.sha256(target.read_bytes()).hexdigest()[:16]
    log(f"patched {target} (sha {sha}, route check {check})")
    log(f"telegram mode: {set_webhook_override(True)}")
    write_state({"status": "patched", "ts": time.time(), "sha256": sha})
    return 0


if __name__ == "__main__":
    sys.exit(main())
