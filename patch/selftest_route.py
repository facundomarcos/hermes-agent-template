#!/usr/bin/env python3
"""Functional self-test for the injected Telegram webhook route.

Loads the PATCHED /app/server.py in-process over ASGI (no lifespan => no real gateway
or dashboard subprocesses) against a fake webhook listener on 127.0.0.1:8443, and
asserts the behaviour that matters for Railway scale-to-zero:

 1. `POST /telegram` is routed WITHOUT the admin cookie and reaches the listener,
    forwarding the raw body plus Telegram's X-Telegram-Bot-Api-Secret-Token header.
 2. The listener's status/body come back to the caller (Telegram sees the gateway's
    answer, not ours).
 3. Everything else stays cookie-gated: `POST /nope` -> 401, `GET /health` -> 200.
 4. Wake path: with the gateway "stopped", the route starts it and waits for the
    listener before forwarding.

Usage: python3 selftest_route.py /app/_patched_test.py
"""

from __future__ import annotations

import asyncio
import importlib.machinery
import importlib.util
import sys

SECRET = "test-secret-token"
PORT = 8443
received: list[dict] = []
start_calls = 0


def make_listener():
    async def handle(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = {}
            for ln in lines[1:]:
                if ":" in ln:
                    k, _, v = ln.partition(":")
                    headers[k.strip().lower()] = v.strip()
            n = int(headers.get("content-length", "0") or 0)
            body = await reader.readexactly(n) if n else b""
            received.append({"method": method, "path": path, "headers": headers, "body": body})
            if headers.get("x-telegram-bot-api-secret-token") != SECRET:
                payload, status = b'{"ok":false,"why":"bad secret"}', "403 Forbidden"
            else:
                payload, status = b'{"ok":true,"forwarded":true}', "200 OK"
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                + payload
            )
            await writer.drain()
        except Exception as e:  # noqa: BLE001
            print(f"[selftest] listener error: {e!r}", flush=True)
        finally:
            writer.close()

    return handle


def load_module(path: str):
    loader = importlib.machinery.SourceFileLoader("patched_server", path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


async def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "/app/_patched_test.py"
    mod = load_module(path)
    app = getattr(mod, "app", None) or getattr(mod, "application", None)
    assert app is not None, "no ASGI app found in the patched module"

    from httpx import ASGITransport, AsyncClient

    failures: list[str] = []

    def check(cond: bool, label: str) -> None:
        print(f"[selftest] {'PASS' if cond else 'FAIL'} — {label}", flush=True)
        if not cond:
            failures.append(label)

    server = await asyncio.start_server(make_listener(), "127.0.0.1", PORT)
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://local") as client:
            # ── 1/2. the webhook route is public and proxies verbatim ─────────
            mod.gw.state = "running"          # no wake needed: listener is up
            payload = b'{"update_id":1,"message":{"message_id":2}}'
            r = await client.post(
                "/telegram", content=payload,
                headers={"content-type": "application/json",
                         "X-Telegram-Bot-Api-Secret-Token": SECRET},
            )
            check(r.status_code == 200, f"POST /telegram -> 200 (got {r.status_code})")
            check(r.json().get("forwarded") is True, "gateway response body relayed back")
            check(bool(received), "request reached the 127.0.0.1:8443 listener")
            if received:
                req = received[-1]
                check(req["path"] == "/telegram", "forwarded path is /telegram")
                check(req["body"] == payload, "body forwarded byte-for-byte")
                check(req["headers"].get("x-telegram-bot-api-secret-token") == SECRET,
                      "secret header forwarded (gateway can validate it)")

            # a wrong/absent secret must NOT be rejected by us — the gateway decides
            r = await client.post("/telegram", content=b"{}",
                                  headers={"content-type": "application/json"})
            check(r.status_code == 403, f"no-secret request relayed the gateway's 403 (got {r.status_code})")

            # ── 3. the rest of the surface is still cookie-gated ──────────────
            r = await client.post("/nope", content=b"{}")
            check(r.status_code == 401, f"POST /nope without cookie -> 401 (got {r.status_code})")
            r = await client.get("/health")
            check(r.status_code == 200, f"GET /health -> 200 (got {r.status_code})")

            # ── 4. wake path: gateway down -> start it, wait, then forward ────
            global start_calls
            calls = {"n": 0}

            async def fake_start(*a, **k):
                calls["n"] += 1
                mod.gw.state = "running"

            mod.gw.state = "stopped"
            mod.gw.start = fake_start
            mod.TELEGRAM_WAKE_WAIT_S = 5.0
            mod.TELEGRAM_IDLE_STOP_S = 1.0
            r = await client.post(
                "/telegram", content=b'{"update_id":9}',
                headers={"content-type": "application/json",
                         "X-Telegram-Bot-Api-Secret-Token": SECRET},
            )
            start_calls = calls["n"]
            check(calls["n"] == 1, "gateway was started once on an inbound update")
            check(r.status_code == 200, f"forwarded after wake (got {r.status_code})")

            # ── idle-stop logic is wired: idle-stop predicate is time based ───
            mod._last_telegram_activity = mod.time.time() - 10_000
            check(mod._telegram_listener_up() is True, "listener probe detects 8443")
    finally:
        server.close()
        await server.wait_closed()
        try:
            client_mod = getattr(mod, "_http_client", None)
            if client_mod is not None:
                await client_mod.aclose()
        except Exception:  # noqa: BLE001
            pass

    print(f"[selftest] {'ALL PASS' if not failures else 'FAILURES: ' + '; '.join(failures)}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
