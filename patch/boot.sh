#!/bin/bash
# Railway start command (referenced from railway.toml in the fork):
#     startCommand = "/usr/bin/tini -g -- /data/.hermes/patch/boot.sh"
#
# Fail-open by design: nothing here may stop the container from booting. The patcher
# itself falls back to Telegram polling (bot keeps answering, container just never
# sleeps) when it cannot patch or verify /app/server.py.
set -u

PATCHER=/data/.hermes/patch/apply_telegram_route.py
ENVF="${HERMES_HOME:-/data/.hermes}/.env"

if [ -f "$PATCHER" ]; then
    python3 "$PATCHER" || echo "[boot] patcher exited non-zero — continuing anyway"
else
    echo "[boot] telegram route patcher missing — forcing polling so the bot keeps working"
    grep -q '^TELEGRAM_WEBHOOK_URL=$' "$ENVF" 2>/dev/null || printf 'TELEGRAM_WEBHOOK_URL=\n' >> "$ENVF"
fi

# Post-deploy self-check, detached: waits 90s for the gateway to come up, then reports
# the wiring state to Telegram + patch/postdeploy_check.log. Never blocks the boot and
# never keeps the container awake (a handful of requests, then it exits).
CHECKER="${TELEGRAM_POSTDEPLOY_CHECK:-/data/.hermes/patch/postdeploy_check.py}"
if [ -f "$CHECKER" ]; then
    nohup python3 "$CHECKER" --delay 90 >>/data/.hermes/patch/postdeploy_stdout.log 2>&1 &
fi

exec /app/start.sh
