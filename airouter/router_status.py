"""Status and usage reports.

Python port of ``pkg/router/status.go``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .limiter import day_of, until_reset
from .ratelimit import RateInfo
from .types import CATEGORY_TEXT, ErrUnknownTenant


@dataclass
class ProviderStatus:
    """Live, human-readable state of one provider."""

    id: int = 0
    name: str = ""
    tenant_id: str = ""
    base_url: str = ""
    workers: int = 0
    kind: str = ""
    model: str = ""
    preset: str = ""
    category: str = ""
    priority: int = 0
    state: str = "ok"
    used_today: int = 0
    tokens_today: int = 0
    audio_sec_today: int = 0
    rpd: int = 0
    rpm: int = 0
    tpm: int = 0
    ash: int = 0
    asd: int = 0
    last_minute: int = 0
    reset_tz: str = ""
    resets_in_seconds: int = 0
    has_key: bool = False
    limits_source: str = "config"
    key_source: str = ""
    key_hint: str = ""
    upstream: Optional[RateInfo] = None
    remaining_today: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "workers": self.workers,
            "kind": self.kind,
            "model": self.model,
            "category": self.category,
            "priority": self.priority,
            "state": self.state,
            "used_today": self.used_today,
            "tokens_today": self.tokens_today,
            "audio_seconds_today": self.audio_sec_today,
            "rpd": self.rpd,
            "rpm": self.rpm,
            "tpm": self.tpm,
            "ash": self.ash,
            "asd": self.asd,
            "requests_last_minute": self.last_minute,
            "reset_tz": self.reset_tz,
            "resets_in_seconds": self.resets_in_seconds,
            "has_key": self.has_key,
            "limits_source": self.limits_source,
            "key_source": self.key_source,
        }
        if self.tenant_id:
            out["tenant_id"] = self.tenant_id
        if self.base_url:
            out["base_url"] = self.base_url
        if self.preset:
            out["preset"] = self.preset
        if self.key_hint:
            out["key_hint"] = self.key_hint
        if self.upstream:
            out["upstream"] = self.upstream.to_dict()
        if self.remaining_today is not None:
            out["remaining_today"] = self.remaining_today
        return out


@dataclass
class UsageRow:
    provider: str = ""
    model: str = ""
    day: str = ""
    tenant_requests: int = 0
    tenant_tokens: int = 0
    tenant_errors: int = 0
    all_requests: int = 0
    limit_per_day: int = 0
    resets_in_seconds: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "day": self.day,
            "tenant_requests": self.tenant_requests,
            "tenant_tokens": self.tenant_tokens,
            "tenant_errors": self.tenant_errors,
            "all_tenants_requests": self.all_requests,
            "limit_per_day": self.limit_per_day,
            "resets_in_seconds": self.resets_in_seconds,
        }


@dataclass
class UsageReport:
    tenant: str = ""
    requests_today: int = 0
    tokens_today: int = 0
    daily_limit: int = 0
    providers: list[UsageRow] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant": self.tenant,
            "requests_today": self.requests_today,
            "tokens_today": self.tokens_today,
            "tenant_daily_limit": self.daily_limit,
            "providers": [p.to_dict() for p in self.providers],
        }


def _limits_source(st) -> str:
    if st.learned_rpd > 0 or st.learned_tpm > 0:
        return "reported"
    return "config"


def status(r, tenant_id: str = "") -> list[ProviderStatus]:
    """Live status of every provider. Pass a tenant_id to see only its own providers plus shared ones."""
    now = r.now()
    out: list[ProviderStatus] = []
    for c in r._snapshot():
        if tenant_id and c.cfg.tenant_id and c.cfg.tenant_id != tenant_id:
            continue
        cfg, st = c.cfg, c.state
        with st.mu:
            r._hydrate(cfg, st, now)
            cat = cfg.category or CATEGORY_TEXT
            s = ProviderStatus(
                id=cfg.id, name=cfg.name, tenant_id=cfg.tenant_id, base_url=cfg.base_url,
                workers=cfg.workers, kind=cfg.kind, model=cfg.model, preset=cfg.preset,
                category=cat, priority=cfg.priority,
                used_today=st.requests_today,
                tokens_today=st.tokens_today,
                audio_sec_today=st.audio_sec_today,
                rpd=st.eff_rpd(cfg),
                limits_source=_limits_source(st),
                rpm=cfg.rpm,
                tpm=st.eff_tpm(cfg),
                ash=cfg.ash,
                asd=cfg.asd,
                reset_tz=cfg.reset_tz,
                resets_in_seconds=int(until_reset(cfg, now)),
                has_key=cfg.has_key(),
                key_source=cfg.key_source(),
                key_hint=cfg.key_hint(),
                upstream=st.upstream,
                state="ok",
            )
            s.last_minute = sum(1 for h in st.hits if (now - h.at) < 60)
            rpd = st.eff_rpd(cfg)
            if rpd > 0:
                s.remaining_today = max(0, rpd - st.requests_today)
            if cfg.disabled:
                s.state = "disabled"
            elif not s.has_key:
                s.state = "no api key"
            elif now < st.dead_until:
                s.state = "paused: " + st.dead_reason
            elif st.eff_rpd(cfg) > 0 and st.requests_today >= st.eff_rpd(cfg):
                s.state = "daily request limit reached"
            elif cfg.asd > 0 and st.audio_sec_today >= cfg.asd:
                s.state = "daily audio seconds limit reached"
            elif now < st.cool_until:
                s.state = "cooling down"
        out.append(s)
    return out


def usage(r, tenant_id: str, day: str = "") -> UsageReport:
    """Per-tenant usage report across every provider.

    Pass ``day=""`` for "today" (per provider, in that provider's timezone)
    or a specific YYYY-MM-DD for a historical day.
    """
    t = r.store.tenant_by_id(tenant_id)
    if t is None:
        raise ErrUnknownTenant()
    now = r.now()
    rep = UsageReport(tenant=t.id, daily_limit=t.daily_request_limit)
    for c in r._snapshot():
        if c.cfg.tenant_id and c.cfg.tenant_id != tenant_id:
            continue
        d = day or day_of(c.cfg, now)
        tr, tt, te = r.store.tenant_provider_usage(tenant_id, c.cfg.id, d)
        ar, _ = r.store.provider_day_usage(c.cfg.id, d)
        rep.requests_today += tr
        rep.tokens_today += tt
        rep.providers.append(UsageRow(
            provider=c.cfg.name, model=c.cfg.model, day=d,
            tenant_requests=tr, tenant_tokens=tt, tenant_errors=te,
            all_requests=ar, limit_per_day=c.state.eff_rpd(c.cfg),
            resets_in_seconds=int(until_reset(c.cfg, now)),
        ))
    return rep


__all__ = ["ProviderStatus", "UsageRow", "UsageReport", "status", "usage"]
