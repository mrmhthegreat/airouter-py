"""Text-to-speech routing.

Python port of ``pkg/router/speak.go``. Synthesizes speech from text by
picking a TTS-capable provider (category=tts), sending the request, and
failing over on errors — same failover shape as :meth:`Router.generate`.

Two API shapes are supported:

* **Groq-style** (OpenAI-compatible): ``POST {base}/audio/speech`` with a JSON
  body. The provider returns raw audio bytes directly.
* **Gemini-style** (native): ``POST {base}/models/{model}:generateContent``
  with audio modality in ``generationConfig``. The response JSON contains
  base64-encoded WAV bytes in
  ``candidates[0].content.parts[0].inlineData.data``.
"""
from __future__ import annotations

import base64
import json as _json
import time
from typing import Optional

import requests

from .limiter import day_of
from .ratelimit import parse_retry_after
from .types import (
    CATEGORY_TTS,
    DEFAULT_TENANT_ID,
    Attempt,
    ErrBadRequest,
    ErrTenantDisabled,
    ErrUnknownTenant,
    ExhaustedError,
    HTTPError,
    ProviderConfig,
    SpeakRequest,
    SpeakResponse,
    TenantLimitError,
    truncate,
)


def _default_voice(p: ProviderConfig) -> str:
    """Pick a sensible voice id for the provider/model when the caller did not specify one."""
    if "generativelanguage.googleapis.com" in p.base_url:
        return "Kore"  # one of Gemini's 30 prebuilt voices
    if "orpheus-v1-english" in p.model:
        return "tara"  # natural female English voice
    if "orpheus-arabic-saudi" in p.model:
        return "hassan"  # male Saudi Arabic voice
    return "tara"


def _content_type_for_format(fmt: str) -> str:
    fmt = (fmt or "wav").lower()
    return {
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "pcm": "audio/L16;rate=24000",
        "flac": "audio/flac",
        "opus": "audio/ogg",
    }.get(fmt, "audio/wav")


def _estimate_tts_audio_seconds(text: str) -> int:
    """Estimate output audio duration from input text length.

    Average English speaking rate is ~150 wpm = ~15 chars/sec. Always at
    least 1 second. The 90% safety margin in ``acquire`` absorbs the error.
    """
    n = len(text or "")
    if n == 0:
        return 0
    secs = n // 15
    return max(secs, 1)


def _call_speak_openai(r, p: ProviderConfig, req: SpeakRequest, voice: str, fmt: str):
    """Groq path: POST {base}/audio/speech with JSON body."""
    url = p.base_url + "/audio/speech"
    body = {
        "model": p.model,
        "input": req.text,
        "voice": voice,
        "response_format": fmt,
    }
    headers = {"Content-Type": "application/json"}
    if p.resolve_key():
        headers["Authorization"] = f"Bearer {p.resolve_key()}"
    try:
        resp = r.session.post(url, json=body, headers=headers, timeout=60)
    except requests.RequestException as e:
        return b"", "", fmt, voice, e
    if resp.status_code // 100 != 2:
        ra = parse_retry_after(resp.headers.get("Retry-After", ""), r.now())
        return b"", "", fmt, voice, HTTPError(
            code=resp.status_code,
            body=resp.text[:4096],
            retry_after=ra,
            headers=dict(resp.headers),
        )
    ct = resp.headers.get("Content-Type") or _content_type_for_format(fmt)
    return resp.content, ct, fmt, voice, None


def _call_speak_gemini(r, p: ProviderConfig, req: SpeakRequest, voice: str, fmt: str):
    """Native Gemini path: POST {base}/models/{model}:generateContent with audio modality."""
    url = p.base_url + "/models/" + p.model + ":generateContent"
    body = {
        "contents": [{"parts": [{"text": req.text}]}],
        "generationConfig": {
            "response_modalities": ["AUDIO"],
            "speech_config": {
                "voice_config": {
                    "prebuilt_voice_config": {"voice_name": voice}
                }
            },
        },
    }
    headers = {"Content-Type": "application/json"}
    # Gemini's native endpoint uses x-goog-api-key, not Bearer.
    if p.resolve_key():
        headers["x-goog-api-key"] = p.resolve_key()
    try:
        resp = r.session.post(url, json=body, headers=headers, timeout=60)
    except requests.RequestException as e:
        return b"", "", fmt, voice, e
    data = resp.content
    if resp.status_code // 100 != 2:
        ra = parse_retry_after(resp.headers.get("Retry-After", ""), r.now())
        return b"", "", fmt, voice, HTTPError(
            code=resp.status_code,
            body=data.decode("utf-8", errors="replace"),
            retry_after=ra,
            headers=dict(resp.headers),
        )
    try:
        payload = _json.loads(data)
    except ValueError:
        return b"", "", fmt, voice, ValueError(f"bad gemini TTS response: {truncate(data.decode('utf-8', 'replace'), 200)}")
    candidates = payload.get("candidates") or []
    if not candidates:
        return b"", "", fmt, voice, ValueError(f"bad gemini TTS response: {truncate(data.decode('utf-8', 'replace'), 200)}")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    if not parts:
        return b"", "", fmt, voice, ValueError(f"bad gemini TTS response: {truncate(data.decode('utf-8', 'replace'), 200)}")
    inline = parts[0].get("inlineData") or {}
    audio_b64 = inline.get("data", "")
    try:
        audio = base64.b64decode(audio_b64)
    except Exception as e:
        return b"", "", fmt, voice, ValueError(f"bad base64 in gemini TTS response: {e}")
    ct = inline.get("mimeType") or "audio/wav"
    # Gemini unary always returns WAV (PCM 24kHz mono with RIFF header).
    return audio, ct, "wav", voice, None


def _call_speak(r, p: ProviderConfig, req: SpeakRequest):
    voice = req.voice or _default_voice(p)
    fmt = req.format or "wav"
    if "generativelanguage.googleapis.com" in p.base_url:
        return _call_speak_gemini(r, p, req, voice, fmt)
    return _call_speak_openai(r, p, req, voice, fmt)


def speak(r, req: SpeakRequest) -> SpeakResponse:
    """Synthesize speech from text. Picks a TTS provider, sends the request, fails over on errors."""
    from .limiter import classify_failure, observe_rate_headers
    from .router import Request
    start = r.now()
    if not req.tenant_id:
        req.tenant_id = DEFAULT_TENANT_ID
    if not (req.text or "").strip():
        raise BadRequestError("SpeakRequest.text is required")
    tenant = r.store.tenant_by_id(req.tenant_id)
    if tenant is None:
        raise ErrUnknownTenant()
    if tenant.disabled:
        raise ErrTenantDisabled()
    if tenant.daily_request_limit > 0:
        import datetime as _dt
        from .limiter import loc_for
        loc = loc_for(tenant.tz)
        day = _dt.datetime.fromtimestamp(start, loc).strftime("%Y-%m-%d")
        used = r.store.tenant_day_usage(tenant.id, day)
        if used >= tenant.daily_request_limit:
            now = _dt.datetime.fromtimestamp(start, loc)
            next_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + _dt.timedelta(days=1)
            raise TenantLimitError((next_midnight - now).total_seconds())

    cands = r._candidates(Request(tenant_id=req.tenant_id, provider=req.provider, category=CATEGORY_TTS))
    if not cands:
        raise RuntimeError("airouter: no TTS providers configured (run `airouter bootstrap -category tts`)")

    attempts: list[Attempt] = []
    failed: set[int] = set()
    deadline = start + r.max_wait

    while True:
        now = r.now()
        picked = None
        min_wait = float("inf")
        reasons: dict[str, str] = {}

        for c in cands:
            if c.cfg.id in failed:
                continue
            if not c.cfg.has_key():
                reasons[c.cfg.name] = f"no API key (set ${c.cfg.api_key_env})"
                continue
            ok, wait, _, why = r._acquire(c.cfg, c.state, now)
            if ok:
                picked = c
                break
            reasons[c.cfg.name] = why
            if wait < min_wait:
                min_wait = wait

        if picked is None:
            if (now + min_wait) > deadline or now > start + r.request_timeout:
                if min_wait == float("inf"):
                    min_wait = 3600
                raise ExhaustedError(min_wait, reasons)
            time.sleep(min(min_wait + 0.02, deadline - now))
            continue

        audio, ct, fmt, voice, err = _call_speak(r, picked.cfg, req)
        picked.state.release(0)
        audio_secs = _estimate_tts_audio_seconds(req.text)
        if err is None and audio_secs > 0:
            picked.state.release_audio(audio_secs, r.now())
        errs = 1 if err is not None else 0
        try:
            r.store.add_usage(req.tenant_id, picked.cfg.id, day_of(picked.cfg, now), 1, 0, errs)
        except Exception:
            pass
        if err is None:
            out = SpeakResponse(
                audio=audio,
                content_type=ct,
                format=fmt,
                provider=picked.cfg.name,
                model=picked.cfg.model,
                voice=voice,
                latency_ms=int((r.now() - start) * 1000),
                attempts=attempts,
            )
            return out
        if now > start + r.request_timeout:
            raise TimeoutError("airouter: request timed out")
        classify_failure(err=err, cfg=picked.cfg, state=picked.state, now=now, logf=r.logf)
        if picked.state.dead_until > now:
            try:
                r.store.set_pause(picked.cfg.id, picked.state.dead_until, picked.state.dead_reason)
            except Exception:
                pass
        observe_rate_headers(
            headers=getattr(err, "headers", None), base_url=picked.cfg.base_url,
            state=picked.state, cfg=picked.cfg, now=now, store=r.store, logf=r.logf,
        )
        attempts.append(Attempt(provider=picked.cfg.name, error=str(err)))
        failed.add(picked.cfg.id)


__all__ = ["speak"]
