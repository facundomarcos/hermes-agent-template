# usercustomize stub (user-site on the persistent volume, HOME=/data).
#
# site.py imports this module at interpreter startup, BEFORE CPython reads the main script, so
# whatever the real hook does to /app/server.py lands in time for that same run (verified).
# All logic lives in /data/.hermes/patch/boot_hook.py so only one file has to be maintained;
# this stub is version-dir specific (python3.12 here), hence the copies for other versions.
import os as _os

_HOOK = "/data/.hermes/patch/boot_hook.py"
try:
    if _os.path.exists(_HOOK):
        _g = {"__name__": "hermes_boot_hook", "__file__": _HOOK}
        exec(compile(open(_HOOK, encoding="utf-8").read(), _HOOK, "exec"), _g)
        _g["main"]()
except Exception:
    pass  # never break interpreter startup
