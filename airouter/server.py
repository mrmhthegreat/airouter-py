"""HTTP API for airouter.

Python port of ``pkg/server``. Uses FastAPI / Starlette. Exposes:

* Native API (richer: attempts, provider, latency):

  - ``POST /v1/generate`` — generate text (or JSON)
  - ``POST /v1/speak``    — text → audio (WAV bytes back, or JSON envelope)
  - ``POST /v1/transcribe`` — audio → text (multipart in, JSON out)
  - ``GET  /v1/usage``    — usage + live provider status
  - ``GET  /v1/whoami``   — who am I

* OpenAI-compatible surface (point any OpenAI SDK / editor plugin at
  ``http://HOST/v1`` with an airouter key and it just works):

  - ``POST /v1/chat/completions`` (stream=true is buffered streaming)
  - ``GET  /v1/models``
  - ``POST /v1/audio/speech``
  - ``POST /v1/audio/transcriptions``

* Tenant self-service at ``/v1/me/*``.
* Admin endpoints (``Authorization: Bearer <admin token>``).
* Public endpoints: ``GET /`` (web UI), ``GET /healthz``,
  ``GET /v1/presets``, ``GET /v1/presets/free-pool``.

Auth: tenant endpoints take ``Authorization: Bearer air_...``. Admin
endpoints take ``Authorization: Bearer <admin token>`` (set via
``AIROUTER_ADMIN_TOKEN``). If the admin token is empty, admin endpoints
return 403 — use the CLI instead.
"""
from __future__ import annotations

import base64
import json as _json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Optional

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from . import preset as _preset
from . import router as _router
from . import types as _types
from .router import Router
from .router_status import status as _status
from .router_tenantprov import ollama_models as _ollama_models
from .types import (
    CATEGORY_TEXT,
    CATEGORY_TTS,
    CATEGORY_STT,
    DEFAULT_TENANT_ID,
    AirouterError,
    AllFailedError,
    Attempt,
    BadRequestError,
    ErrBadRequest,
    ErrNoProviders,
    ErrNotFound,
    ErrTenantDisabled,
    ErrTimeout,
    ErrUnauthorized,
    ErrUnknownTenant,
    ExhaustedError,
    HTTPError,
    Message,
    ProviderConfig,
    Request,
    SpeakRequest,
    TenantLimitError,
    TranscribeRequest,
    truncate,
)

_HERE = Path(__file__).resolve().parent
_WEB_DIR = _HERE / "web"
_INDEX_HTML = _WEB_DIR / "index.html"


# ---------------------------------------------------------------------------
# Error mapping (single place that decides HTTP status codes)
# ---------------------------------------------------------------------------


def _map_err(err: Exception) -> tuple[int, str, dict[str, Any]]:
    """Map a router error to ``(HTTP status, OpenAI-style error type, extra fields)``."""
    if isinstance(err, ExhaustedError):
        return 503, "providers_busy", {
            "retry_after_seconds": int(err.retry_after) + 1,
            "reasons": err.reasons,
        }
    if isinstance(err, TenantLimitError):
        return 429, "rate_limit", {"retry_after_seconds": int(err.retry_after) + 1}
    if isinstance(err, BadRequestError):
        return 400, "bad_request", {"attempts": [a.__dict__ for a in err.attempts]}
    if isinstance(err, ErrBadRequest):
        return 400, "bad_request", {}
    if isinstance(err, AllFailedError):
        return 502, "upstream_error", {"attempts": [a.to_dict() for a in err.attempts]}
    if isinstance(err, ErrTimeout):
        return 504, "timeout", {}
    if isinstance(err, ErrNoProviders):
        return 503, "no_providers", {}
    if isinstance(err, (ErrUnauthorized, ErrUnknownTenant)):
        return 401, "unauthorized", {}
    if isinstance(err, ErrTenantDisabled):
        return 403, "forbidden", {}
    if isinstance(err, ErrNotFound):
        return 404, "not_found", {}
    return 500, "internal_error", {}


def _err_response(err: Exception, *, oai_shape: bool = False) -> JSONResponse:
    code, typ, extra = _map_err(err)
    headers = {}
    ra = extra.get("retry_after_seconds")
    if ra and ra > 0:
        headers["Retry-After"] = str(ra)
    if oai_shape:
        body: dict[str, Any] = {
            "error": {"message": str(err), "type": typ, "code": code, "param": None}
        }
        if extra:
            body["airouter"] = extra
    else:
        body = {"error": str(err)}
        body.update(extra)
    return JSONResponse(body, status_code=code, headers=headers)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bearer(req: StarletteRequest) -> str:
    h = req.headers.get("Authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return (req.headers.get("X-API-Key") or "").strip()


def _is_admin(router: Router, admin_token: str, req: StarletteRequest) -> bool:
    tok = _bearer(req)
    return bool(admin_token) and bool(tok) and secrets.compare_digest(tok, admin_token)


def _resolve_tenant(router: Router, admin_token: str, req: StarletteRequest):
    """Resolve the caller: a tenant key, or the admin token acting as X-Tenant-ID."""
    if _is_admin(router, admin_token, req):
        id = req.headers.get("X-Tenant-ID") or req.query_params.get("tenant") or DEFAULT_TENANT_ID
        t = router.store.tenant_by_id(id)
        if t is None:
            return None, _err_response(_types.ErrUnknownTenant())
        return t, None
    key = _bearer(req)
    if not key:
        return None, JSONResponse({"error": "missing api key (Authorization: Bearer <key>)"}, status_code=401)
    try:
        t = router.authenticate(key)
    except AirouterError as e:
        return None, _err_response(e)
    return t, None


# ---------------------------------------------------------------------------
# Native API handlers
# ---------------------------------------------------------------------------


async def _generate(req: StarletteRequest) -> Response:
    router: Router = req.app.state.router
    admin_token: str = req.app.state.admin_token
    t, err = _resolve_tenant(router, admin_token, req)
    if err:
        return err
    try:
        body = await req.json()
    except Exception as e:
        return JSONResponse({"error": f"invalid JSON body: {e}"}, status_code=400)
    rreq = Request(
        tenant_id=t.id,
        system=body.get("system", ""),
        prompt=body.get("prompt", ""),
        messages=[Message(role=m.get("role", "user"), content=m.get("content", ""))
                  for m in body.get("messages", [])],
        json=bool(body.get("json", False)),
        max_tokens=int(body.get("max_tokens", 0) or 0),
        temperature=body.get("temperature"),
        provider=body.get("provider", ""),
        strategy=body.get("strategy", ""),
        category=body.get("category", ""),
    )
    try:
        resp = router.generate(rreq)
    except AirouterError as e:
        return _err_response(e)
    fmt = req.query_params.get("format", "")
    accept = req.headers.get("Accept", "")
    if fmt == "text" or "text/plain" in accept:
        return PlainTextResponse(resp.text, headers={
            "X-AIRouter-Provider": resp.provider,
            "X-AIRouter-Model": resp.model,
        })
    return JSONResponse(resp.to_dict(), headers={
        "X-AIRouter-Provider": resp.provider,
        "X-AIRouter-Model": resp.model,
    })


async def _usage(req: StarletteRequest) -> Response:
    router: Router = req.app.state.router
    admin_token: str = req.app.state.admin_token
    t, err = _resolve_tenant(router, admin_token, req)
    if err:
        return err
    try:
        rep = router.usage(t.id, req.query_params.get("day", ""))
    except AirouterError as e:
        return _err_response(e)
    return JSONResponse({
        "usage": rep.to_dict(),
        "providers": [s.to_dict() for s in _status(router, t.id)],
        "server_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


async def _whoami(req: StarletteRequest) -> Response:
    router: Router = req.app.state.router
    admin_token: str = req.app.state.admin_token
    role = "tenant"
    if _is_admin(router, admin_token, req):
        role = "admin"
    t, err = _resolve_tenant(router, admin_token, req)
    if err:
        return err
    return JSONResponse({"role": role, "tenant": t.to_dict()})


# ---------------------------------------------------------------------------
# Speak / Transcribe
# ---------------------------------------------------------------------------


async def _speak(req: StarletteRequest) -> Response:
    router: Router = req.app.state.router
    admin_token: str = req.app.state.admin_token
    t, err = _resolve_tenant(router, admin_token, req)
    if err:
        return err
    try:
        body = await req.json()
    except Exception as e:
        return JSONResponse({"error": f"invalid JSON body: {e}"}, status_code=400)
    sreq = SpeakRequest(
        tenant_id=t.id,
        text=body.get("text", ""),
        voice=body.get("voice", ""),
        format=body.get("format", ""),
        language=body.get("language", ""),
        provider=body.get("provider", ""),
    )
    try:
        resp = router.speak(sreq)
    except (AirouterError, RuntimeError) as e:
        return _err_response(e)
    wants_json = req.query_params.get("format") == "json" or "application/json" in req.headers.get("Accept", "")
    if wants_json:
        out = resp.to_dict(include_audio_b64=True)
        out["audio_base64"] = base64.b64encode(resp.audio).decode("ascii")
        return JSONResponse(out, headers={
            "X-AIRouter-Provider": resp.provider,
            "X-AIRouter-Model": resp.model,
        })
    return Response(resp.audio, media_type=resp.content_type, headers={
        "X-AIRouter-Provider": resp.provider,
        "X-AIRouter-Model": resp.model,
        "X-AIRouter-Voice": resp.voice,
    })


async def _transcribe(req: StarletteRequest) -> Response:
    router: Router = req.app.state.router
    admin_token: str = req.app.state.admin_token
    t, err = _resolve_tenant(router, admin_token, req)
    if err:
        return err
    try:
        form = await req.form()
    except Exception as e:
        return JSONResponse({"error": f"invalid multipart form: {e}"}, status_code=400)
    upload = form.get("file")
    if upload is None:
        return JSONResponse({"error": "missing 'file' field in multipart form"}, status_code=400)
    audio = await upload.read()
    treq = TranscribeRequest(
        tenant_id=t.id,
        audio=audio,
        filename=upload.filename or "audio.bin",
        language=form.get("language", "") or "",
        prompt=form.get("prompt", "") or "",
        provider=form.get("provider", "") or "",
    )
    try:
        resp = router.transcribe(treq)
    except (AirouterError, RuntimeError) as e:
        return _err_response(e)
    fmt = req.query_params.get("format", "")
    accept = req.headers.get("Accept", "")
    if fmt == "text" or "text/plain" in accept:
        return PlainTextResponse(resp.text, headers={
            "X-AIRouter-Provider": resp.provider,
            "X-AIRouter-Model": resp.model,
        })
    return JSONResponse(resp.to_dict(), headers={
        "X-AIRouter-Provider": resp.provider,
        "X-AIRouter-Model": resp.model,
    })


# ---------------------------------------------------------------------------
# OpenAI-compatible API
# ---------------------------------------------------------------------------


def _flatten_content(raw: Any) -> str:
    if raw is None or raw == "null":
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        out = []
        for p in raw:
            if not isinstance(p, dict):
                continue
            if p.get("type") not in ("", "text"):
                raise ValueError(f"content part type {p.get('type')!r} is not supported (text only)")
            out.append(p.get("text", ""))
        return "".join(out)
    raise ValueError("message content must be a string or a list of text parts")


def _resolve_model(router: Router, tenant_id: str, model: str, category: str) -> tuple[str, bool]:
    """Map an OpenAI ``model`` string to a provider name to pin."""
    m = (model or "").strip().lower()
    if m in ("", "auto", "airouter", "default"):
        return "", True
    for st in _status(router, tenant_id):
        cat = st.category or CATEGORY_TEXT
        if cat == category and (st.name == model or st.model == model):
            return st.name, True
    return "", False


async def _chat_completions(req: StarletteRequest) -> Response:
    router: Router = req.app.state.router
    admin_token: str = req.app.state.admin_token
    t, err = _resolve_tenant(router, admin_token, req)
    if err:
        return err
    try:
        body = await req.json()
    except Exception as e:
        return _err_response(ValueError(f"invalid JSON body: {e}"), oai_shape=True)
    if body.get("tools") or body.get("functions"):
        return JSONResponse({
            "error": {"message": "tool / function calling is not supported by airouter yet; send plain chat messages",
                      "type": "bad_request", "code": 400, "param": None}
        }, status_code=400)
    messages: list[Message] = []
    try:
        for m in body.get("messages", []):
            text = _flatten_content(m.get("content"))
            role = m.get("role", "user")
            if role == "developer":
                role = "system"
            if role not in ("system", "user", "assistant"):
                return JSONResponse({
                    "error": {"message": f"message role {role!r} is not supported",
                              "type": "bad_request", "code": 400, "param": None}
                }, status_code=400)
            messages.append(Message(role=role, content=text))
    except ValueError as e:
        return JSONResponse({"error": {"message": str(e), "type": "bad_request", "code": 400, "param": None}},
                            status_code=400)
    response_format = body.get("response_format") or {}
    want_json = response_format.get("type") in ("json_object", "json_schema")
    rreq = Request(
        tenant_id=t.id,
        messages=messages,
        temperature=body.get("temperature"),
        max_tokens=int(body.get("max_tokens", 0) or body.get("max_completion_tokens", 0) or 0),
        json=want_json,
    )
    prov, found = _resolve_model(router, t.id, body.get("model", ""), CATEGORY_TEXT)
    if not found:
        return JSONResponse({
            "error": {"message": f"model {body.get('model')!r} not found; use \"auto\" or see GET /v1/models",
                      "type": "model_not_found", "code": 404, "param": None}
        }, status_code=404)
    rreq.provider = prov
    try:
        resp = router.generate(rreq)
    except AirouterError as e:
        return _err_response(e, oai_shape=True)
    content = resp.text
    if want_json and resp.json is not None:
        content = _json.dumps(resp.json)
    chat_id = "chatcmpl-air-" + secrets.token_hex(12)
    created = int(time.time())
    headers = {"X-AIRouter-Provider": resp.provider, "X-AIRouter-Model": resp.model}
    if not body.get("stream", False):
        return JSONResponse({
            "id": chat_id, "object": "chat.completion", "created": created, "model": resp.model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": resp.tokens},
            "airouter": {"provider": resp.provider, "latency_ms": resp.latency_ms, "attempts": [a.to_dict() for a in resp.attempts]},
        }, headers=headers)

    # Buffered streaming: airouter needs the whole answer to fail over safely,
    # so chunks are sent after it completes.
    async def stream():
        def chunk(delta: dict, finish):
            payload = {
                "id": chat_id, "object": "chat.completion.chunk", "created": created, "model": resp.model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return f"data: {_json.dumps(payload)}\n\n"
        yield chunk({"role": "assistant", "content": ""}, None)
        # Split into ~48-rune pieces.
        s = content
        while s:
            cut = min(48, len(s))
            yield chunk({"content": s[:cut]}, None)
            s = s[cut:]
        yield chunk({}, "stop")
        yield "data: [DONE]\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)


async def _models(req: StarletteRequest) -> Response:
    router: Router = req.app.state.router
    admin_token: str = req.app.state.admin_token
    t, err = _resolve_tenant(router, admin_token, req)
    if err:
        return err
    now = int(time.time())
    data: list[dict] = [{"id": "auto", "object": "model", "created": now, "owned_by": "airouter"}]
    seen = {"auto"}
    for st in _status(router, t.id):
        if (st.category and st.category != CATEGORY_TEXT) or st.model in seen:
            continue
        seen.add(st.model)
        owner = st.preset or st.kind
        data.append({"id": st.model, "object": "model", "created": now, "owned_by": owner})
    return JSONResponse({"object": "list", "data": data})


async def _audio_speech(req: StarletteRequest) -> Response:
    router: Router = req.app.state.router
    admin_token: str = req.app.state.admin_token
    t, err = _resolve_tenant(router, admin_token, req)
    if err:
        return err
    try:
        body = await req.json()
    except Exception as e:
        return _err_response(ValueError(f"invalid JSON body: {e}"), oai_shape=True)
    prov, found = _resolve_model(router, t.id, body.get("model", ""), CATEGORY_TTS)
    if not found:
        return JSONResponse({
            "error": {"message": f"tts model {body.get('model')!r} not found",
                      "type": "model_not_found", "code": 404, "param": None}
        }, status_code=404)
    sreq = SpeakRequest(
        tenant_id=t.id, text=body.get("input", ""), voice=body.get("voice", ""),
        format=body.get("response_format", ""), provider=prov,
    )
    try:
        resp = router.speak(sreq)
    except (AirouterError, RuntimeError) as e:
        return _err_response(e, oai_shape=True)
    return Response(resp.audio, media_type=resp.content_type, headers={
        "X-AIRouter-Provider": resp.provider,
        "X-AIRouter-Model": resp.model,
    })


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------


def _admin_only(handler):
    async def wrapper(req: StarletteRequest) -> Response:
        router: Router = req.app.state.router
        admin_token: str = req.app.state.admin_token
        if not admin_token:
            return JSONResponse({"error": "admin API disabled: start the server with AIROUTER_ADMIN_TOKEN set"},
                                status_code=403)
        if not _is_admin(router, admin_token, req):
            return JSONResponse({"error": "admin token required"}, status_code=401)
        return await handler(req)
    return wrapper


@_admin_only
async def _list_providers(req: StarletteRequest) -> Response:
    router: Router = req.app.state.router
    return JSONResponse([s.to_dict() for s in _status(router, "")])


@_admin_only
async def _save_provider(req: StarletteRequest) -> Response:
    router: Router = req.app.state.router
    try:
        body = await req.json()
    except Exception as e:
        return JSONResponse({"error": f"invalid JSON body: {e}"}, status_code=400)
    p = ProviderConfig(
        id=int(body.get("id", 0) or 0),
        name=body.get("name", ""),
        tenant_id=body.get("tenant_id", ""),
        preset=body.get("preset", ""),
        kind=body.get("kind", ""),
        base_url=body.get("base_url", ""),
        api_key_env=body.get("api_key_env", ""),
        api_key=body.get("api_key", ""),
        model=body.get("model", ""),
        priority=int(body.get("priority", 0) or 0),
        rpm=int(body.get("rpm", 0) or 0),
        rpd=int(body.get("rpd", 0) or 0),
        tpm=int(body.get("tpm", 0) or 0),
        ash=int(body.get("ash", 0) or 0),
        asd=int(body.get("asd", 0) or 0),
        workers=int(body.get("workers", 0) or 0),
        reset_tz=body.get("reset_tz", ""),
        json_mode=body.get("json_mode"),
        system_role=body.get("system_role"),
        category=body.get("category", ""),
        disabled=bool(body.get("disabled", False)),
        used_today_hint=int(body.get("used_today_hint", 0) or 0),
    )
    try:
        router.save_provider(p)
    except (AirouterError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(p.to_dict(include_key=False), status_code=201)


@_admin_only
async def _delete_provider(req: StarletteRequest) -> Response:
    router: Router = req.app.state.router
    try:
        id = int(req.path_params["id"])
    except (KeyError, ValueError):
        return JSONResponse({"error": "bad id"}, status_code=400)
    try:
        router.delete_provider(id)
    except AirouterError as e:
        return _err_response(e)
    return JSONResponse({"deleted": True})


@_admin_only
async def _toggle_provider(req: StarletteRequest) -> Response:
    router: Router = req.app.state.router
    try:
        id = int(req.path_params["id"])
    except (KeyError, ValueError):
        return JSONResponse({"error": "bad id"}, status_code=400)
    action = req.path_params.get("action", "")
    if action in ("enable", "disable"):
        try:
            router.set_disabled(id, action == "disable")
        except AirouterError as e:
            return _err_response(e)
        return JSONResponse({"id": id, "disabled": action == "disable"})
    if action == "key":
        try:
            body = await req.json()
        except Exception as e:
            return JSONResponse({"error": f"invalid JSON body: {e}"}, status_code=400)
        try:
            router.set_provider_key(id, body.get("api_key", ""))
        except (AirouterError, ValueError) as e:
            return _err_response(e)
        return JSONResponse({"id": id, "key_stored": True})
    if action == "reset":
        try:
            router.clear_pause(id)
        except AirouterError as e:
            return _err_response(e)
        return JSONResponse({"id": id, "paused": False})
    return JSONResponse({"error": "use /v1/providers/{id}/enable, /disable, /reset, or /key"}, status_code=400)


@_admin_only
async def _list_tenants(req: StarletteRequest) -> Response:
    router: Router = req.app.state.router
    return JSONResponse([t.to_dict() for t in router.store.list_tenants()])


@_admin_only
async def _create_tenant(req: StarletteRequest) -> Response:
    router: Router = req.app.state.router
    try:
        body = await req.json()
    except Exception as e:
        return JSONResponse({"error": f"invalid JSON body: {e}"}, status_code=400)
    from .types import Tenant
    t = Tenant(
        id=body.get("id", ""),
        name=body.get("name", ""),
        tz=body.get("tz", "UTC"),
        daily_request_limit=int(body.get("daily_request_limit", 0) or 0),
        disabled=bool(body.get("disabled", False)),
    )
    try:
        t, key = router.create_tenant(t)
    except (AirouterError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"tenant": t.to_dict(), "api_key": key,
                         "note": "store this key now — it is not shown again"}, status_code=201)


@_admin_only
async def _rotate_tenant_key(req: StarletteRequest) -> Response:
    router: Router = req.app.state.router
    tid = req.path_params.get("id", "")
    try:
        key = router.rotate_tenant_key(tid)
    except AirouterError as e:
        return _err_response(e)
    return JSONResponse({"tenant": tid, "api_key": key,
                         "note": "the old key no longer works; store this one now — it is not shown again"})


@_admin_only
async def _bootstrap(req: StarletteRequest) -> Response:
    router: Router = req.app.state.router
    overwrite = req.query_params.get("overwrite") in ("1", "true")
    only_preset = req.query_params.get("preset", "")
    category_param = req.query_params.get("category", "")
    if category_param == "":
        categories = None
    elif category_param == "all":
        categories = []
    else:
        categories = [c.strip() for c in category_param.split(",") if c.strip()]
        if "all" in categories:
            categories = []
    store_keys = req.query_params.get("store_keys") in ("1", "true")
    try:
        added, skipped = router.bootstrap_providers_opts(_router.BootstrapOptions(
            overwrite=overwrite, preset=only_preset, categories=categories, store_keys=store_keys,
        ))
    except (AirouterError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    label = "text (default)" if categories is None else ("all" if not categories else ",".join(categories))
    return JSONResponse({
        "added": added,
        "skipped": skipped,
        "verified": _preset.catalog_verified_on(),
        "categories_used": label,
        "available_categories": _preset.CATEGORIES,
        "summary": _preset.catalog_summary(),
        "next_step": "set the env vars named in skipped[] to import those providers too",
    })


# ---------------------------------------------------------------------------
# /v1/me/* — tenant self-service
# ---------------------------------------------------------------------------


def _me(handler):
    async def wrapper(req: StarletteRequest) -> Response:
        router: Router = req.app.state.router
        admin_token: str = req.app.state.admin_token
        t, err = _resolve_tenant(router, admin_token, req)
        if err:
            return err
        return await handler(req, t)
    return wrapper


@_me
async def _me_get(req: StarletteRequest, t) -> Response:
    return JSONResponse({"role": "tenant", "tenant": t.to_dict()})


@_me
async def _me_patch(req: StarletteRequest, t) -> Response:
    router: Router = req.app.state.router
    try:
        body = await req.json()
    except Exception as e:
        return JSONResponse({"error": f"invalid JSON body: {e}"}, status_code=400)
    try:
        u = router.update_tenant_profile(t.id, body.get("name", ""), body.get("tz", ""))
    except (AirouterError, ValueError) as e:
        return _err_response(e)
    return JSONResponse({"role": "tenant", "tenant": u.to_dict()})


@_me
async def _me_rotate_key(req: StarletteRequest, t) -> Response:
    router: Router = req.app.state.router
    try:
        key = router.rotate_tenant_key(t.id)
    except AirouterError as e:
        return _err_response(e)
    return JSONResponse({"tenant": t.id, "api_key": key,
                         "note": "the old key no longer works; store this one now, it is not shown again"})


@_me
async def _me_usage(req: StarletteRequest, t) -> Response:
    router: Router = req.app.state.router
    try:
        rep = router.usage(t.id, req.query_params.get("day", ""))
    except AirouterError as e:
        return _err_response(e)
    own, shared = router.list_tenant_providers(t.id)
    return JSONResponse({
        "tenant": t.id,
        "usage": rep.to_dict(),
        "own": [s.to_dict() for s in own],
        "shared": shared,
    })


@_me
async def _me_bootstrap(req: StarletteRequest, t) -> Response:
    router: Router = req.app.state.router
    try:
        body = await req.json()
    except Exception as e:
        return JSONResponse({"error": f"invalid JSON body: {e}"}, status_code=400)
    keys = body.get("keys", {}) or {}
    if not keys:
        return JSONResponse({"error": 'send your own keys: {"keys":{"GROQ_API_KEY":"gsk_..."}}'}, status_code=400)
    try:
        added, skipped = router.bootstrap_providers_opts(_router.BootstrapOptions(
            overwrite=body.get("overwrite", False), preset=body.get("preset", ""),
            tenant_id=t.id, keys=keys, no_env=True,
        ))
    except (AirouterError, ValueError) as e:
        return _err_response(e)
    return JSONResponse({"tenant": t.id, "added": added, "skipped": skipped})


@_me
async def _me_list(req: StarletteRequest, t) -> Response:
    router: Router = req.app.state.router
    own, shared = router.list_tenant_providers(t.id)
    return JSONResponse({"tenant": t.id, "own": [s.to_dict() for s in own], "shared": shared})


@_me
async def _me_add(req: StarletteRequest, t) -> Response:
    router: Router = req.app.state.router
    try:
        body = await req.json()
    except Exception as e:
        return JSONResponse({"error": f"invalid JSON body: {e}"}, status_code=400)
    p = ProviderConfig(
        name=body.get("name", ""),
        preset=body.get("preset", ""),
        kind=body.get("kind", ""),
        base_url=body.get("base_url", ""),
        api_key=body.get("api_key", ""),
        model=body.get("model", ""),
        priority=int(body.get("priority", 0) or 0),
        rpm=int(body.get("rpm", 0) or 0),
        rpd=int(body.get("rpd", 0) or 0),
        tpm=int(body.get("tpm", 0) or 0),
        workers=int(body.get("workers", 0) or 0),
        category=body.get("category", ""),
    )
    try:
        out = router.add_tenant_provider(t.id, p)
    except (AirouterError, ValueError) as e:
        return _err_response(e)
    return JSONResponse(out.to_dict(include_key=False), status_code=201)


@_me
async def _me_action(req: StarletteRequest, t) -> Response:
    router: Router = req.app.state.router
    try:
        id = int(req.path_params["id"])
    except (KeyError, ValueError):
        return JSONResponse({"error": "bad id"}, status_code=400)
    action = req.path_params.get("action", "")
    try:
        if action == "enable":
            router.set_tenant_provider_disabled(t.id, id, False)
        elif action == "disable":
            router.set_tenant_provider_disabled(t.id, id, True)
        elif action == "reset":
            router.reset_tenant_provider(t.id, id)
        elif action == "key":
            body = await req.json()
            router.set_tenant_provider_key(t.id, id, body.get("api_key", ""))
        elif action == "limits":
            body = await req.json()
            router.update_tenant_limits(t.id, id, _router.TenantLimits(
                rpm=body.get("rpm"), rpd=body.get("rpd"), tpm=body.get("tpm"),
                workers=body.get("workers"), priority=body.get("priority"),
            ))
        else:
            return JSONResponse({"error": "use enable, disable, reset, key or limits"}, status_code=404)
    except (AirouterError, ValueError) as e:
        return _err_response(e)
    return JSONResponse({"id": id, "ok": True})


@_me
async def _me_delete(req: StarletteRequest, t) -> Response:
    router: Router = req.app.state.router
    try:
        id = int(req.path_params["id"])
    except (KeyError, ValueError):
        return JSONResponse({"error": "bad id"}, status_code=400)
    try:
        router.delete_tenant_provider(t.id, id)
    except AirouterError as e:
        return _err_response(e)
    return JSONResponse({"deleted": True})


@_me
async def _me_catalog(req: StarletteRequest, t) -> Response:
    router: Router = req.app.state.router
    own, _ = router.list_tenant_providers(t.id)
    have = {f"{p.base_url.rstrip('/') }|{p.model}" for p in own}
    models: list[dict] = []
    provs: list[dict] = []
    seen: set[str] = set()
    for m in _preset.FREE_POOL:
        def_ = _preset.DEFINITIONS.get(m.preset)
        if def_ is None or def_.kind == "ollama":
            continue
        url = def_.base_url.rstrip("/")
        models.append({
            "preset": m.preset, "model": m.model, "category": m.category,
            "rpm": m.rpm, "rpd": m.rpd, "tpm": m.tpm,
            "notes": m.notes, "verified_on": m.verified_on,
            "added": f"{url}|{m.model}" in have,
        })
        if m.preset not in seen:
            seen.add(m.preset)
            provs.append({
                "preset": m.preset, "kind": def_.kind, "key_env": def_.api_key_env,
                "needs_key": def_.kind != "ollama",
            })
    provs.sort(key=lambda d: d["preset"])
    return JSONResponse({"providers": provs, "models": models, "verified_on": _preset.catalog_verified_on()})


@_me
async def _me_catalog_add(req: StarletteRequest, t) -> Response:
    router: Router = req.app.state.router
    try:
        body = await req.json()
    except Exception as e:
        return JSONResponse({"error": f"invalid JSON body: {e}"}, status_code=400)
    try:
        added, skipped, err = router.add_tenant_catalog(
            t.id, body.get("preset", ""), (body.get("api_key") or "").strip(), body.get("models", []) or [],
        )
    except (AirouterError, ValueError) as e:
        return _err_response(e)
    if err is not None:
        return _err_response(err)
    return JSONResponse({"added": added, "skipped": skipped})


@_me
async def _me_ollama(req: StarletteRequest, t) -> Response:
    router: Router = req.app.state.router
    try:
        ms = router.ollama_models()
    except Exception as e:
        return JSONResponse({"available": False, "error": str(e), "models": []})
    return JSONResponse({"available": True, "models": ms})


@_me
async def _me_ollama_add(req: StarletteRequest, t) -> Response:
    router: Router = req.app.state.router
    try:
        body = await req.json()
    except Exception as e:
        return JSONResponse({"error": f"invalid JSON body: {e}"}, status_code=400)
    added, skipped = router.add_tenant_ollama(t.id, body.get("models", []) or [])
    return JSONResponse({"added": added, "skipped": skipped})


@_me
async def _me_import(req: StarletteRequest, t) -> Response:
    router: Router = req.app.state.router
    try:
        body = await req.json()
    except Exception as e:
        return JSONResponse({"error": f"invalid JSON body: {e}"}, status_code=400)
    providers_in = body.get("providers") if isinstance(body, dict) else body
    if not providers_in:
        return JSONResponse({"error": 'expected {"providers":[ ... ]}'}, status_code=400)
    configs = []
    for p in providers_in:
        configs.append(ProviderConfig(
            name=p.get("name", ""), preset=p.get("preset", ""), kind=p.get("kind", ""),
            base_url=p.get("base_url", ""), api_key=p.get("api_key", ""), model=p.get("model", ""),
            priority=int(p.get("priority", 0) or 0), rpm=int(p.get("rpm", 0) or 0),
            rpd=int(p.get("rpd", 0) or 0), tpm=int(p.get("tpm", 0) or 0),
            workers=int(p.get("workers", 0) or 0), category=p.get("category", ""),
        ))
    added, failed = router.import_tenant_providers(t.id, configs)
    return JSONResponse({"added": added, "failed": failed})


@_me
async def _me_export(req: StarletteRequest, t) -> Response:
    router: Router = req.app.state.router
    out = [p.to_dict(include_key=False) for p in router.export_tenant_providers(t.id)]
    return JSONResponse({"providers": out}, headers={
        "Content-Disposition": f'attachment; filename="airouter-providers-{t.id}.json"',
    })


# ---------------------------------------------------------------------------
# Public endpoints
# ---------------------------------------------------------------------------


async def _healthz(req: StarletteRequest) -> Response:
    return JSONResponse({"status": "ok"})


async def _presets(req: StarletteRequest) -> Response:
    out = {}
    for k, v in _preset.DEFINITIONS.items():
        out[k] = {
            "kind": v.kind, "base_url": v.base_url, "api_key_env": v.api_key_env,
            "reset_tz": v.reset_tz, "json_mode": v.json_mode, "system_role": v.system_role,
            "category": v.category,
        }
    return JSONResponse(out)


async def _free_pool(req: StarletteRequest) -> Response:
    cat_param = req.query_params.get("category", "")
    categories: Optional[list[str]] = None
    if cat_param and cat_param != "all":
        categories = [c.strip() for c in cat_param.split(",") if c.strip()]
    return JSONResponse({
        "models": [m.to_dict() for m in _preset.free_pool_by_category(categories)],
        "verified_on": _preset.catalog_verified_on(),
        "summary": _preset.catalog_summary(),
        "categories": _preset.CATEGORIES,
    })


async def _ui(req: StarletteRequest) -> Response:
    try:
        html = _INDEX_HTML.read_text(encoding="utf-8")
    except FileNotFoundError:
        html = "<html><body><h1>airouter</h1><p>web UI not installed</p></body></html>"
    return Response(html, media_type="text/html; charset=utf-8", headers={
        "Cache-Control": "no-cache",
        "X-Content-Type-Options": "nosniff",
    })


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def new_handler(router: Router, admin_token: str = "", cors: str = "") -> Starlette:
    """Build the Starlette app. Mount inside your own program via ``app.router.mount("/airouter", app)`` if needed."""
    routes = [
        Route("/", _ui, methods=["GET"]),
        Route("/healthz", _healthz, methods=["GET"]),
        Route("/v1/presets", _presets, methods=["GET"]),
        Route("/v1/presets/free-pool", _free_pool, methods=["GET"]),

        # Native API
        Route("/v1/generate", _generate, methods=["POST"]),
        Route("/v1/speak", _speak, methods=["POST"]),
        Route("/v1/transcribe", _transcribe, methods=["POST"]),
        Route("/v1/audio/transcriptions", _transcribe, methods=["POST"]),
        Route("/v1/usage", _usage, methods=["GET"]),
        Route("/v1/whoami", _whoami, methods=["GET"]),

        # OpenAI-compatible surface
        Route("/v1/chat/completions", _chat_completions, methods=["POST"]),
        Route("/v1/models", _models, methods=["GET"]),
        Route("/v1/audio/speech", _audio_speech, methods=["POST"]),

        # Admin
        Route("/v1/providers", _list_providers, methods=["GET"]),
        Route("/v1/providers", _save_provider, methods=["POST"]),
        Route("/v1/providers/{id}", _delete_provider, methods=["DELETE"]),
        Route("/v1/providers/{id}/{action}", _toggle_provider, methods=["POST"]),
        Route("/v1/bootstrap", _bootstrap, methods=["POST"]),
        Route("/v1/tenants", _list_tenants, methods=["GET"]),
        Route("/v1/tenants", _create_tenant, methods=["POST"]),
        Route("/v1/tenants/{id}/rotate-key", _rotate_tenant_key, methods=["POST"]),

        # /v1/me/*
        Route("/v1/me", _me_get, methods=["GET"]),
        Route("/v1/me", _me_patch, methods=["PATCH"]),
        Route("/v1/me/rotate-key", _me_rotate_key, methods=["POST"]),
        Route("/v1/me/usage", _me_usage, methods=["GET"]),
        Route("/v1/me/bootstrap", _me_bootstrap, methods=["POST"]),
        Route("/v1/me/providers", _me_list, methods=["GET"]),
        Route("/v1/me/providers", _me_add, methods=["POST"]),
        Route("/v1/me/providers/{id}/{action}", _me_action, methods=["POST"]),
        Route("/v1/me/providers/{id}", _me_delete, methods=["DELETE"]),
        Route("/v1/me/catalog", _me_catalog, methods=["GET"]),
        Route("/v1/me/catalog/add", _me_catalog_add, methods=["POST"]),
        Route("/v1/me/ollama", _me_ollama, methods=["GET"]),
        Route("/v1/me/ollama/add", _me_ollama_add, methods=["POST"]),
        Route("/v1/me/import", _me_import, methods=["POST"]),
        Route("/v1/me/export", _me_export, methods=["GET"]),
    ]
    middleware = []
    if cors:
        origins = [o.strip() for o in cors.split(",") if o.strip()]
        middleware.append(Middleware(
            CORSMiddleware, allow_origins=origins,
            allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Tenant-ID"],
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            expose_headers=["X-AIRouter-Provider", "X-AIRouter-Model", "Retry-After"],
        ))
    app = Starlette(routes=routes, middleware=middleware)
    app.state.router = router
    app.state.admin_token = admin_token
    return app


__all__ = ["new_handler"]
