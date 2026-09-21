#!/usr/bin/env python3
"""Boot hook (lives on the volume): patch /app/server.py before the admin server is read.

Why it exists
-------------
The deploy shape is supposed to run /data/.hermes/patch/boot.sh as the service start command
(railway.toml on the fork). Verified failure mode: Railway deployed a commit whose railway.toml
points at boot.sh, but the container still booted with the *previous* start command
(`/usr/bin/tini -g -- /app/start.sh`, seen in /proc/1/cmdline), so boot.sh never ran, the route
and the idle-stop never got injected, the gateway stayed in Telegram polling mode and the
service could never sleep (polling = outbound getUpdates every ~10 s).

This hook does not depend on the start command at all: site.py imports `usercustomize` from the
user-site directory at interpreter startup, and with HOME=/data that directory is
/data/.local/lib/python3.12/site-packages — on the persistent volume. A stub there runs this
file for every python process; this module acts only for the admin server
(`python /app/server.py`) and only before CPython reads that script, so the patched source is
what runs. Verified empirically: a script whose text said ORIGINAL printed PATCHED.

Fail-open by contract
---------------------
Every failure path leaves /app/server.py untouched and never raises — an exception here would
break every python process in the container. The patcher it drives keeps
/data/.hermes/patch/server.py.orig, verifies the patched module imports and exposes /telegram,
and on failure forces Telegram polling (bot keeps answering, container just never sleeps).

Escape hatches (env)
--------------------
HERMES_BOOT_HOOK_OFF=1        disable the hook entirely.
HERMES_BOOT_HOOK_TARGET=path  patch another file (tests).
HERMES_BOOT_HOOK_NO_CHECK=1   do not spawn the post-deploy checker (tests).

Log: /data/.hermes/patch/boot_hook.log
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

PATCH_DIR = "/data/.hermes/patch"
PATCHER = os.path.join(PATCH_DIR, "apply_telegram_route.py")
CHECKER = os.path.join(PATCH_DIR, "postdeploy_check.py")
LOGF = os.path.join(PATCH_DIR, "boot_hook.log")
MARKER = "hermes-telegram-webhook-route"
ADMIN_ARGV0 = "/app/server.py"

TARGET = os.environ.get("HERMES_BOOT_HOOK_TARGET") or ADMIN_ARGV0
ENV_FILE = os.path.join(os.environ.get("HERMES_HOME", "/data/.hermes"), ".env")


def _log(msg: str) -> None:
    try:
        os.makedirs(PATCH_DIR, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime())
        with open(LOGF, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} [boot_hook] {msg}\n")
    except Exception:
        pass


def _argv0() -> str:
    try:
        return ((sys.argv or [""])[0]) or ""
    except Exception:
        return ""


def _force_polling() -> str:
    """Degrade to polling: without the route, webhook mode would mute the bot."""
    try:
        try:
            with open(ENV_FILE, "r", encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except OSError:
            lines = []
        if any(ln.strip() == "TELEGRAM_WEBHOOK_URL=" for ln in lines):
            return "polling already forced"
        lines.append("TELEGRAM_WEBHOOK_URL=")
        with open(ENV_FILE, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return "polling forced"
    except Exception as e:
        return f"force-polling failed: {e!r}"


def run_patcher() -> int:
    """Drive the field-tested patcher through its own CLI (no shell, no argv surprises)."""
    import importlib.machinery
    import importlib.util

    loader = importlib.machinery.SourceFileLoader("hermes_apply_telegram_route", PATCHER)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("cannot load patcher")
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    saved = sys.argv
    sys.argv = ["apply_telegram_route.py", "--target", TARGET]
    try:
        return int(mod.main() or 0)
    finally:
        sys.argv = saved


def spawn_checker() -> bool:
    """Report the wiring to Telegram ~2.5 min after boot, independent of the start command."""
    if os.environ.get("HERMES_BOOT_HOOK_NO_CHECK") == "1" or not os.path.exists(CHECKER):
        return False
    out = open(os.path.join(PATCH_DIR, "postdeploy_stdout.log"), "a", encoding="utf-8")
    subprocess.Popen(
        [sys.executable, CHECKER, "--delay", "150"],
        stdout=out,
        stderr=subprocess.STDOUT,
        cwd="/app",
        start_new_session=True,
    )
    return True


def main() -> None:
    if os.environ.get("HERMES_BOOT_HOOK_OFF") == "1":
        return
    if _argv0() not in (ADMIN_ARGV0, TARGET):
        return  # gateway / dashboard / cron / anything else: hands off
    _log(f"fired: argv0={_argv0()!r} target={TARGET!r} pid={os.getpid()}")

    if not os.path.exists(PATCHER):
        _log("patcher missing -> " + _force_polling())
        return

    try:
        _log(f"patcher rc={run_patcher()}")
    except Exception as e:
        _log(f"patcher crashed: {e!r} -> " + _force_polling())

    # Own verification, independent of the patcher's own check.
    try:
        with open(TARGET, "r", encoding="utf-8") as fh:
            if MARKER not in fh.read():
                _log("VERIFY FAILED: marker missing -> " + _force_polling())
    except Exception as e:
        _log(f"verify read failed: {e!r} -> " + _force_polling())

    try:
        if spawn_checker():
            _log("post-deploy checker spawned")
    except Exception as e:
        _log(f"checker spawn failed: {e!r}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never raise: this runs inside every python startup
        _log(f"hook crashed: {e!r}")
