"""Live in-memory state of one provider entry, plus the limiter that uses it.

Python port of ``pkg/router/limiter.go`` and the error-classification half
of ``pkg/router/errors.go``.

The provider state holds:

* a rolling 60-second window of request hits (for RPM/TPM);
* a rolling 1-hour window of audio hits (for ASH);
* a daily request/token/audio-second counter;
* the cooldown / dead-until timestamps that drive failover;
* the last rate-limit headers the provider sent (so the router can prefer
  the provider's own numbers over the configured ones).

Daily counters are hydrated from the store, so restarts don't forget usage.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

from .ratelimit import (
    RateInfo,
    parse_rate_headers,
    parse_retry_after,
    retry_delay_from_body,
    quota_detail,
)
from .types import ProviderConfig

# Failure kinds recorded on Attempt.kind.
KIND_AUTH = "auth"
KIND_RATE_LIMIT = "rate_limit"
KIND_DAILY_LIMIT = "daily_limit"
KIND_BILLING = "billing"
KIND_BAD_REQUEST = "bad_request"
KIND_TIMEOUT = "timeout"
KIND_TRANSIENT = "transient"
KIND_BAD_RESPONSE = "bad_response"

# Tunables — mirror the Go constants.
MAX_COOLDOWN = 5 * 60          # above this a 429 is treated as a daily limit
BASE_BACKOFF = 20              # first transient failure (seconds)
MAX_BACKOFF = 5 * 60           # backoff ceiling (seconds)
DEFAULT_RATE_WINDOW = 30       # 429 with no Retry-After hint (seconds)
DEFAULT_TOKEN_GUESS = 1500     # used to estimate TPM before the response arrives
DEFAULT_AUDIO_SEC_GUESS = 15   # used to estimate ASH/ASD before the response

# Strings that indicate a 400 body is misconfiguration, not the user's fault.
_CONFIG_BODY_KEYS = (
    "response_format", "json_mode", "system role", "system message",
    "developer instruction", "unknown model", "model_not_found",
    "invalid model", "not a valid model", "does not exist", "unsupported",
    "failed_precondition", "free tier is not available",
)
_DAILY_BODY_KEYS = (
    "perday", "per day", "daily", "per_day", "requests per day", "rpd",
)


def _is_config_body(low: str) -> bool:
    return any(k in low for k in _CONFIG_BODY_KEYS)


def _is_daily_body(low: str) -> bool:
    return any(k in low for k in _DAILY_BODY_KEYS)


def _backoff(streak: int) -> float:
    """``base * 2^streak`` capped at ``MAX_BACKOFF`` (seconds)."""
    d = BASE_BACKOFF
    for _ in range(streak):
        if d >= MAX_BACKOFF:
            break
        d *= 2
    return min(d, MAX_BACKOFF)


def _is_timeout(err: Exception) -> bool:
    """True when ``err`` is a requests timeout or DeadlineExceeded."""
    if isinstance(err, requests.Timeout):
        return True
    msg = str(err).lower()
    return "timed out" in msg or "timeout" in msg or "deadline" in msg


def _is_network_err(err: Exception) -> bool:
    msg = str(err).lower()
    return (
        isinstance(err, (requests.ConnectionError, requests.ChunkedEncodingError))
        or "connection" in msg
        or "eof" in msg
        or "no such host" in msg
        or "name or service not known" in msg
    )


@dataclass
class RequestHit:
    at: float
    tokens: int = 0


@dataclass
class AudioHit:
    at: float
    secs: int = 0


class BadResponseError(Exception):
    """A 200 that was unusable (empty text, JSON requested but not returned).

    The provider is healthy, so it gets no cooldown.
    """

    def __init__(self, msg: str) -> None:
        super().__init__(msg)
        self.msg = msg


class ProviderState:
    """Live (in-memory) counter state of one provider entry."""

    def __init__(self) -> None:
        self.mu = threading.Lock()
        self.hits: list[RequestHit] = []          # requests in the last 60s (rpm/tpm window)
        self.audio_hits: list[AudioHit] = []      # audio requests in the last 1h (ash window)
        self.day: str = ""
        self.requests_today: int = 0
        self.tokens_today: int = 0
        self.audio_sec_today: int = 0             # asd counter (resets at provider-local midnight)
        self.avg_audio_secs: int = 0              # running average of audio seconds per request
        self.cool_until: float = 0.0              # short pause (per-minute 429, upstream hiccup)
        self.dead_until: float = 0.0              # long pause (daily limit until reset, bad key)
        self.dead_reason: str = ""
        self.inflight: int = 0
        self.avg_tokens: int = 0
        self.bad400: int = 0                      # consecutive config-looking 400s
        self.fail_streak: int = 0                 # consecutive transient failures (drives backoff)
        self.upstream: Optional[RateInfo] = None  # last rate-limit headers the provider sent
        self.learned_rpd: int = 0                 # daily request limit the provider itself reported
        self.learned_tpm: int = 0                 # per-minute token limit the provider itself reported
        self.loaded: bool = False                 # pause state read from the store

    # ---- learning from provider-reported headers -------------------------
    def learn(self, ri: RateInfo, now: float, est_tokens: int) -> int:
        """Record what a provider reported about its own limits.

        Returns the daily-request counter delta so the caller can persist the
        correction. Caller must hold ``self.mu``.
        """
        self.upstream = ri
        delta = 0
        if ri.requests_window == "day" and ri.limit_requests > 0 and ri.remaining_requests >= 0:
            self.learned_rpd = ri.limit_requests
            want = ri.limit_requests - ri.remaining_requests + self.inflight
            if want != self.requests_today:
                delta = want - self.requests_today
                self.requests_today = want
        if ri.limit_tokens > 0:
            self.learned_tpm = ri.limit_tokens
        if ri.remaining_requests == 0 and ri.reset_requests > 0:
            if ri.reset_requests > MAX_COOLDOWN:
                self.dead_until = now + ri.reset_requests + 1
                self.dead_reason = "limit reached (reported by provider)"
            else:
                until = now + ri.reset_requests + 0.2
                if until > self.cool_until:
                    self.cool_until = until
        if (
            ri.remaining_tokens >= 0
            and ri.remaining_tokens < est_tokens
            and ri.reset_tokens > 0
            and ri.reset_tokens <= MAX_COOLDOWN
        ):
            until = now + ri.reset_tokens + 0.2
            if until > self.cool_until:
                self.cool_until = until
        return delta

    def eff_rpd(self, p: ProviderConfig) -> int:
        return self.learned_rpd or p.rpd

    def eff_tpm(self, p: ProviderConfig) -> int:
        return self.learned_tpm or p.tpm

    # ---- slot release ----------------------------------------------------
    def release(self, tokens: int) -> None:
        """Free the slot and book the real token usage once the response is in.

        Pass ``tokens=0`` for TTS/STT (no text tokens to record).
        """
        with self.mu:
            self.inflight -= 1
            if tokens > 0:
                if self.hits:
                    self.hits[-1].tokens = tokens
                self.tokens_today += tokens
                if self.avg_tokens == 0:
                    self.avg_tokens = tokens
                else:
                    self.avg_tokens = (self.avg_tokens * 7 + tokens * 3) // 10

    def release_audio(self, secs: int, at: float) -> None:
        """Book the real audio seconds used by a TTS or STT request."""
        if secs <= 0:
            return
        with self.mu:
            self.audio_sec_today += secs
            self.audio_hits.append(AudioHit(at, secs))
            if self.avg_audio_secs == 0:
                self.avg_audio_secs = secs
            else:
                self.avg_audio_secs = (self.avg_audio_secs * 7 + secs * 3) // 10

    def mark_success(self) -> None:
        with self.mu:
            self.fail_streak = 0
            self.bad400 = 0


# ---------------------------------------------------------------------------
# Helpers that work with timezones
# ---------------------------------------------------------------------------


def loc_for(tz: str):
    """Return a ZoneInfo for ``tz``, or UTC if unknown."""
    try:
        import zoneinfo
        return zoneinfo.ZoneInfo(tz)
    except Exception:
        import datetime as _dt
        return _dt.timezone.utc


def day_of(p: ProviderConfig, t: float) -> str:
    """Quota "day" of provider ``p`` at time ``t`` (unix seconds), in the provider's reset timezone."""
    import datetime as _dt
    loc = loc_for(p.reset_tz)
    return _dt.datetime.fromtimestamp(t, loc).strftime("%Y-%m-%d")


def until_reset(p: ProviderConfig, t: float) -> float:
    """Seconds left until ``p``'s daily quota resets (just after local midnight)."""
    import datetime as _dt
    loc = loc_for(p.reset_tz)
    now = _dt.datetime.fromtimestamp(t, loc)
    next_midnight = now.replace(hour=0, minute=1, second=0, microsecond=0) + _dt.timedelta(days=1)
    return (next_midnight - now).total_seconds()


def _max_int(a: int, b: int) -> int:
    return a if a > b else b


# ---------------------------------------------------------------------------
# classify() — turns one upstream failure into the right penalty
# ---------------------------------------------------------------------------


def classify_failure(
    *,
    err: Exception,
    cfg: ProviderConfig,
    state: ProviderState,
    now: float,
    logf=None,
) -> tuple[str, int]:
    """Turn an upstream failure into ``(kind, status)`` and apply the penalty.

    Penalty rules (mirror the Go ``classify``):

    * 401 / 403 / 404 — pause 24h (key or model id is wrong)
    * 402            — pause 1h (out of credits / billing)
    * 429 + daily hint — pause until the provider's local midnight
    * 429            — cool down for Retry-After / retryDelay (else 30s)
    * 400/413/422    — NO penalty (caller's request). 3 config-looking 400s
                       in a row pause 24h.
    * 408/5xx/timeout/network — exponential backoff 20s -> 40s -> ... -> 5min,
                                reset by the next success.
    * unusable 200   — no penalty.
    """
    from .types import HTTPError

    state.mu.acquire()
    try:
        # Non-HTTP errors (timeouts, network errors, BadResponseError).
        if not isinstance(err, HTTPError):
            if isinstance(err, BadResponseError):
                return KIND_BAD_RESPONSE, 0
            if _is_timeout(err):
                state.cool_until = now + _backoff(state.fail_streak)
                state.fail_streak += 1
                if logf:
                    logf("provider %s timed out; backing off until %s",
                         cfg.name, time.strftime("%H:%M:%S", time.localtime(state.cool_until)))
                return KIND_TIMEOUT, 0
            if cfg.kind == "ollama" or _is_network_err(err):
                state.cool_until = now + _backoff(state.fail_streak)
                state.fail_streak += 1
                return KIND_TRANSIENT, 0
            return KIND_BAD_RESPONSE, 0

        # HTTPError path.
        low = err.body.lower()
        status = err.code
        if status in (401, 403, 404):
            state.dead_until = now + 24 * 3600
            state.dead_reason = f"HTTP {status} (check key/model)"
            if logf:
                logf("provider %s paused for 24h: %s", cfg.name, err)
            return KIND_AUTH, status
        if status == 402:
            state.dead_until = now + 3600
            state.dead_reason = "HTTP 402 (out of credits / billing)"
            if logf:
                logf("provider %s paused for 1h: out of credits", cfg.name)
            return KIND_BILLING, status
        if status == 429:
            ra = err.retry_after
            if not ra:
                ra = retry_delay_from_body(err.body)
            if not ra and "1302" in low:
                # Z.AI concurrency limit — the single slot frees as soon as the
                # running call ends.
                ra = 2.0
            ri, _ = parse_rate_headers(err.headers or {}, now, cfg.base_url)
            header_daily = (
                ri.remaining_requests == 0
                and (
                    ri.reset_requests > MAX_COOLDOWN
                    or (ri.requests_window == "day" and ri.reset_requests > 0)
                )
            )
            g_daily, g_retry, g_ok = quota_detail(err.body)
            if not ra and g_retry > 0:
                ra = g_retry
            body_daily = g_daily or ((not g_ok) and _is_daily_body(low))
            if ra > MAX_COOLDOWN or body_daily or header_daily:
                wait = until_reset(cfg, now)  # the provider's own midnight (Gemini: Pacific)
                if header_daily and ri.reset_requests > 0:
                    wait = ri.reset_requests + 1
                state.dead_until = now + wait
                state.dead_reason = "daily limit"
                if logf:
                    logf("provider %s hit its daily limit; paused until reset", cfg.name)
                return KIND_DAILY_LIMIT, status
            # transient 429
            if ra <= 0:
                ra = DEFAULT_RATE_WINDOW
            if ra < 1:
                ra = 1
            state.cool_until = now + ra
            return KIND_RATE_LIMIT, status
        if status in (400, 413, 422):
            if status == 400 and _is_config_body(low):
                state.bad400 += 1
                if state.bad400 >= 3:
                    state.dead_until = now + 24 * 3600
                    state.dead_reason = "repeated HTTP 400 (check model / json_mode / system_role)"
            return KIND_BAD_REQUEST, status
        # 408, 425, 5xx, 529 and anything unexpected
        state.cool_until = now + _backoff(state.fail_streak)
        state.fail_streak += 1
        return KIND_TRANSIENT, status
    finally:
        # Persist long pauses so other processes / restarts respect them.
        if state.dead_until > now:
            try:
                # The caller wires the store in — we expose the values and let
                # the Router call SetPause if needed. We can't reach the store
                # from here without creating a circular dep, so we mark the
                # state and let the caller observe.
                pass
            finally:
                state.mu.release()
        else:
            state.mu.release()


def observe_rate_headers(
    *,
    headers: Optional[dict[str, str]],
    base_url: str,
    state: ProviderState,
    cfg: ProviderConfig,
    now: float,
    store=None,
    logf=None,
) -> None:
    """Record rate-limit headers a provider sent (on success OR failure).

    Persists any pause they imply and any daily-counter correction they
    require.
    """
    if not headers:
        return
    ri, ok = parse_rate_headers(headers, now, base_url)
    if not ok:
        return
    with state.mu:
        est = state.avg_tokens or DEFAULT_TOKEN_GUESS
        delta = state.learn(ri, now, est)
        until = state.dead_until
        why = state.dead_reason
    if delta and store is not None:
        # keep the stored daily total honest (shared keys, restarts)
        try:
            store.add_usage("_sync", cfg.id, day_of(cfg, now), delta, 0, 0)
        except Exception:
            pass
    if until > now and store is not None:
        try:
            store.set_pause(cfg.id, until, why)
        except Exception:
            pass


__all__ = [
    "ProviderState",
    "RequestHit",
    "AudioHit",
    "BadResponseError",
    "classify_failure",
    "observe_rate_headers",
    "day_of",
    "until_reset",
    "loc_for",
    "DEFAULT_TOKEN_GUESS",
    "DEFAULT_AUDIO_SEC_GUESS",
    "KIND_AUTH",
    "KIND_RATE_LIMIT",
    "KIND_DAILY_LIMIT",
    "KIND_BILLING",
    "KIND_BAD_REQUEST",
    "KIND_TIMEOUT",
    "KIND_TRANSIENT",
    "KIND_BAD_RESPONSE",
    "MAX_COOLDOWN",
    "BASE_BACKOFF",
    "MAX_BACKOFF",
    "DEFAULT_RATE_WINDOW",
]
