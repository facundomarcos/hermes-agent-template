#!/usr/bin/bash
# Image-side bootstrap for the Telegram webhook / scale-to-zero patch system.
#
# Why this exists
# ---------------
# Railway mounts the persistent volume at /data, which HIDES whatever the image
# has at /data/.hermes/patch — so the patch sources cannot simply be COPYed to
# their runtime path. They ship in the image at /opt/hermes-patch (see the
# Dockerfile) and this script seeds the volume from the image on every boot.
#
# That makes THIS REPOSITORY the source of truth: a redeploy ships a new patcher,
# and a wiped volume re-bootstraps itself instead of leaving the service unable to
# start at all — railway.toml's startCommand points at
# /data/.hermes/patch/boot.sh, a path that nothing else creates.
#
# Status: NOT WIRED. To activate, railway.toml's [deploy] startCommand becomes:
#     startCommand = "/usr/bin/tini -g -- /usr/bin/bash /opt/hermes-patch/entry.sh"
set -u

SRC=/opt/hermes-patch
DST="${HERMES_HOME:-/data/.hermes}/patch"

mkdir -p "$DST" 2>/dev/null || true

# -u: copy only when the image's copy is newer (or the target is missing), so a
# redeploy wins while a plain restart on an older image leaves the volume alone.
# Never fatal: a missing file must not stop the container from booting.
for f in apply_telegram_route.py boot.sh boot_hook.py postdeploy_check.py \
         selftest_route.py verify_turn_active.py verify_upgrade.py; do
    [ -f "$SRC/$f" ] && cp -u "$SRC/$f" "$DST/$f" 2>/dev/null
done

# usercustomize stub: site.py imports it at interpreter startup from the user-site
# dir, which lives on the volume (HOME=/data). One copy per python version present.
for d in /data/.local/lib/python3.11/site-packages \
         /data/.local/lib/python3.12/site-packages \
         /data/.local/lib/python3.13/site-packages; do
    [ -f "$SRC/usercustomize.py" ] || break
    mkdir -p "$d" 2>/dev/null || continue
    cp -u "$SRC/usercustomize.py" "$d/usercustomize.py" 2>/dev/null
done

exec /usr/bin/bash "$DST/boot.sh"
