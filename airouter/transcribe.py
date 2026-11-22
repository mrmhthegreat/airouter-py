"""Speech-to-text routing.

Python port of ``pkg/router/transcribe.go``. Converts speech (audio bytes)
to text by picking an STT-capable provider (category=stt), sending the
audio, and failing over on errors.

Two API shapes are supported:

* **Groq Whisper** (OpenAI-compatible): ``POST {base}/audio/transcriptions``
  with ``multipart/form-data``. Returns JSON ``{"text": "..."}``.
* **Gemini 3.5 Transcribe** (two-step Files API): upload the file via
  ``POST /upload/v1beta/files``, poll until ``ACTIVE``, then POST the file
  URI to ``/v1beta/interactions``. Returns ``{"output_text": "..."}``.
"""
from __future__ import annotations

import io
import time
from typing import Optional

import requests

from .limiter import classify_failure, day_of, observe_rate_headers
from .ratelimit import parse_retry_after
from .types import (
    CATEGORY_STT,
    DEFAULT_TENANT_ID,
    Attempt,
    BadRequestError,
    ErrTenantDisabled,
    ErrUnknownTenant,
    ExhaustedError,
    HTTPError,
    ProviderConfig,
    TenantLimitError,
    TranscribeRequest,
    TranscribeResponse,
    truncate,
)


def _mime_from_filename(name: str) -> str:
    low = (name or "").lower()
    if low.endswith(".mp3"):
        return "audio/mpeg"
    if low.endswith(".wav"):
        return "audio/wav"
    if low.endswith(".m4a"):
        return "audio/mp4"
    if low.endswith(".ogg"):
        return "audio/ogg"
    if low.endswith(".flac"):
        return "audio/flac"
    if low.endswith(".webm"):
        return "audio/webm"
    return "application/octet-stream"


def _wav_duration_seconds(audio: bytes) -> int:
    """Parse a RIFF/WAV header and return the audio duration in seconds.

    Returns 0 if the bytes don't look like a WAV file or the header is
    incomplete.
    """
    if len(audio) < 44:
        return 0
    if audio[0:4] != b"RIFF" or audio[8:12] != b"WAVE":
        return 0
    # Walk chunks to find "data".
    i = 12
    data_start = -1
    while i + 8 <= len(audio):
        chunk_id = audio[i:i + 4]
        chunk_size = int.from_bytes(audio[i + 4:i + 8], "little")
        if chunk_id == b"data":
            data_start = i + 8
            break
        i += 8 + chunk_size
        if chunk_size & 1:
            i += 1
    if data_start < 0:
        return 0
    sample_rate = int.from_bytes(audio[24:28], "little")
    byte_rate = int.from_bytes(audio[28:32], "little")
    if byte_rate <= 0 or sample_rate <= 0:
        return 0
    data_size_offset = data_start - 4
    if data_size_offset + 4 > len(audio):
        return 0
    data_size = int.from_bytes(audio[data_size_offset:data_size_offset + 4], "little")
    if data_size <= 0:
        data_size = len(audio) - data_start
    secs = data_size // byte_rate
    return max(secs, 1)


def _estimate_stt_audio_seconds(audio: bytes, filename: str, mime_type: str) -> int:
    """Estimate input audio duration in seconds from byte count + filename/MIME."""
    if not audio:
        return 0
    secs = _wav_duration_seconds(audio)
    if secs > 0:
        return secs
    # Fall back to byte-count heuristic: assume ~16 KB/s (128 kbps compressed).
    secs = len(audio) // 16000
    return max(secs, 1)


# ---------------------------------------------------------------------------
# Groq Whisper (OpenAI-compatible multipart endpoint)
# ---------------------------------------------------------------------------


def _call_transcribe_openai(r, p: ProviderConfig, req: TranscribeRequest):
    url = p.base_url + "/audio/transcriptions"
    filename = req.filename or "audio.bin"
    mime = req.mime_type or _mime_from_filename(filename)
    files = {"file": (filename, req.audio, mime)}
    data = {"model": p.model, "response_format": "json"}
    if req.language:
        data["language"] = req.language
    if req.prompt:
        data["prompt"] = req.prompt
    headers = {}
    if p.resolve_key():
        headers["Authorization"] = f"Bearer {p.resolve_key()}"
    try:
        resp = r.session.post(url, files=files, data=data, headers=headers, timeout=120)
    except requests.RequestException as e:
        return "", "", e
    data_bytes = resp.content
    if resp.status_code // 100 != 2:
        ra = parse_retry_after(resp.headers.get("Retry-After", ""), r.now())
        return "", "", HTTPError(
            code=resp.status_code,
            body=data_bytes.decode("utf-8", errors="replace"),
            retry_after=ra,
            headers=dict(resp.headers),
        )
    try:
        import json as _json
        payload = _json.loads(data_bytes)
    except ValueError:
        return "", "", ValueError(f"bad transcription response: {truncate(data_bytes.decode('utf-8', 'replace'), 200)}")
    text = payload.get("text", "") or ""
    lang = payload.get("language", "") or ""
    if not text.strip():
        return "", "", ValueError("empty transcription (no speech detected?)")
    return text, lang, None


# ---------------------------------------------------------------------------
# Gemini Transcribe (two-step Files API + interactions endpoint)
# ---------------------------------------------------------------------------


def _gemini_upload_file(r, p: ProviderConfig, audio: bytes, mime: str, filename: str):
    """Upload raw audio bytes to Gemini's Files API; return ``(file_name, file_uri)``."""
    import json as _json
    url = p.base_url + "/upload/v1beta/files?uploadType=multipart"
    boundary = f"-----airouter-gemini-{int(time.time() * 1e9)}"
    meta = _json.dumps({"file": {"display_name": filename}})
    body = (
        f"--{boundary}\r\n"
        "Content-Type: application/json\r\n\r\n"
        f"{meta}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: {mime}\r\n\r\n"
    ).encode("utf-8") + audio + f"\r\n--{boundary}--\r\n".encode("utf-8")
    headers = {"Content-Type": f"multipart/related; boundary={boundary}"}
    if p.resolve_key():
        headers["x-goog-api-key"] = p.resolve_key()
    try:
        resp = r.session.post(url, content=body, headers=headers, timeout=60)
    except requests.RequestException as e:
        return "", "", e
    data = resp.content
    if resp.status_code // 100 != 2:
        ra = parse_retry_after(resp.headers.get("Retry-After", ""), r.now())
        return "", "", HTTPError(
            code=resp.status_code,
            body=data.decode("utf-8", errors="replace"),
            retry_after=ra,
            headers=dict(resp.headers),
        )
    try:
        payload = _json.loads(data)
    except ValueError:
        return "", "", ValueError(f"bad upload response: {truncate(data.decode('utf-8', 'replace'), 200)}")
    file = (payload.get("file") or {})
    name = file.get("name", "")
    if not name:
        return "", "", ValueError(f"bad upload response: {truncate(data.decode('utf-8', 'replace'), 200)}")
    return name, file.get("uri", ""), None


def _gemini_wait_for_file(r, p: ProviderConfig, file_name: str) -> Optional[Exception]:
    """Poll GET /v1beta/files/{name} until state == ACTIVE or FAILED."""
    import json as _json
    url = p.base_url + "/v1beta/" + file_name
    deadline = time.time() + 60
    headers = {}
    if p.resolve_key():
        headers["x-goog-api-key"] = p.resolve_key()
    while time.time() < deadline:
        try:
            resp = r.session.get(url, headers=headers, timeout=10)
        except requests.RequestException as e:
            return e
        data = resp.content
        if resp.status_code // 100 != 2:
            return ValueError(f"poll file status HTTP {resp.status_code}: {truncate(data.decode('utf-8', 'replace'), 200)}")
        try:
            payload = _json.loads(data)
        except ValueError:
            payload = {}
        state = payload.get("state", "")
        if state == "ACTIVE":
            return None
        if state == "FAILED":
            err = (payload.get("error") or {}).get("message", "")
            return ValueError(f"file processing failed: {err}" if err else "file processing failed (state=FAILED)")
        time.sleep(0.5)
    return ValueError("timed out waiting for file to become ACTIVE")


def _gemini_delete_file(r, p: ProviderConfig, file_name: str) -> None:
    """Best-effort cleanup of an uploaded file."""
    if not file_name:
        return
    url = p.base_url + "/v1beta/" + file_name
    headers = {}
    if p.resolve_key():
        headers["x-goog-api-key"] = p.resolve_key()
    try:
        r.session.delete(url, headers=headers, timeout=10)
    except Exception:
        pass


def _gemini_transcribe_interactions(r, p: ProviderConfig, file_uri: str, req: TranscribeRequest):
    """POST the file URI to /v1beta/interactions and return the transcribed text."""
    import json as _json
    url = p.base_url + "/v1beta/interactions"
    body: dict = {
        "model": "models/" + p.model,
        "input": [{"type": "file", "file_uri": file_uri}],
    }
    transcription_config: dict = {}
    if req.language:
        transcription_config["language_codes"] = [req.language]
    if req.prompt:
        transcription_config["custom_vocabulary"] = [s.strip() for s in req.prompt.split(",") if s.strip()]
    if transcription_config:
        body["generation_config"] = {"transcription_config": transcription_config}
    headers = {"Content-Type": "application/json"}
    if p.resolve_key():
        headers["x-goog-api-key"] = p.resolve_key()
    try:
        resp = r.session.post(url, json=body, headers=headers, timeout=120)
    except requests.RequestException as e:
        return "", e
    data = resp.content
    if resp.status_code // 100 != 2:
        ra = parse_retry_after(resp.headers.get("Retry-After", ""), r.now())
        return "", HTTPError(
            code=resp.status_code,
            body=data.decode("utf-8", errors="replace"),
            retry_after=ra,
            headers=dict(resp.headers),
        )
    try:
        payload = _json.loads(data)
    except ValueError:
        return "", ValueError(f"bad transcribe response: {truncate(data.decode('utf-8', 'replace'), 200)}")
    err_msg = (payload.get("error") or {}).get("message", "")
    if err_msg:
        return "", ValueError(f"gemini transcribe error: {err_msg}")
    text = payload.get("output_text", "") or ""
    if not text.strip():
        return "", ValueError("empty transcription (no speech detected?)")
    return text, None


def _call_transcribe_gemini(r, p: ProviderConfig, req: TranscribeRequest):
    filename = req.filename or "audio.bin"
    mime = req.mime_type or _mime_from_filename(filename)
    file_name, file_uri, err = _gemini_upload_file(r, p, req.audio, mime, filename)
    if err:
        return "", "", (ValueError(f"gemini files upload: {err}") if not isinstance(err, HTTPError) else err)
    try:
        wait_err = _gemini_wait_for_file(r, p, file_name)
        if wait_err:
            return "", "", (ValueError(f"gemini files wait: {wait_err}") if not isinstance(wait_err, HTTPError) else wait_err)
        text, terr = _gemini_transcribe_interactions(r, p, file_uri, req)
        if terr:
            return "", "", terr
        return text, "", None
    finally:
        _gemini_delete_file(r, p, file_name)


def _call_transcribe(r, p: ProviderConfig, req: TranscribeRequest):
    if "generativelanguage.googleapis.com" in p.base_url:
        return _call_transcribe_gemini(r, p, req)
    return _call_transcribe_openai(r, p, req)


def transcribe(r, req: TranscribeRequest) -> TranscribeResponse:
    """Convert speech to text. Picks an STT provider, sends the audio, fails over on errors."""
    start = r.now()
    if not req.tenant_id:
        req.tenant_id = DEFAULT_TENANT_ID
    if not req.audio:
        raise BadRequestError("TranscribeRequest.audio is required (raw audio bytes)")
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

    from .router import Request
    cands = r._candidates(Request(tenant_id=req.tenant_id, provider=req.provider, category=CATEGORY_STT))
    if not cands:
        raise RuntimeError("airouter: no STT providers configured (run `airouter bootstrap -category stt`)")

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

        text, lang, err = _call_transcribe(r, picked.cfg, req)
        picked.state.release(0)
        audio_secs = _estimate_stt_audio_seconds(req.audio, req.filename, req.mime_type)
        if err is None and audio_secs > 0:
            picked.state.release_audio(audio_secs, r.now())
        errs = 1 if err is not None else 0
        try:
            r.store.add_usage(req.tenant_id, picked.cfg.id, day_of(picked.cfg, now), 1, 0, errs)
        except Exception:
            pass
        if err is None:
            out = TranscribeResponse(
                text=text,
                provider=picked.cfg.name,
                model=picked.cfg.model,
                language=lang,
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


__all__ = ["transcribe"]
