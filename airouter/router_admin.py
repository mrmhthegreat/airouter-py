"""Admin operations: tenants, providers, bootstrap.

Python port of ``pkg/router/admin.go``.
"""
from __future__ import annotations

import time
from typing import Optional

from . import preset as _preset
from .limiter import day_of
from .store import new_api_key, hash_key
from .types import (
    DEFAULT_TENANT_ID,
    ErrNotFound,
    ErrTenantDisabled,
    ErrUnauthorized,
    ErrUnknownTenant,
    ProviderConfig,
    Tenant,
)


def create_tenant(r, t: Tenant) -> tuple[Tenant, str]:
    """Validate and store a new tenant; return ``(tenant, api_key_shown_once)``."""
    if not (t.id or "").strip():
        raise ValueError("tenant id is required")
    if not t.tz:
        t.tz = "UTC"
    try:
        import zoneinfo
        zoneinfo.ZoneInfo(t.tz)
    except Exception as e:
        raise ValueError(f"unknown tz {t.tz!r}") from e
    if not t.name:
        t.name = t.id
    key, h = new_api_key()
    t.created_at = time.time()
    r.store.create_tenant(t, h)
    return t, key


def authenticate(r, key: str) -> Tenant:
    """Resolve a tenant from an API key."""
    t = r.store.tenant_by_key_hash(hash_key(key))
    if t is None:
        raise ErrUnauthorized()
    if t.disabled:
        raise ErrTenantDisabled()
    return t


def update_tenant_profile(r, id: str, name: str, tz: str) -> Tenant:
    t = r.store.tenant_by_id(id)
    if t is None:
        raise ErrUnknownTenant()
    name = (name or "").strip() or t.name
    if not tz:
        tz = t.tz
    try:
        import zoneinfo
        zoneinfo.ZoneInfo(tz)
    except Exception as e:
        raise ValueError(f"unknown tz {tz!r}") from e
    r.store.update_tenant_profile(id, name, tz)
    t.name = name
    t.tz = tz
    return t


def rotate_tenant_key(r, tenant_id: str) -> str:
    t = r.store.tenant_by_id(tenant_id)
    if t is None:
        raise ErrUnknownTenant()
    key, h = new_api_key()
    r.store.update_tenant_key_hash(tenant_id, h)
    return key


def set_provider_key(r, id: int, key: str) -> None:
    key = (key or "").strip()
    if not key:
        raise ValueError("api key is empty")
    ps = r.store.list_providers()
    for p in ps:
        if p.id == id:
            p.api_key = key
            return save_provider(r, p)
    raise ErrNotFound()


def save_provider(r, p: ProviderConfig) -> None:
    """Validate and store a provider.

    Applies a preset and the ``used_today_hint`` if given. Editing an existing
    provider lifts any pause on it (a new key or model should be tried
    immediately).
    """
    if p.preset:
        _preset.apply(p)
    p.validate()
    is_new = p.id == 0
    r.store.save_provider(p)
    if not is_new:
        try:
            clear_pause(r, p.id)
        except Exception:
            pass
    if is_new and p.used_today_hint > 0:
        try:
            r.store.add_usage("_seed", p.id, day_of(p, r.now()), p.used_today_hint, 0, 0)
        except Exception:
            pass
    r.refresh_providers()


def delete_provider(r, id: int) -> None:
    r.store.delete_provider(id)
    r.refresh_providers()


def clear_pause(r, id: int) -> None:
    """Lift any daily-limit / bad-key pause on a provider."""
    r.store.set_pause(id, 0.0, "")
    with r._mu:
        st = r._states.get(id)
    if st:
        with st.mu:
            st.dead_until = 0.0
            st.dead_reason = ""
            st.cool_until = 0.0
            st.bad400 = 0


def set_disabled(r, id: int, disabled: bool) -> None:
    ps = r.store.list_providers()
    for p in ps:
        if p.id == id:
            p.disabled = disabled
            r.store.save_provider(p)
            if not disabled:
                try:
                    clear_pause(r, id)
                except Exception:
                    pass
            r.refresh_providers()
            return
    raise ErrNotFound()


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def bootstrap_providers_opts(r, o) -> tuple[list[str], list[str]]:
    """Import entries from the preset free pool into the store.

    Skips entries whose env var is unset (so providers with no key don't get
    added with a broken state) and entries whose category is not in
    ``o.categories`` (defaults to chat-capable text models only).
    Returns the list of names added and skipped (with reasons).
    """
    from .router import BootstrapOptions
    overwrite, only_preset, categories = o.overwrite, o.preset, o.categories
    # Three cases:
    #   categories == None          -> default = text only (caller said nothing)
    #   categories == []            -> all categories (caller explicitly said "all")
    #   categories == ["text",...]  -> only those
    if categories is None:
        pool = _preset.free_pool_by_category(_preset.DEFAULT_BOOTSTRAP_CATEGORIES)
    else:
        pool = _preset.free_pool_by_category(categories)
    existing = r.store.list_providers()
    by_name: dict[str, ProviderConfig] = {f"{p.tenant_id}|{p.name}": p for p in existing}
    added: list[str] = []
    skipped: list[str] = []
    for m in pool:
        if only_preset and m.preset != only_preset:
            continue
        cfg = m.to_provider_config()
        if o.no_env and cfg.kind == "ollama":
            continue  # tenants add Ollama models through AddTenantOllama (which honours the policy)
        cfg.tenant_id = o.tenant_id
        env_name = cfg.api_key_env
        if o.tenant_id and cfg.api_key_env:
            k = (o.keys or {}).get(cfg.api_key_env, "").strip()
            if k:
                cfg.api_key = k
            elif not o.no_env:
                cfg.api_key = cfg.resolve_key()
            cfg.api_key_env = ""
        if cfg.api_key_env and (o.keys or {}).get(cfg.api_key_env, "").strip():
            cfg.api_key = (o.keys or {})[cfg.api_key_env].strip()
            cfg.api_key_env = ""
        # Skip providers that need an env var the user has not set, unless
        # they're Ollama (no key needed).
        if not cfg.has_key():
            skipped.append(f"{cfg.name} (no key: set ${env_name} or pass it in)")
            continue
        key = f"{cfg.tenant_id}|{cfg.name}"
        prev = by_name.get(key)
        if prev and not overwrite:
            skipped.append(f"{cfg.name} (already exists; use -overwrite)")
            continue
        if prev:
            cfg.id = prev.id
        if o.store_keys and cfg.api_key_env:
            cfg.api_key = cfg.resolve_key()
        try:
            save_provider(r, cfg)
        except Exception as e:
            skipped.append(f"{cfg.name} ({e})")
            continue
        added.append(cfg.name)
    return added, skipped


__all__ = [
    "create_tenant",
    "authenticate",
    "update_tenant_profile",
    "rotate_tenant_key",
    "set_provider_key",
    "save_provider",
    "delete_provider",
    "clear_pause",
    "set_disabled",
    "bootstrap_providers_opts",
]
