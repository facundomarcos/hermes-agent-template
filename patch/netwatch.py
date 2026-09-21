#!/usr/bin/env python3
"""Sample established outbound TCP connections for ~13 min (diagnostics only).

Writes one line per sample to /data/.hermes/patch/netwatch.log.
Pure /proc reading: emits no packets itself, so it cannot falsify the measurement.
"""
import datetime
import time

OUT = "/data/.hermes/patch/netwatch.log"
ZERO = "00000000"
LOOP4 = "0100007F"          # 127.0.0.1
LOOP6 = "00000000000000000000000001000000"  # ::1


def established():
    rows = []
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path) as fh:
                next(fh)
                for line in fh:
                    p = line.split()
                    if len(p) < 4 or p[3] != "01":   # 01 = ESTABLISHED
                        continue
                    rem_hex, rem_port_hex = p[2].rsplit(":", 1)
                    if rem_hex in (ZERO, LOOP4, LOOP6):
                        continue
                    rows.append((path.split("/")[-1], rem_hex[:16], int(rem_port_hex, 16)))
        except Exception:
            pass
    return rows


end = time.time() + 13 * 60
with open(OUT, "a") as fh:
    fh.write("--- netwatch start %s ---\n" % datetime.datetime.utcnow().isoformat())
while time.time() < end:
    c = established()
    with open(OUT, "a") as fh:
        fh.write("%s n_out=%d %s\n" % (datetime.datetime.utcnow().isoformat(), len(c), c[:8]))
    time.sleep(20)
with open(OUT, "a") as fh:
    fh.write("--- netwatch end %s ---\n" % datetime.datetime.utcnow().isoformat())
