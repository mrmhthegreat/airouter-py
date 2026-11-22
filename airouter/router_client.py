"""Single upstream call — sends one request to one provider.

Python port of ``pkg/router/client.go``. Builds the right body shape for
OpenAI-compatible vs Ollama, applies JSON-mode and system-role quirks, and
turns non-2xx responses into :class:`~airouter.types.HTTPError`.
"""
from __future__ import annotations

import json as _json
from typing import Any, Optional

import requests

from .limiter import BadResponseError
from .ratelimit import parse_retry_after
from .types import HTTPError, Message, ProviderConfig, Request, truncate


def _fold_system(msgs: list[Message]) -> list[Message]:
    """Merge system messages into the first user message, for models that reject a dedicated system role."""
    sys: list[str] = []
    rest: list[Message] = []
    for m in msgs:
        if m.role == "system":
            sys.append(m.content)
        else:
            rest.append(m)
    if not sys:
        return msgs
    for m in rest:
        if m.role == "user":
            m.content = "\n\n".join(sys) + "\n\n" + m.content
            break
    return rest


def call_provider(r, p: ProviderConfig, msgs: list[Message], req: Request):
    """Send one request to one provider and return ``(text, total_tokens, headers, error)``.

    ``r`` is the :class:`~airouter.router.Router`; we use it for its session
    and ``attempt_timeout``.
    """
    # Gemma-style provider: no response_format, so ask for JSON in words.
    if req.json and p.kind != "ollama" and not p.use_json_mode():
        msgs = list(msgs)
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].role == "user":
                msgs[i].content += "\n\nReturn ONLY valid JSON. No markdown fences, no commentary."
                break
    if not p.use_system_role():
        msgs = _fold_system(msgs)

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if p.resolve_key():
        headers["Authorization"] = f"Bearer {p.resolve_key()}"

    if p.kind == "ollama":
        url = p.base_url + "/api/chat"
        opts: dict[str, Any] = {"num_ctx": 8192}
        if req.temperature is not None:
            opts["temperature"] = req.temperature
        if req.max_tokens > 0:
            opts["num_predict"] = req.max_tokens
        body: dict[str, Any] = {"model": p.model, "messages": [m.__dict__ for m in msgs],
                                "stream": False, "options": opts}
        if req.json:
            body["format"] = "json"
    else:
        url = p.base_url + "/chat/completions"
        body = {"model": p.model, "messages": [m.__dict__ for m in msgs]}
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.max_tokens > 0:
            body["max_tokens"] = req.max_tokens
        if req.json and p.use_json_mode():
            body["response_format"] = {"type": "json_object"}

    try:
        resp = r.session.post(url, json=body, headers=headers, timeout=r.attempt_timeout)
    except requests.RequestException as e:
        return "", 0, None, e

    resp_headers = dict(resp.headers)
    data = resp.content
    if resp.status_code // 100 != 2:
        ra = parse_retry_after(resp.headers.get("Retry-After", ""), r.now())
        return "", 0, resp_headers, HTTPError(
            code=resp.status_code,
            body=data.decode("utf-8", errors="replace"),
            retry_after=ra,
            headers=resp_headers,
        )

    try:
        payload = _json.loads(data)
    except ValueError as e:
        return "", 0, resp_headers, BadResponseError(f"bad response: {truncate(data.decode('utf-8', 'replace'), 160)}")

    if p.kind == "ollama":
        msg = (payload.get("message") or {}).get("content", "")
        tokens = int(payload.get("prompt_eval_count", 0)) + int(payload.get("eval_count", 0))
        return msg, tokens, resp_headers, None

    choices = payload.get("choices") or []
    if not choices:
        return "", 0, resp_headers, BadResponseError(f"bad response: {truncate(data.decode('utf-8', 'replace'), 160)}")
    content = (choices[0].get("message") or {}).get("content", "")
    usage = payload.get("usage") or {}
    tokens = int(usage.get("total_tokens", 0))
    return content, tokens, resp_headers, None


__all__ = ["call_provider"]
