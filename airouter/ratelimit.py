"""Rate-limit header / body parsing.

Python port of ``pkg/router/ratelimit.go``. Providers tell us part of their
limits through HTTP response headers and 429 bodies. The functions here turn
those into a structured :class:`RateInfo` so the router can:

* learn the real per-day request limit / per-minute token limit and replace
  the configured ``rpd`` / ``tpm`` with it (so a paid tier or a changed free
  tier needs no config edit);
* correct the daily counter from the provider (another app sharing the same
  key changes the real remaining);
* pause a provider when it reports "0 remaining" until its own reset time.

Header families understood (all optional, best-effort):

* Groq / OpenAI / Mistral style: ``x-ratelimit-{limit,remaining,reset}-{requests,tokens}``
* Cerebras style:                ``x-ratelimit-{limit,remaining,reset}-{requests-day,tokens-minute}``
* Everyone:                      ``retry-after`` (seconds or an HTTP date)

Gemini's OpenAI-compatible endpoint sends no such headers; its 429 body
carries a ``retryDelay`` instead (see :func:`retry_delay_from_body`).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Optional

# Hosts whose plain x-ratelimit-*-requests headers describe a PER-DAY window
# (Groq). For every other host the plain names are treated as per-minute
# (OpenAI style); the explicit "-day" / "-minute" header names (Cerebras
# style) are always trusted.
DAILY_REQUEST_HOST_HINTS = ["groq.com"]


@dataclass
class RateInfo:
    """What a provider told us about its own limits in response headers."""

    requests_window: str = ""           # "day", "minute", or "" when unknown
    limit_requests: int = -1
    remaining_requests: int = -1
    reset_requests: float = -1.0        # seconds until reset
    limit_tokens: int = -1
    remaining_tokens: int = -1
    reset_tokens: float = -1.0
    reset_requests_seconds: int = 0
    reset_tokens_seconds: int = 0
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.requests_window:
            out["requests_window"] = self.requests_window
        out["limit_requests"] = self.limit_requests
        out["remaining_requests"] = self.remaining_requests
        out["limit_tokens"] = self.limit_tokens
        out["remaining_tokens"] = self.remaining_tokens
        if self.reset_requests_seconds:
            out["reset_requests_seconds"] = self.reset_requests_seconds
        if self.reset_tokens_seconds:
            out["reset_tokens_seconds"] = self.reset_tokens_seconds
        return out


def _first_header(headers: dict[str, str], *names: str) -> str:
    """First non-empty header value among ``names`` (case-insensitive)."""
    if not headers:
        return ""
    # Build a lowercase view once
    lower: dict[str, str] = {}
    for k, v in headers.items():
        lk = k.lower()
        if lk not in lower:
            lower[lk] = (v or "").strip()
    for n in names:
        v = lower.get(n.lower(), "")
        if v:
            return v
    return ""


def _atoi_or(s: str, default: int) -> int:
    if not s:
        return default
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return default


_DUR_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|d|h|m|s)")


def parse_reset_duration(s: str) -> float:
    """Accept ``"7.66s"``, ``"2m59.56s"``, ``"1h2m"``, ``"1d"``, ``"250ms"`` or a bare number of seconds."""
    s = (s or "").strip()
    if not s:
        return -1.0
    try:
        return float(s)
    except ValueError:
        pass
    units = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
    total = 0.0
    found = False
    for m in _DUR_PART.finditer(s):
        v = float(m.group(1))
        total += v * units[m.group(2)]
        found = True
    return total if found else -1.0


def parse_retry_after(v: str, now: Optional[float] = None) -> float:
    """Read ``Retry-After`` as seconds or as an HTTP date."""
    v = (v or "").strip()
    if not v:
        return 0.0
    try:
        f = float(v)
        if f >= 0:
            return f
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(v)
        if dt is not None:
            d = dt.timestamp() - (now if now is not None else time.time())
            if d > 0:
                return d
    except (TypeError, ValueError):
        pass
    return 0.0


_RETRY_DELAY_RE = re.compile(r'"retryDelay"\s*:\s*"([0-9.]+)s"')


def retry_delay_from_body(body: str) -> float:
    """Pull Gemini's ``"retryDelay": "34s"`` out of a 429 body."""
    m = _RETRY_DELAY_RE.search(body or "")
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return 0.0


def parse_rate_headers(headers: dict[str, str], now: Optional[float] = None,
                       base_url: str = "") -> tuple[RateInfo, bool]:
    """Extract :class:`RateInfo`; ``ok`` is False when nothing useful was sent."""
    if now is None:
        now = time.time()
    ri = RateInfo(
        limit_requests=_atoi_or(
            _first_header(headers, "x-ratelimit-limit-requests", "x-ratelimit-limit-requests-day"), -1),
        remaining_requests=_atoi_or(
            _first_header(headers, "x-ratelimit-remaining-requests", "x-ratelimit-remaining-requests-day"), -1),
        reset_requests=parse_reset_duration(
            _first_header(headers, "x-ratelimit-reset-requests", "x-ratelimit-reset-requests-day")),
        limit_tokens=_atoi_or(
            _first_header(headers, "x-ratelimit-limit-tokens", "x-ratelimit-limit-tokens-minute"), -1),
        remaining_tokens=_atoi_or(
            _first_header(headers, "x-ratelimit-remaining-tokens", "x-ratelimit-remaining-tokens-minute"), -1),
        reset_tokens=parse_reset_duration(
            _first_header(headers, "x-ratelimit-reset-tokens", "x-ratelimit-reset-tokens-minute")),
        at=now,
    )
    has_day = bool(_first_header(headers, "x-ratelimit-limit-requests-day", "x-ratelimit-remaining-requests-day"))
    has_minute = bool(_first_header(headers, "x-ratelimit-limit-requests-minute"))
    if has_day:
        ri.requests_window = "day"
    elif has_minute:
        ri.requests_window = "minute"
    elif ri.limit_requests >= 0 or ri.remaining_requests >= 0:
        ri.requests_window = "minute"
        for host in DAILY_REQUEST_HOST_HINTS:
            if host in (base_url or ""):
                ri.requests_window = "day"
                break
    ok = (
        ri.limit_requests >= 0
        or ri.remaining_requests >= 0
        or ri.limit_tokens >= 0
        or ri.remaining_tokens >= 0
    )
    if ri.reset_requests > 0:
        ri.reset_requests_seconds = int(ri.reset_requests)
    if ri.reset_tokens > 0:
        ri.reset_tokens_seconds = int(ri.reset_tokens)
    return ri, ok


def quota_detail(body: str) -> tuple[bool, float, bool]:
    """Dig the structured part out of a Google-style 429 body.

    Returns ``(daily, retry_seconds, ok)`` where:

    * ``daily`` is True when any violated quota is a PerDay quota.
    * ``retry_seconds`` is the parsed ``retryDelay`` value (0 when absent).
    * ``ok`` is True when the body parsed and contained quota information.

    Body shape::

        {"error":{"status":"RESOURCE_EXHAUSTED","details":[
          {"@type":".../google.rpc.QuotaFailure","violations":[{"quotaId":"...PerDay..."}]},
          {"@type":".../google.rpc.RetryInfo","retryDelay":"34s"}]}}
    """
    try:
        env = json.loads(body or "{}")
    except (ValueError, TypeError):
        return False, 0.0, False
    err = env.get("error") or {}
    daily = False
    retry = 0.0
    ok = False
    for d in err.get("details", []) or []:
        for v in d.get("violations", []) or []:
            ok = True
            qid = (v.get("quotaId") or "").lower()
            if "perday" in qid:
                daily = True
        rd = d.get("retryDelay") or ""
        if rd:
            try:
                retry = float(rd.rstrip("s"))
                ok = True
            except ValueError:
                pass
    return daily, retry, ok


__all__ = [
    "DAILY_REQUEST_HOST_HINTS",
    "RateInfo",
    "parse_rate_headers",
    "parse_retry_after",
    "parse_reset_duration",
    "retry_delay_from_body",
    "quota_detail",
]
