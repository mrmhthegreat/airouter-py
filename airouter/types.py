"""Public data shapes used across the airouter package.

This is the Python port of ``pkg/types/types.go``. It defines provider
configs, tenants, requests, responses and the error types returned by the
router. Keeping them in one small module lets ``store``, ``router``,
``preset`` and ``server`` share them without circular imports.
"""
from __future__ import annotations

import json as _json
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

DEFAULT_TENANT_ID = "default"

# Category constants — kept here (as well as in preset) so the router and
# callers can filter without importing preset.
CATEGORY_TEXT = "text"
CATEGORY_TTS = "tts"
CATEGORY_STT = "stt"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AirouterError(Exception):
    """Base class for every typed airouter error."""


class ErrNoProviders(AirouterError):
    """No enabled provider matches the request."""

    def __init__(self) -> None:
        super().__init__("airouter: no enabled providers for this tenant")


class ErrUnknownTenant(AirouterError):
    def __init__(self) -> None:
        super().__init__("airouter: unknown tenant")


class ErrTenantDisabled(AirouterError):
    def __init__(self) -> None:
        super().__init__("airouter: tenant disabled")


class ErrUnauthorized(AirouterError):
    def __init__(self) -> None:
        super().__init__("airouter: invalid api key")


class ErrBadRequest(AirouterError):
    """Caller must fix the request (empty prompt, every provider rejected it)."""

    def __init__(self, message: str = "airouter: bad request") -> None:
        super().__init__(message)


class ErrTimeout(AirouterError):
    def __init__(self) -> None:
        super().__init__("airouter: request timed out")


class ErrNotFound(AirouterError):
    def __init__(self) -> None:
        super().__init__("not found")


class BadRequestError(AirouterError):
    """Every provider rejected the request itself (HTTP 400/413/422)."""

    def __init__(self, message: str, attempts: Optional[list["Attempt"]] = None) -> None:
        super().__init__("airouter: bad request: " + message)
        self.message = message
        self.attempts = attempts or []


class ExhaustedError(AirouterError):
    """Every provider is out of quota / cooling down right now."""

    def __init__(self, retry_after: float, reasons: Optional[dict[str, str]] = None) -> None:
        secs = int(retry_after)
        super().__init__(
            f"airouter: all providers are limited right now (retry in {secs}s)"
        )
        self.retry_after = retry_after
        self.reasons = reasons or {}


class AllFailedError(AirouterError):
    """Providers were tried and each one failed (network error, bad response, ...)."""

    def __init__(self, attempts: list["Attempt"]) -> None:
        parts = [f"{a.provider}: {a.error}" for a in attempts]
        super().__init__("airouter: all providers failed: " + "; ".join(parts))
        self.attempts = attempts


class TenantLimitError(AirouterError):
    """The tenant's own daily request cap is used up."""

    def __init__(self, retry_after: float) -> None:
        super().__init__("airouter: tenant daily request limit reached")
        self.retry_after = retry_after


class HTTPError(AirouterError):
    """An upstream (provider) HTTP error, surfaced for inspection."""

    def __init__(
        self,
        code: int,
        body: str,
        retry_after: float = 0.0,
        headers: Optional[dict[str, str]] = None,
    ) -> None:
        super().__init__(f"HTTP {code}: {truncate(body, 200)}")
        self.code = code
        self.body = body
        self.retry_after = retry_after
        self.headers = headers or {}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


def truncate(s: str, n: int) -> str:
    """Trim ``s`` to ``n`` characters, appending ``...`` when it was longer."""
    s = (s or "").strip()
    if len(s) > n:
        return s[:n] + "..."
    return s


@dataclass
class Message:
    role: str  # system | user | assistant
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class Attempt:
    provider: str
    error: str
    status: int = 0  # upstream HTTP status, when there was one
    kind: str = ""   # auth | rate_limit | daily_limit | billing | bad_request | timeout | transient | bad_response

    def to_dict(self) -> dict[str, Any]:
        out = {"provider": self.provider, "error": self.error}
        if self.status:
            out["status"] = self.status
        if self.kind:
            out["kind"] = self.kind
        return out


@dataclass
class Response:
    text: str = ""
    json: Any = None  # decoded JSON value when json was requested
    provider: str = ""
    model: str = ""
    tokens: int = 0
    latency_ms: int = 0
    attempts: list[Attempt] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "text": self.text,
            "provider": self.provider,
            "model": self.model,
            "tokens": self.tokens,
            "latency_ms": self.latency_ms,
        }
        if self.json is not None:
            out["json"] = self.json
        if self.attempts:
            out["attempts"] = [a.to_dict() for a in self.attempts]
        return out


@dataclass
class Request:
    tenant_id: str = ""
    system: str = ""
    prompt: str = ""
    messages: list[Message] = field(default_factory=list)
    json: bool = False          # ask for (and validate) a JSON answer
    max_tokens: int = 0
    temperature: Optional[float] = None
    provider: str = ""          # pin one provider by name
    strategy: str = ""          # "priority" (default) | "spread"
    category: str = ""          # internal: filter providers by category; defaults to "text"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in asdict(self).items():  # type: ignore[arg-type]
            if v in (None, "", 0, [], False):
                continue
            if k == "messages":
                out["messages"] = [m.to_dict() for m in self.messages]
            elif k == "temperature":
                out["temperature"] = v
            elif k == "json":
                if v:
                    out["json"] = True
            else:
                out[k] = v
        return out


@dataclass
class SpeakRequest:
    tenant_id: str = ""
    text: str = ""
    voice: str = ""
    format: str = ""            # wav (default), mp3, pcm
    language: str = ""
    provider: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"text": self.text}
        for k in ("voice", "format", "language", "provider"):
            v = getattr(self, k)
            if v:
                out[k] = v
        return out


@dataclass
class SpeakResponse:
    audio: bytes = b""
    audio_b64: str = ""         # set only by HTTP JSON response shape
    content_type: str = ""
    format: str = ""
    provider: str = ""
    model: str = ""
    voice: str = ""
    latency_ms: int = 0
    attempts: list[Attempt] = field(default_factory=list)

    def to_dict(self, include_audio_b64: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "content_type": self.content_type,
            "format": self.format,
            "provider": self.provider,
            "model": self.model,
        }
        if self.voice:
            out["voice"] = self.voice
        out["latency_ms"] = self.latency_ms
        if self.attempts:
            out["attempts"] = [a.to_dict() for a in self.attempts]
        if include_audio_b64 and self.audio_b64:
            out["audio_base64"] = self.audio_b64
        return out


@dataclass
class TranscribeRequest:
    tenant_id: str = ""
    audio: bytes = b""
    filename: str = ""
    mime_type: str = ""
    language: str = ""
    prompt: str = ""
    provider: str = ""


@dataclass
class TranscribeResponse:
    text: str = ""
    provider: str = ""
    model: str = ""
    language: str = ""
    latency_ms: int = 0
    attempts: list[Attempt] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "text": self.text,
            "provider": self.provider,
            "model": self.model,
        }
        if self.language:
            out["language"] = self.language
        out["latency_ms"] = self.latency_ms
        if self.attempts:
            out["attempts"] = [a.to_dict() for a in self.attempts]
        return out


# ---------------------------------------------------------------------------
# ProviderConfig / Tenant
# ---------------------------------------------------------------------------


@dataclass
class ProviderConfig:
    """One model on one provider. Every entry has its OWN limits.

    Mirrors ``types.ProviderConfig`` from the Go source.
    """

    id: int = 0
    name: str = ""
    tenant_id: str = ""        # "" = shared by every tenant
    preset: str = ""           # input only: fills defaults from the catalog

    # Upstream connection
    kind: str = ""             # "openai" | "ollama"
    base_url: str = ""
    api_key_env: str = ""      # env var holding the key (preferred)
    api_key: str = ""          # or stored in the DB (never returned by the API)
    model: str = ""

    # Local limits
    priority: int = 0          # lower = tried first
    rpm: int = 0               # requests per minute (0 = no local cap)
    rpd: int = 0               # requests per day    (0 = no local cap)
    tpm: int = 0               # tokens per minute   (0 = no local cap)
    ash: int = 0               # audio seconds per hour (TTS/STT only)
    asd: int = 0               # audio seconds per day  (TTS/STT only)
    workers: int = 0           # max concurrent requests to this model

    # Behaviour flags
    reset_tz: str = ""
    json_mode: Optional[bool] = None    # None/True = send response_format json; False for Gemma
    system_role: Optional[bool] = None  # None/True = send system messages; False folds them
    category: str = ""                  # "text" (default) | "tts" | "stt"
    disabled: bool = False

    # Input-only seed
    used_today_hint: int = 0

    # ---- helpers (mirror the Go methods) ----------------------------------

    def resolve_key(self) -> str:
        """Prefer the env var, fall back to the literal ``api_key``."""
        if self.api_key_env:
            v = os.environ.get(self.api_key_env, "")
            if v:
                return v
        return self.api_key

    def needs_key(self) -> bool:
        return self.kind != "ollama"

    def has_key(self) -> bool:
        return (not self.needs_key()) or self.resolve_key() != ""

    def key_source(self) -> str:
        if self.api_key_env and os.environ.get(self.api_key_env, ""):
            return "env"
        if self.api_key:
            return "db"
        if not self.needs_key():
            return "not needed"
        return "none"

    def key_hint(self) -> str:
        k = self.resolve_key()
        if len(k) < 8:
            return ""
        return "..." + k[-4:]

    def use_json_mode(self) -> bool:
        return self.json_mode is None or self.json_mode

    def use_system_role(self) -> bool:
        return self.system_role is None or self.system_role

    def validate(self) -> None:
        """Normalise fields and raise ``ValueError`` if required fields are missing."""
        self.name = self.name.strip()
        self.kind = self.kind.strip().lower()
        if not self.name:
            raise ValueError("name is required")
        if self.kind not in ("openai", "ollama"):
            raise ValueError(f"kind must be openai or ollama, got {self.kind!r}")
        if not self.base_url and self.kind == "ollama":
            self.base_url = "http://localhost:11434"
        if not self.base_url:
            raise ValueError("base_url is required")
        if not self.model:
            raise ValueError("model is required (copy the exact id from the provider's console)")
        if not self.reset_tz:
            self.reset_tz = "UTC"
        # validate IANA zone
        try:
            import zoneinfo  # Python 3.9+
            zoneinfo.ZoneInfo(self.reset_tz)
        except Exception as e:
            raise ValueError(f"unknown reset_tz {self.reset_tz!r}") from e
        if self.workers <= 0:
            self.workers = 2
        if self.priority == 0:
            self.priority = 100
        if not self.category:
            self.category = CATEGORY_TEXT
        self.base_url = self.base_url.rstrip("/")

    def to_dict(self, *, include_key: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "base_url": self.base_url,
            "model": self.model,
            "priority": self.priority,
            "rpm": self.rpm,
            "rpd": self.rpd,
            "tpm": self.tpm,
            "ash": self.ash,
            "asd": self.asd,
            "workers": self.workers,
            "reset_tz": self.reset_tz,
            "category": self.category or CATEGORY_TEXT,
            "disabled": self.disabled,
        }
        if self.tenant_id:
            out["tenant_id"] = self.tenant_id
        if self.preset:
            out["preset"] = self.preset
        if self.api_key_env and include_key:
            out["api_key_env"] = self.api_key_env
        if self.api_key and include_key:
            out["api_key"] = self.api_key
        if self.json_mode is not None:
            out["json_mode"] = self.json_mode
        if self.system_role is not None:
            out["system_role"] = self.system_role
        return out


@dataclass
class Tenant:
    id: str
    name: str = ""
    tz: str = "UTC"                       # tenant's own day boundary for its daily cap
    daily_request_limit: int = 0          # 0 = unlimited
    disabled: bool = False
    created_at: float = field(default_factory=lambda: time.time())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "tz": self.tz,
            "daily_request_limit": self.daily_request_limit,
            "disabled": self.disabled,
            "created_at": _iso(self.created_at),
        }


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def parse_iso(s: str) -> float:
    """Parse a RFC3339-ish timestamp into a unix epoch."""
    s = s.strip()
    if not s:
        return 0.0
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%S%z"))
    except Exception:
        try:
            return float(s)
        except Exception:
            return 0.0


__all__ = [
    "DEFAULT_TENANT_ID",
    "CATEGORY_TEXT",
    "CATEGORY_TTS",
    "CATEGORY_STT",
    "AirouterError",
    "ErrNoProviders",
    "ErrUnknownTenant",
    "ErrTenantDisabled",
    "ErrUnauthorized",
    "ErrBadRequest",
    "ErrTimeout",
    "ErrNotFound",
    "BadRequestError",
    "ExhaustedError",
    "AllFailedError",
    "TenantLimitError",
    "HTTPError",
    "Message",
    "Attempt",
    "Response",
    "Request",
    "SpeakRequest",
    "SpeakResponse",
    "TranscribeRequest",
    "TranscribeResponse",
    "ProviderConfig",
    "Tenant",
    "truncate",
]
