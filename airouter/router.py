"""The core router.

Python port of ``pkg/router/router.go`` and ``pkg/router/client.go``. The
router takes a :class:`~airouter.types.Request`, picks the best available
provider, sends the prompt, fails over automatically on errors or limits,
and returns the :class:`~airouter.types.Response`. Provider selection
respects per-provider per-model RPM/RPD/TPM limits, per-provider timezone
resets, and tenant-level daily caps. Long cooldowns (daily limit, bad key)
are persisted to the store so they survive restarts.
"""
from __future__ import annotations

import json as _json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import requests

from . import preset as _preset
from . import store as _store
from .limiter import (
    BadResponseError,
    DEFAULT_AUDIO_SEC_GUESS,
    DEFAULT_TOKEN_GUESS,
    KIND_BAD_REQUEST,
    ProviderState,
    RequestHit,
    classify_failure,
    day_of,
    loc_for,
    observe_rate_headers,
    until_reset,
)
from .ratelimit import parse_retry_after
from .store import Store, new_api_key, hash_key
from .types import (
    CATEGORY_TEXT,
    DEFAULT_TENANT_ID,
    AirouterError,
    AllFailedError,
    Attempt,
    BadRequestError,
    ErrBadRequest,
    ErrNoProviders,
    ErrTenantDisabled,
    ErrTimeout,
    ErrUnauthorized,
    ErrUnknownTenant,
    ExhaustedError,
    HTTPError,
    Message,
    ProviderConfig,
    Request,
    Response,
    Tenant,
    TenantLimitError,
    truncate,
)

# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


@dataclass
class Options:
    """Configure a new :class:`Router`."""

    store: Optional[Store] = None
    db: str = ""                    # SQLite file (used by airouter.open when store is None)
    strategy: str = ""              # "priority" (default) | "spread"
    max_wait: float = 20.0          # how long Generate may wait for a briefly-limited provider
    attempt_timeout: float = 60.0   # bounds ONE upstream call
    request_timeout: float = 120.0  # bounds a whole Generate call including every failover
    session: Optional[requests.Session] = None
    tenant_policy: "TenantPolicy" = None  # type: ignore[assignment]
    ollama_url: str = ""
    now: Callable[[], float] = field(default_factory=lambda: time.time)  # type: ignore[assignment]
    refresh_every: float = 5.0      # how often provider configs are re-read from the Store
    logf: Optional[Callable[..., None]] = None


# We can't use a dataclass default for `now` because time.time is callable.
# Provide a normal default in __post_init__ instead.


# ---------------------------------------------------------------------------
# Candidate
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    cfg: ProviderConfig
    state: ProviderState

    def fraction_used(self) -> float:
        with self.state.mu:
            if self.cfg.rpd > 0:
                return self.state.requests_today / float(self.cfg.rpd)
            return self.state.requests_today / 1000.0


# ---------------------------------------------------------------------------
# TenantPolicy (forward decl; full impl lives in tenantprov.py)
# ---------------------------------------------------------------------------


@dataclass
class TenantPolicy:
    """Limits what tenants may configure for themselves."""

    allow_custom_base_url: bool = False
    max_providers: int = 100
    block_ollama: bool = False


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class Router:
    """The main object. Construct once with :func:`new`, then call :meth:`generate` for each request.

    Safe for concurrent use.
    """

    def __init__(self, o: Options) -> None:
        if o.store is None:
            raise ValueError("airouter: Options.store is required")
        self.store = o.store
        self.strategy = o.strategy or "priority"
        self.max_wait = o.max_wait or 20.0
        self.attempt_timeout = o.attempt_timeout or 60.0
        self.request_timeout = o.request_timeout or 120.0
        self.session = o.session or requests.Session()
        self.now: Callable[[], float] = o.now if callable(o.now) else time.time
        self.refresh = o.refresh_every or 5.0
        self.tpolicy = o.tenant_policy or TenantPolicy()
        if self.tpolicy.max_providers == 0:
            self.tpolicy.max_providers = 100
        self.ollama_url = o.ollama_url
        if not self.ollama_url:
            self.ollama_url = __import__("os").environ.get("OLLAMA_HOST", "")
        if not self.ollama_url:
            self.ollama_url = _preset.DEFINITIONS["ollama"].base_url
        if "://" not in self.ollama_url:
            self.ollama_url = "http://" + self.ollama_url
        self.logf: Callable[..., None] = o.logf or (lambda *a, **kw: None)

        self._mu = threading.RLock()
        self._cands: list[Candidate] = []
        self._states: dict[int, ProviderState] = {}
        self._refreshed = 0.0

        # Always have a default tenant for CLI / admin use.
        if self.store.tenant_by_id(DEFAULT_TENANT_ID) is None:
            self.create_tenant(Tenant(id=DEFAULT_TENANT_ID, name="Default", tz="UTC"))
        self.refresh_providers()

    # ---- store / refresh -------------------------------------------------
    def refresh_providers(self) -> None:
        """Re-read provider configs from the store. Live counters for unchanged providers are preserved."""
        ps = self.store.list_providers()
        with self._mu:
            fresh: list[Candidate] = []
            for p in ps:
                st = self._states.get(p.id)
                if st is None:
                    st = ProviderState()
                    self._states[p.id] = st
                fresh.append(Candidate(p, st))
            self._cands = fresh
            self._refreshed = self.now()

    def _snapshot(self) -> list[Candidate]:
        with self._mu:
            stale = (self.now() - self._refreshed) > self.refresh
        if stale:
            self.refresh_providers()
        with self._mu:
            return list(self._cands)

    # ---- candidate filtering --------------------------------------------
    def _candidates(self, req: Request) -> list[Candidate]:
        category = req.category or CATEGORY_TEXT
        out: list[Candidate] = []
        for c in self._snapshot():
            if c.cfg.disabled or (c.cfg.tenant_id and c.cfg.tenant_id != req.tenant_id):
                continue
            cfg_cat = c.cfg.category or CATEGORY_TEXT
            if cfg_cat != category:
                continue
            if req.provider and c.cfg.name != req.provider:
                continue
            out.append(c)
        strategy = req.strategy or self.strategy
        # Sort: tenant-owned first, then by strategy, then by priority, then by id.
        out.sort(key=lambda c: (
            0 if c.cfg.tenant_id else 1,
            c.fraction_used() if strategy == "spread" else 0.0,
            c.cfg.priority,
            c.cfg.id,
        ))
        return out

    # ---- hydrate / acquire ----------------------------------------------
    def _hydrate(self, p: ProviderConfig, st: ProviderState, now: float) -> None:
        if not st.loaded:
            st.loaded = True
            until, why = self.store.get_pause(p.id)
            if until > now:
                st.dead_until = until
                st.dead_reason = why
        day = day_of(p, now)
        if st.day != day:
            # New day in the provider's timezone -> counters reset to whatever the
            # store has accumulated for the new day (usually 0).
            req, tok = self.store.provider_day_usage(p.id, day)
            st.day = day
            st.requests_today = req
            st.tokens_today = tok
            st.audio_sec_today = 0  # store doesn't persist audio seconds separately

    def _acquire(self, p: ProviderConfig, st: ProviderState, now: float) -> tuple[bool, float, bool, str]:
        """Check every limit and, if the provider can take a request now, reserve a slot.

        Returns ``(ok, wait_seconds, daily, reason)``.
        """
        with st.mu:
            self._hydrate(p, st, now)
            if now < st.dead_until:
                return False, st.dead_until - now, True, st.dead_reason
            rpd = st.eff_rpd(p)
            if rpd > 0 and st.requests_today >= rpd:
                return False, until_reset(p, now), True, "daily request limit reached"
            if now < st.cool_until:
                return False, st.cool_until - now, False, "cooling down"
            if st.inflight >= p.workers:
                return False, 0.25, False, "busy"
            # RPM: rolling 60-second window of request count.
            cutoff = now - 60
            kept = [h for h in st.hits if h.at > cutoff]
            st.hits = kept
            if p.rpm > 0 and len(st.hits) >= max(1, p.rpm * 9 // 10):
                wait = (st.hits[0].at + 60) - now
                return False, wait, False, "per-minute request limit"
            # TPM: rolling 60-second window of token count (only if TPM is set).
            if st.upstream and st.upstream.remaining_tokens >= 0 and st.upstream.reset_tokens > 0:
                est = st.avg_tokens or DEFAULT_TOKEN_GUESS
                clear = st.upstream.at + st.upstream.reset_tokens
                if now < clear and st.upstream.remaining_tokens < est + st.inflight * est:
                    return False, (clear - now) + 0.2, False, "provider reports no tokens left this minute"
            tpm = st.eff_tpm(p)
            if tpm > 0 and st.hits:
                est = st.avg_tokens or DEFAULT_TOKEN_GUESS
                sum_tok = sum(h.tokens for h in st.hits)
                if sum_tok + est > tpm * 9 // 10:
                    wait = (st.hits[0].at + 60) - now
                    return False, wait, False, "per-minute token limit"
            # ASH: rolling 1-hour window of audio seconds (TTS/STT only).
            if p.ash > 0:
                hour_cutoff = now - 3600
                kept_audio = [h for h in st.audio_hits if h.at > hour_cutoff]
                st.audio_hits = kept_audio
                sum_secs = sum(h.secs for h in kept_audio)
                est = st.avg_audio_secs or DEFAULT_AUDIO_SEC_GUESS
                if sum_secs + est > p.ash * 9 // 10:
                    if st.audio_hits:
                        wait = (st.audio_hits[0].at + 3600) - now
                    else:
                        wait = 3600
                    return False, wait, False, "per-hour audio seconds limit"
            # ASD: daily audio seconds total (TTS/STT only).
            if p.asd > 0:
                est = st.avg_audio_secs or DEFAULT_AUDIO_SEC_GUESS
                if st.audio_sec_today + est > p.asd * 9 // 10:
                    return False, until_reset(p, now), True, "daily audio seconds limit"
            # Reserve the slot. The est token count is refined in release() when
            # the real response arrives.
            est = st.avg_tokens or DEFAULT_TOKEN_GUESS
            st.inflight += 1
            st.requests_today += 1
            st.hits.append(RequestHit(at=now, tokens=est))
            return True, 0.0, False, ""

    # ---- generate --------------------------------------------------------
    def generate(self, req: Request) -> Response:
        """Pick the best available provider, send the request, fail over on errors."""
        start = self.now()
        if not req.tenant_id:
            req.tenant_id = DEFAULT_TENANT_ID
        tenant = self.store.tenant_by_id(req.tenant_id)
        if tenant is None:
            raise ErrUnknownTenant()
        if tenant.disabled:
            raise ErrTenantDisabled()
        if tenant.daily_request_limit > 0:
            loc = loc_for(tenant.tz)
            import datetime as _dt
            day = _dt.datetime.fromtimestamp(start, loc).strftime("%Y-%m-%d")
            used = self.store.tenant_day_usage(tenant.id, day)
            if used >= tenant.daily_request_limit:
                now = _dt.datetime.fromtimestamp(start, loc)
                next_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + _dt.timedelta(days=1)
                raise TenantLimitError((next_midnight - now).total_seconds())
        msgs = _build_messages(req)
        cands = self._candidates(req)
        if not cands:
            raise ErrNoProviders()

        attempts: list[Attempt] = []
        failed: set[int] = set()
        deadline = start + self.max_wait
        request_deadline = start + self.request_timeout

        while True:
            now = self.now()
            picked: Optional[Candidate] = None
            min_wait = float("inf")
            reasons: dict[str, str] = {}
            all_daily = True
            remaining = 0

            for c in cands:
                if c.cfg.id in failed:
                    continue
                remaining += 1
                if not c.cfg.has_key():
                    reasons[c.cfg.name] = f"no API key (set ${c.cfg.api_key_env} or store one: airouter provider set-key)"
                    continue
                ok, wait, daily, why = self._acquire(c.cfg, c.state, now)
                if ok:
                    picked = c
                    break
                reasons[c.cfg.name] = why
                if not daily:
                    all_daily = False
                if wait < min_wait:
                    min_wait = wait

            if picked is None:
                if remaining == 0 or (failed and all_daily):
                    raise _all_failed(attempts)
                if all_daily or (now + min_wait) > deadline or now > request_deadline:
                    if now > request_deadline:
                        raise ErrTimeout()
                    if min_wait == float("inf"):
                        min_wait = 3600
                    raise ExhaustedError(min_wait, reasons)
                # Wait briefly for the soonest provider.
                time.sleep(min(min_wait + 0.02, deadline - now))
                continue

            text, tokens, hdr, err = self._call_provider(picked.cfg, msgs, req)
            out: Optional[Response] = None
            if err is None:
                out = Response(
                    text=text,
                    provider=picked.cfg.name,
                    model=picked.cfg.model,
                    tokens=tokens,
                )
                if req.json:
                    j, ok = _extract_json(text)
                    if ok:
                        out.json = j
                    else:
                        err = BadResponseError("model did not return valid JSON")
                elif not text.strip():
                    err = BadResponseError("empty response")
            picked.state.release(tokens if err is None else 0)
            observe_rate_headers(
                headers=hdr, base_url=picked.cfg.base_url, state=picked.state,
                cfg=picked.cfg, now=now, store=self.store, logf=self.logf,
            )
            errs = 1 if err is not None else 0
            try:
                self.store.add_usage(req.tenant_id, picked.cfg.id, day_of(picked.cfg, now), 1, tokens if err is None else 0, errs)
            except Exception:
                pass
            if err is None:
                picked.state.mark_success()
                out.latency_ms = int((self.now() - start) * 1000)
                out.attempts = attempts
                return out
            if now > request_deadline:
                raise ErrTimeout()
            kind, status = classify_failure(
                err=err, cfg=picked.cfg, state=picked.state, now=now, logf=self.logf
            )
            # Persist long pauses so other processes / restarts respect them.
            if picked.state.dead_until > now:
                try:
                    self.store.set_pause(picked.cfg.id, picked.state.dead_until, picked.state.dead_reason)
                except Exception:
                    pass
            attempts.append(Attempt(provider=picked.cfg.name, error=str(err), status=status, kind=kind))
            failed.add(picked.cfg.id)

    # ---- speak -----------------------------------------------------------
    def speak(self, req) -> "Any":
        """Forward to the speak module. Implemented in :mod:`airouter.speak`."""
        from .speak import speak as _speak
        return _speak(self, req)

    # ---- transcribe ------------------------------------------------------
    def transcribe(self, req) -> "Any":
        """Forward to the transcribe module. Implemented in :mod:`airouter.transcribe`."""
        from .transcribe import transcribe as _transcribe
        return _transcribe(self, req)

    # ---- admin (forward decls; impl in router_admin.py) -----------------
    def create_tenant(self, t: Tenant) -> tuple[Tenant, str]:
        from .router_admin import create_tenant as _ct
        return _ct(self, t)

    def authenticate(self, key: str) -> Tenant:
        from .router_admin import authenticate as _au
        return _au(self, key)

    def update_tenant_profile(self, id: str, name: str, tz: str) -> Tenant:
        from .router_admin import update_tenant_profile as _ut
        return _ut(self, id, name, tz)

    def rotate_tenant_key(self, tenant_id: str) -> str:
        from .router_admin import rotate_tenant_key as _rt
        return _rt(self, tenant_id)

    def save_provider(self, p: ProviderConfig) -> None:
        from .router_admin import save_provider as _sp
        return _sp(self, p)

    def delete_provider(self, id: int) -> None:
        from .router_admin import delete_provider as _dp
        return _dp(self, id)

    def clear_pause(self, id: int) -> None:
        from .router_admin import clear_pause as _cp
        return _cp(self, id)

    def set_disabled(self, id: int, disabled: bool) -> None:
        from .router_admin import set_disabled as _sd
        return _sd(self, id, disabled)

    def set_provider_key(self, id: int, key: str) -> None:
        from .router_admin import set_provider_key as _sk
        return _sk(self, id, key)

    def bootstrap_providers(self, overwrite: bool = False, only_preset: str = "",
                            categories: Optional[list[str]] = None) -> tuple[list[str], list[str]]:
        return self.bootstrap_providers_opts(BootstrapOptions(
            overwrite=overwrite, preset=only_preset, categories=categories))

    def bootstrap_providers_opts(self, o: "BootstrapOptions") -> tuple[list[str], list[str]]:
        from .router_admin import bootstrap_providers_opts as _bp
        return _bp(self, o)

    # ---- status / usage (forward decls; impl in router_status.py) -------
    def status(self, tenant_id: str = "") -> list:
        from .router_status import status as _st
        return _st(self, tenant_id)

    def usage(self, tenant_id: str, day: str = ""):
        from .router_status import usage as _us
        return _us(self, tenant_id, day)

    # ---- tenantprov (forward decls; impl in router_tenantprov.py) -------
    def list_tenant_providers(self, tenant_id: str):
        from .router_tenantprov import list_tenant_providers as _lt
        return _lt(self, tenant_id)

    def export_tenant_providers(self, tenant_id: str) -> list[ProviderConfig]:
        from .router_tenantprov import export_tenant_providers as _et
        return _et(self, tenant_id)

    def add_tenant_provider(self, tenant_id: str, in_: ProviderConfig):
        from .router_tenantprov import add_tenant_provider as _at
        return _at(self, tenant_id, in_)

    def import_tenant_providers(self, tenant_id: str, in_: list[ProviderConfig]):
        from .router_tenantprov import import_tenant_providers as _it
        return _it(self, tenant_id, in_)

    def add_tenant_catalog(self, tenant_id: str, preset_name: str, api_key: str, models: list[str]):
        from .router_tenantprov import add_tenant_catalog as _ac
        return _ac(self, tenant_id, preset_name, api_key, models)

    def update_tenant_limits(self, tenant_id: str, id: int, l: "TenantLimits") -> None:
        from .router_tenantprov import update_tenant_limits as _ul
        return _ul(self, tenant_id, id, l)

    def set_tenant_provider_key(self, tenant_id: str, id: int, key: str) -> None:
        from .router_tenantprov import set_tenant_provider_key as _tk
        return _tk(self, tenant_id, id, key)

    def set_tenant_provider_disabled(self, tenant_id: str, id: int, disabled: bool) -> None:
        from .router_tenantprov import set_tenant_provider_disabled as _td
        return _td(self, tenant_id, id, disabled)

    def reset_tenant_provider(self, tenant_id: str, id: int) -> None:
        from .router_tenantprov import reset_tenant_provider as _rt
        return _rt(self, tenant_id, id)

    def delete_tenant_provider(self, tenant_id: str, id: int) -> None:
        from .router_tenantprov import delete_tenant_provider as _dt
        return _dt(self, tenant_id, id)

    def ollama_models(self) -> list:
        from .router_tenantprov import ollama_models as _om
        return _om(self)

    def add_tenant_ollama(self, tenant_id: str, models: list[str]):
        from .router_tenantprov import add_tenant_ollama as _ao
        return _ao(self, tenant_id, models)

    # ---- single upstream call (forward decl; impl in router_client.py) --
    def _call_provider(self, p: ProviderConfig, msgs: list[Message], req: Request):
        from .router_client import call_provider
        return call_provider(self, p, msgs, req)


# ---------------------------------------------------------------------------
# BootstrapOptions (forward decl; used by router_admin)
# ---------------------------------------------------------------------------


@dataclass
class BootstrapOptions:
    overwrite: bool = False
    preset: str = ""
    categories: Optional[list[str]] = None
    store_keys: bool = False
    keys: dict[str, str] = field(default_factory=dict)
    tenant_id: str = ""
    no_env: bool = False


@dataclass
class TenantLimits:
    rpm: Optional[int] = None
    rpd: Optional[int] = None
    tpm: Optional[int] = None
    workers: Optional[int] = None
    priority: Optional[int] = None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _build_messages(req: Request) -> list[Message]:
    msgs: list[Message] = []
    if req.system:
        msgs.append(Message(role="system", content=req.system))
    if req.messages:
        msgs.extend(req.messages)
    elif req.prompt:
        msgs.append(Message(role="user", content=req.prompt))
    if not any(m.role == "user" for m in msgs):
        raise BadRequestError("a prompt or at least one user message is required")
    return msgs


def _all_failed(attempts: list[Attempt]) -> AirouterError:
    """Build the right error once every candidate has failed.

    If every attempt was rejected as a bad request, that is the caller's
    fault (HTTP 400), not a provider outage (HTTP 502).
    """
    if attempts and all(a.kind == KIND_BAD_REQUEST for a in attempts):
        return BadRequestError(attempts[0].error, attempts)
    return AllFailedError(attempts)


def _extract_json(s: str) -> tuple[Any, bool]:
    """Pull a valid JSON value out of model output: handles ``` fences and surrounding chatter."""
    s = (s or "").strip()
    if s.startswith("```"):
        i = s.find("\n")
        if i >= 0:
            s = s[i + 1:]
        s = s.strip()
        if s.endswith("```"):
            s = s[:-3].strip()
    try:
        return _json.loads(s), True
    except ValueError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        i = s.find(opener)
        j = s.rfind(closer)
        if i >= 0 and j > i:
            try:
                return _json.loads(s[i:j + 1]), True
            except ValueError:
                pass
    return None, False


def new(o: Options) -> Router:
    """Construct a Router. Also ensures the default tenant exists and does an initial Refresh."""
    return Router(o)


__all__ = [
    "Router",
    "Options",
    "TenantPolicy",
    "TenantLimits",
    "BootstrapOptions",
    "Candidate",
    "new",
]
