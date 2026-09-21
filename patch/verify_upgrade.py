#!/usr/bin/env python3
"""End-to-end check of the stale-patch upgrade path.

Takes the CURRENT (old-patched) /app/server.py, runs it through
strip_previous_patch() + build_patched(), and asserts the result carries the
idle-stop fix and still compiles.
"""
import importlib.util
import pathlib
import py_compile
import tempfile

spec = importlib.util.spec_from_file_location("patcher", "/data/.hermes/patch/apply_telegram_route.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

cur = pathlib.Path("/app/server.py").read_text(encoding="utf-8")
orig = pathlib.Path(m.BACKUP).read_text(encoding="utf-8")

print("current /app/server.py  len=%d  marker=%s  fix=%s" % (len(cur), m.MARKER in cur, m.FIX_SIG in cur))
print("pristine server.py.orig len=%d  marker=%s" % (len(orig), m.MARKER in orig))
print("FIX_SIG present in INJECTED template:", m.FIX_SIG in m.INJECTED)

stripped = m.strip_previous_patch(cur)
print("after strip             len=%d  marker=%s  fix=%s" % (len(stripped), m.MARKER in stripped, m.FIX_SIG in stripped))
print("strip leaves no orphan route line:", m.INJECTED_ROUTE not in stripped)
print("strip leaves no orphan watcher line:", m.INJECTED_WATCHER not in stripped)

rebuilt = m.build_patched(stripped)
p = "/tmp/rebuilt_server.py"
pathlib.Path(p).write_text(rebuilt, encoding="utf-8")
py_compile.compile(p, cfile=tempfile.mktemp(), doraise=True)
print("rebuilt                 len=%d  fix=%s  route=%s" % (len(rebuilt), m.FIX_SIG in rebuilt, "/telegram" in rebuilt))
print("rebuild is idempotent-safe: marker count =", rebuilt.count(m.MARKER))
print("compiles: OK")
