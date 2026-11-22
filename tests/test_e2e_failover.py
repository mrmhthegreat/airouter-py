"""Test failover: first provider 500s, second provider succeeds.

Spins up two mock servers — one that always returns 500, one that returns
a canned chat completion. Adds both providers to a Router and calls
generate(); verifies the response came from the second provider and that
the first failure was recorded as an Attempt.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class _GoodHandler(BaseHTTPRequestHandler):
    def log_message(self, *a, **kw):
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        resp = {
            "choices": [{"message": {"role": "assistant", "content": "hello from good server"}}],
            "usage": {"total_tokens": 7},
        }
        data = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _BadHandler(BaseHTTPRequestHandler):
    def log_message(self, *a, **kw):
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        body = json.dumps({"error": {"message": "internal failure"}}).encode("utf-8")
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    bad_srv = HTTPServer(("127.0.0.1", 0), _BadHandler)
    bad_port = bad_srv.server_address[1]
    threading.Thread(target=bad_srv.serve_forever, daemon=True).start()

    good_srv = HTTPServer(("127.0.0.1", 0), _GoodHandler)
    good_port = good_srv.server_address[1]
    threading.Thread(target=good_srv.serve_forever, daemon=True).start()

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import airouter
    from airouter import ProviderConfig, Request

    c = airouter.new_client(airouter.MemoryStore(), airouter.Options())
    # Bad provider has lower priority so it's tried first.
    c.save_provider(ProviderConfig(
        name="bad-provider", kind="openai", base_url=f"http://127.0.0.1:{bad_port}",
        api_key="sk_bad", model="m", priority=10, workers=1,
    ))
    c.save_provider(ProviderConfig(
        name="good-provider", kind="openai", base_url=f"http://127.0.0.1:{good_port}",
        api_key="sk_good", model="m", priority=20, workers=1,
    ))

    resp = c.generate(Request(prompt="hi"))
    print("text:", resp.text)
    print("provider:", resp.provider)
    print("attempts:", resp.attempts)
    assert resp.text == "hello from good server"
    assert resp.provider == "good-provider"
    assert len(resp.attempts) == 1
    assert resp.attempts[0].provider == "bad-provider"
    assert resp.attempts[0].status == 500

    print()
    print("OK: failover works (bad 500 -> good 200)")

    bad_srv.shutdown()
    good_srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
