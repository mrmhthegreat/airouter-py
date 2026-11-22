"""Spin up the airouter HTTP server in-process and exercise every endpoint.

This is a single integrated smoke test that:

1. Boots a Router with a MemoryStore preloaded with a mock OpenAI-compatible
   provider that returns a canned chat completion.
2. Creates a tenant and gets an API key.
3. Starts the Starlette app on a random port using uvicorn in a thread.
4. Calls the native API, the OpenAI-compatible API, the whoami, usage,
   status, presets and free-pool endpoints, and a tenant /v1/me endpoint.
5. Tears the server down.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import uvicorn


class _MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *a, **kw):
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8", "replace")
        if self.path == "/chat/completions":
            try:
                payload = json.loads(body)
            except ValueError:
                payload = {}
            content = f"Mock answer to: {(payload.get('messages') or [{}])[-1].get('content', '')[:40]}"
            resp = {
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"total_tokens": 11},
            }
            self._send_json(200, resp)
            return
        self._send_json(404, {"error": "not found"})

    def _send_json(self, code: int, body) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    # Mock provider.
    mock = HTTPServer(("127.0.0.1", 0), _MockHandler)
    mock_port = mock.server_address[1]
    threading.Thread(target=mock.serve_forever, daemon=True).start()

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import airouter
    from airouter import ProviderConfig, Tenant
    from airouter.server import new_handler

    c = airouter.new_client(airouter.MemoryStore(), airouter.Options())
    c.save_provider(ProviderConfig(
        name="mock-gpt", kind="openai", base_url=f"http://127.0.0.1:{mock_port}",
        api_key="sk_mock", model="mock-model", priority=10, workers=1,
    ))
    admin_token = "test-admin-token-123"
    _, tenant_key = c.create_tenant(Tenant(id="acme", name="Acme", tz="UTC"))

    app = new_handler(c.router, admin_token)

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    # Find the bound port — uvicorn picks one when port=0.
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # Wait for server to start.
    for _ in range(30):
        # Inspect uvicorn's server state to find the listen port.
        for s in getattr(server, "servers", []) or []:
            sock = getattr(s, "sockets", None)
            if sock:
                port = sock[0].getsockname()[1]
                break
        else:
            port = None
        if port:
            break
        time.sleep(0.1)
    if not port:
        print("FAIL: server did not start", file=sys.stderr)
        return 1
    base = f"http://127.0.0.1:{port}"
    print(f"server listening at {base}")

    failures = []
    def check(name: str, ok: bool, detail: str = "") -> None:
        marker = "OK  " if ok else "FAIL"
        print(f"  {marker} {name}: {detail}")
        if not ok:
            failures.append(name)

    print("\n=== /healthz ===")
    r = requests.get(base + "/healthz")
    check("healthz", r.status_code == 200 and r.json()["status"] == "ok", r.text[:60])

    print("\n=== /v1/presets ===")
    r = requests.get(base + "/v1/presets")
    check("presets", r.status_code == 200 and "groq" in r.json(), f"{len(r.json())} presets")

    print("\n=== /v1/presets/free-pool ===")
    r = requests.get(base + "/v1/presets/free-pool?category=text")
    check("free-pool", r.status_code == 200 and len(r.json()["models"]) >= 10,
          f"{len(r.json()['models'])} models")

    print("\n=== /v1/whoami (tenant key) ===")
    r = requests.get(base + "/v1/whoami", headers={"Authorization": f"Bearer {tenant_key}"})
    check("whoami tenant", r.status_code == 200 and r.json()["role"] == "tenant",
          r.json().get("tenant", {}).get("id", ""))

    print("\n=== /v1/whoami (admin token) ===")
    r = requests.get(base + "/v1/whoami", headers={"Authorization": f"Bearer {admin_token}"})
    check("whoami admin", r.status_code == 200 and r.json()["role"] == "admin", r.text[:60])

    print("\n=== /v1/chat/completions (OpenAI-compatible) ===")
    r = requests.post(base + "/v1/chat/completions",
                      headers={"Authorization": f"Bearer {tenant_key}", "Content-Type": "application/json"},
                      json={"model": "auto", "messages": [{"role": "user", "content": "Hello"}]})
    body = r.json()
    check("chat.completions",
          r.status_code == 200 and "Mock answer" in body["choices"][0]["message"]["content"],
          body["choices"][0]["message"]["content"][:60])
    check("chat.completions airouter metadata",
          "airouter" in body and body["airouter"]["provider"] == "mock-gpt",
          str(body.get("airouter")))

    print("\n=== /v1/models (OpenAI-compatible) ===")
    r = requests.get(base + "/v1/models", headers={"Authorization": f"Bearer {tenant_key}"})
    body = r.json()
    check("models", r.status_code == 200 and any(m["id"] == "auto" for m in body["data"]),
          f"{len(body['data'])} models")

    print("\n=== /v1/generate (native) ===")
    r = requests.post(base + "/v1/generate",
                      headers={"Authorization": f"Bearer {tenant_key}", "Content-Type": "application/json"},
                      json={"prompt": "Hi there"})
    body = r.json()
    check("generate", r.status_code == 200 and "Mock answer" in body["text"],
          body["text"][:60])
    check("generate headers",
          r.headers.get("X-AIRouter-Provider") == "mock-gpt",
          r.headers.get("X-AIRouter-Provider", ""))

    print("\n=== /v1/usage ===")
    r = requests.get(base + "/v1/usage", headers={"Authorization": f"Bearer {tenant_key}"})
    body = r.json()
    check("usage", r.status_code == 200 and body["usage"]["requests_today"] >= 1,
          f"{body['usage']['requests_today']} requests today")

    print("\n=== /v1/providers (admin) ===")
    r = requests.get(base + "/v1/providers", headers={"Authorization": f"Bearer {admin_token}"})
    check("admin list providers", r.status_code == 200 and len(r.json()) >= 1,
          f"{len(r.json())} providers")

    print("\n=== /v1/providers (without admin token → 401) ===")
    r = requests.get(base + "/v1/providers", headers={"Authorization": f"Bearer {tenant_key}"})
    check("admin endpoints protected", r.status_code == 401, f"HTTP {r.status_code}")

    print("\n=== /v1/bootstrap (admin) ===")
    r = requests.post(base + "/v1/bootstrap?category=text", headers={"Authorization": f"Bearer {admin_token}"})
    body = r.json()
    check("bootstrap", r.status_code == 200 and "added" in body and "skipped" in body,
          f"added={len(body['added'])}, skipped={len(body['skipped'])}")

    print("\n=== /v1/me (tenant self-service) ===")
    r = requests.get(base + "/v1/me", headers={"Authorization": f"Bearer {tenant_key}"})
    check("me", r.status_code == 200 and r.json()["tenant"]["id"] == "acme",
          r.json()["tenant"]["id"])

    print("\n=== /v1/chat/completions unauthorized (no key) ===")
    r = requests.post(base + "/v1/chat/completions", json={"model": "auto", "messages": [{"role": "user", "content": "Hi"}]})
    check("unauthorized", r.status_code == 401, f"HTTP {r.status_code}")

    print("\n=== / (web UI) ===")
    r = requests.get(base + "/")
    check("web ui", r.status_code == 200 and "<html" in r.text.lower(), f"{len(r.text)} bytes")

    server.should_exit = True
    thread.join(timeout=2)
    mock.shutdown()

    if failures:
        print(f"\nFAIL: {len(failures)} check(s) failed: {failures}", file=sys.stderr)
        return 1
    print("\nOK: all server smoke tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
