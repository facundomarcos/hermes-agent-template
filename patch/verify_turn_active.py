#!/usr/bin/env python3
"""Verify the fixed patcher: build, compile, and unit-test _telegram_turn_active.

Imports the patcher module, rebuilds a patched server.py from the pristine
server.py.orig, compiles it, then extracts the NEW _telegram_turn_active() source
and exercises it against synthetic agent.log tails.
"""
import importlib.util
import os
import pathlib
import py_compile
import tempfile
import time

spec = importlib.util.spec_from_file_location("patcher", "/data/.hermes/patch/apply_telegram_route.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

src = pathlib.Path(m.BACKUP).read_text(encoding="utf-8")
out = m.build_patched(src)
pathlib.Path("/tmp/srv_built.py").write_text(out, encoding="utf-8")
py_compile.compile("/tmp/srv_built.py", cfile=tempfile.mktemp(), doraise=True)
print("BUILD OK — patched server.py compiles (%d bytes)" % len(out))
print("fix present in build:", "Do NOT trust agent.log mtime alone" in out)
print("/telegram route in build:", 'Route("/telegram"' in out or "'/telegram'" in out or "/telegram" in out)

start = out.index("def _telegram_turn_active")
end = out.index("async def _telegram_start_gateway")
fn_src = out[start:end]

ns = {"time": time, "os": os, "Path": pathlib.Path, "HERMES_HOME": "/tmp/tw_test"}
exec(compile(fn_src, "<turn_active>", "exec"), ns)
fn = ns["_telegram_turn_active"]

d = pathlib.Path("/tmp/tw_test/logs")
d.mkdir(parents=True, exist_ok=True)
af = d / "agent.log"


def run(tail, age=5):
    af.write_text(tail, encoding="utf-8")
    t = time.time() - age
    os.utime(af, (t, t))
    return fn()


hk = "\n".join(
    "2026-09-21 21:%02d:01,000 INFO hermes_cli.mem_trim: memory trim: "
    "reason=messaging gateway housekeeping malloc_trim=1 rss_kib=229028" % i
    for i in range(30)
)
reap = ("\n2026-09-21 21:02:57,045 INFO hermes_cli.mem_trim: memory trim: "
        "reason=idle reaper periodic trim malloc_trim=1")
cleanup = ("\n2026-09-21 21:04:45,488 INFO tools.terminal_tool: Cleaned up inactive "
           "environment for task: session:x")
turn = ("\n2026-09-21 21:30:00,000 INFO [20260921_x] agent.conversation_loop: "
        "API call #1: model=deepseek-flash provider=deepseek latency=8.2s")

cases = [
    ("solo housekeeping (idle real)   -> espero False", hk + reap + cleanup, 5, False),
    ("con línea de turno              -> espero True", hk + turn, 5, True),
    ("turno viejo (age=300)           -> espero False", hk + turn, 300, False),
    ("archivo vacío                   -> espero False", "", 5, False),
    ("solo housekeeping, age=300      -> espero False", hk, 300, False),
]
bad = 0
for label, tail, age, want in cases:
    got = run(tail, age)
    ok = (got == want)
    bad += (not ok)
    print(("PASS " if ok else "FAIL ") + label + "  got=%s" % got)
print("RESULT:", "ALL PASS" if bad == 0 else "%d FAILURES" % bad)
