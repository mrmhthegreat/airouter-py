"""End-to-end test of the router against a mock OpenAI-compatible server.

Spins up an in-process HTTP server that returns a canned chat completion,
boots a Router with a MemoryStore, adds a provider pointing at the mock,
and calls generate(). Verifies that the text, tokens, and provider name
round-trip correctly.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class _MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *a, **kw):
        pass

    def do_POST(self) -> None:
        if self.path == "/chat/completions":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8", "replace")
            try:
                payload = json.loads(body)
            except ValueError:
                payload = {}
            content = f"Hello from mock! You said: {(payload.get('messages') or [{}])[-1].get('content', '')[:40]}"
            resp = {
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"total_tokens": 42},
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
    # Start a mock OpenAI-compatible server on a random port.
    srv = HTTPServer(("127.0.0.1", 0), _MockHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import airouter
    from airouter import ProviderConfig, Request

    c = airouter.new_client(airouter.MemoryStore(), airouter.Options())
    c.save_provider(ProviderConfig(
        name="mock-gpt",
        kind="openai",
        base_url=f"http://127.0.0.1:{port}",
        api_key="sk_mock",
        model="mock-model",
        priority=10,
        workers=1,
    ))

    resp = c.generate(Request(prompt="Hello, world!"))
    print("text:", resp.text)
    print("provider:", resp.provider)
    print("model:", resp.model)
    print("tokens:", resp.tokens)
    print("latency_ms:", resp.latency_ms)
    print("attempts:", resp.attempts)

    assert resp.text.startswith("Hello from mock!"), f"unexpected text: {resp.text}"
    assert resp.provider == "mock-gpt"
    assert resp.model == "mock-model"
    assert resp.tokens == 42
    assert resp.attempts == []

    # Verify usage was recorded.
    rep = c.usage("default")
    print()
    print("usage report:")
    print("  requests_today:", rep.requests_today)
    print("  tokens_today:", rep.tokens_today)
    print("  providers:")
    for u in rep.providers:
        print(f"    {u.provider}: {u.tenant_requests} req, {u.tenant_tokens} tok, all={u.all_requests}")
    assert rep.requests_today == 1
    assert rep.tokens_today == 42

    # Verify status reflects the call.
    print()
    print("status:")
    for s in c.status():
        print(f"  #{s.id} {s.name:30s} state={s.state:10s} used_today={s.used_today} tokens_today={s.tokens_today}")

    srv.shutdown()
    print()
    print("OK: end-to-end generate works")
    return 0


if __name__ == "__main__":
    sys.exit(main())
