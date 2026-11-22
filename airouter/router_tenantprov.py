"""Tenant self-service: own models, keys, limits, Ollama.

Python port of ``pkg/router/tenantprov.go``. Every tenant can manage THEIR
OWN models, keys and limits: pick models from the preset catalog (or from
the Ollama server), paste their own API key, set their own limits, or
import a JSON file. Their providers are private (other tenants never see
or use them) and are tried before the shared pool.

Safety rules enforced here, because tenants are not admins:

* a tenant can only touch providers it owns (anything else is "not found");
* tenants never use env-var keys (that would spend the server's keys);
* the upstream URL comes from the preset, so a tenant cannot make the
  server call arbitrary hosts (SSRF), unless the operator opts in with
  ``TenantPolicy.allow_custom_base_url``;
* the number of providers and the worker count per provider are capped.
"""
from __future__ import annotations

from typing import Optional

import requests

from . import preset as _preset
from .router import TenantLimits, TenantPolicy
from .types import CATEGORY_TEXT, ErrNotFound, ProviderConfig

MAX_TENANT_WORKERS = 8

_ERR_NOT_YOURS = ErrNotFound()  # never reveal that someone else's provider exists


def _owned_provider(r, tenant_id: str, id: int) -> ProviderConfig:
    if not tenant_id:
        raise _ERR_NOT_YOURS
    ps = r.store.list_providers()
    for p in ps:
        if p.id == id and p.tenant_id == tenant_id:
            return p
    raise _ERR_NOT_YOURS


def _preset_for_url(base: str) -> str:
    base = (base or "").rstrip("/")
    for n in sorted(_preset.DEFINITIONS):
        if _preset.DEFINITIONS[n].base_url.rstrip("/") == base:
            return n
    return ""


def _prepare_tenant_provider(r, tenant_id: str, p: ProviderConfig) -> ProviderConfig:
    """Turn tenant input into a safe, owned ProviderConfig."""
    if not (tenant_id or "").strip():
        raise ValueError("tenant id is required")
    if (p.api_key_env or "").strip():
        raise ValueError("api_key_env is not allowed for tenants: put your own key in api_key")
    p.id = 0
    p.tenant_id = tenant_id
    p.api_key_env = ""
    if not p.preset and p.base_url:
        p.preset = _preset_for_url(p.base_url)
    if not p.preset and not r.tpolicy.allow_custom_base_url:
        raise ValueError(
            f"pick a provider from the list ({', '.join(_preset.names())}); custom URLs are disabled"
        )
    if p.preset:
        try:
            def_ = _preset.DEFINITIONS[p.preset]
        except KeyError as e:
            raise ValueError(
                f"unknown provider {p.preset!r} (have: {', '.join(_preset.names())})"
            ) from e
        if not r.tpolicy.allow_custom_base_url:
            p.kind = def_.kind
            p.base_url = def_.base_url
        if def_.kind == "ollama":
            if r.tpolicy.block_ollama:
                raise ValueError("this server does not let tenants use Ollama")
            if not r.tpolicy.allow_custom_base_url:
                p.base_url = r.ollama_url
        _preset.apply(p)
        p.api_key_env = ""
        p.preset = ""  # apply() re-adds the env var; tenants never get it
    if p.kind == "ollama" and r.tpolicy.block_ollama:
        raise ValueError("this server does not let tenants use Ollama")
    for name, v in (("rpm", p.rpm), ("rpd", p.rpd), ("tpm", p.tpm),
                    ("ash", p.ash), ("asd", p.asd),
                    ("workers", p.workers), ("priority", p.priority)):
        if v < 0:
            raise ValueError(f"{name} cannot be negative")
    if p.workers > MAX_TENANT_WORKERS:
        p.workers = MAX_TENANT_WORKERS
    p.api_key = (p.api_key or "").strip()
    return p


def _count_owned(r, tenant_id: str) -> tuple[int, set[str]]:
    ps = r.store.list_providers()
    n = 0
    names: set[str] = set()
    for p in ps:
        if p.tenant_id == tenant_id:
            n += 1
            names.add(p.name)
    return n, names


def add_tenant_provider(r, tenant_id: str, in_: ProviderConfig):
    """Add one model for a tenant. Returns the stored config (without the key)."""
    p = _prepare_tenant_provider(r, tenant_id, in_)
    if p.needs_key() and not p.api_key:
        raise ValueError(
            f"paste your own API key for {p.name} (it is stored encrypted and never shown again)"
        )
    n, names = _count_owned(r, tenant_id)
    if p.name in names:
        raise ValueError(f"you already have {p.name!r}; change its key or limits instead")
    if n >= r.tpolicy.max_providers:
        raise ValueError(f"limit reached: at most {r.tpolicy.max_providers} models per tenant")
    r.save_provider(p)
    p.api_key = ""
    return p


def import_tenant_providers(r, tenant_id: str, in_: list[ProviderConfig]):
    """Add many providers from a parsed JSON file. Each entry succeeds or fails on its own."""
    added: list[str] = []
    failed: dict[str, str] = {}
    for i, p in enumerate(in_):
        label = p.name or f"#{i + 1} {p.model}"
        try:
            out = add_tenant_provider(r, tenant_id, p)
        except Exception as e:
            failed[label] = str(e)
            continue
        added.append(out.name)
    return added, failed


def add_tenant_catalog(r, tenant_id: str, preset_name: str, api_key: str, models: list[str]):
    """Add models of one catalog provider for a tenant, all using the tenant's own key.

    ``models`` is a list of exact model ids; empty means every text model
    that provider has in the free pool.
    """
    if preset_name not in _preset.DEFINITIONS:
        raise ValueError(
            f"unknown provider {preset_name!r} (have: {', '.join(_preset.names())})"
        )
    all_ = not models
    want = set(models)
    added: list[str] = []
    skipped: list[str] = []
    for m in _preset.FREE_POOL:
        if m.preset != preset_name:
            continue
        if all_ and m.category != CATEGORY_TEXT:
            continue
        if not all_ and m.model not in want:
            continue
        cfg = m.to_provider_config()
        cfg.api_key_env = ""
        cfg.api_key = api_key
        want.discard(m.model)
        try:
            add_tenant_provider(r, tenant_id, cfg)
        except ValueError as e:
            if "paste your own API key" in str(e):
                return added, skipped, e
            skipped.append(f"{m.model}: {e}")
            continue
        added.append(cfg.name)
    if not all_:
        for m in list(want):
            skipped.append(f"{m}: not in the catalog for {preset_name} (use JSON import)")
            want.discard(m)
    return added, skipped, None


def update_tenant_limits(r, tenant_id: str, id: int, l: TenantLimits) -> None:
    p = _owned_provider(r, tenant_id, id)

    def _set(dst_attr: str, v: Optional[int]) -> None:
        if v is None:
            return
        if v < 0:
            raise ValueError("limits cannot be negative")
        setattr(p, dst_attr, v)

    _set("rpm", l.rpm)
    _set("rpd", l.rpd)
    _set("tpm", l.tpm)
    _set("workers", l.workers)
    _set("priority", l.priority)
    if p.workers > MAX_TENANT_WORKERS:
        p.workers = MAX_TENANT_WORKERS
    r.save_provider(p)


def set_tenant_provider_key(r, tenant_id: str, id: int, key: str) -> None:
    _owned_provider(r, tenant_id, id)
    r.set_provider_key(id, key)


def set_tenant_provider_disabled(r, tenant_id: str, id: int, disabled: bool) -> None:
    _owned_provider(r, tenant_id, id)
    r.set_disabled(id, disabled)


def reset_tenant_provider(r, tenant_id: str, id: int) -> None:
    _owned_provider(r, tenant_id, id)
    r.clear_pause(id)


def delete_tenant_provider(r, tenant_id: str, id: int) -> None:
    _owned_provider(r, tenant_id, id)
    r.delete_provider(id)


def list_tenant_providers(r, tenant_id: str):
    """Return ``(own, shared)``: the tenant's own models with live status, plus shared models (read-only)."""
    from .router_status import ProviderStatus, status

    own: list[ProviderStatus] = []
    shared: list[dict] = []
    for s in status(r, tenant_id):
        if s.tenant_id == tenant_id and tenant_id:
            own.append(s)
        elif not s.tenant_id:
            shared.append({
                "name": s.name, "model": s.model, "category": s.category, "state": s.state,
            })
    return own, shared


def export_tenant_providers(r, tenant_id: str) -> list[ProviderConfig]:
    ps = r.store.list_providers()
    out: list[ProviderConfig] = []
    for p in ps:
        if p.tenant_id == tenant_id and tenant_id:
            p.id = 0
            p.tenant_id = ""
            p.api_key = ""
            p.api_key_env = ""
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------


def ollama_models(r) -> list[dict]:
    """List models installed on the Ollama server (operator's URL, never tenant-supplied)."""
    try:
        resp = r.session.get(r.ollama_url.rstrip("/") + "/api/tags", timeout=3)
    except requests.RequestException as e:
        raise RuntimeError(f"Ollama is not reachable at {r.ollama_url}") from e
    if resp.status_code != 200:
        raise RuntimeError(f"unexpected reply from Ollama at {r.ollama_url}")
    try:
        payload = resp.json()
    except ValueError as e:
        raise RuntimeError(f"unexpected reply from Ollama at {r.ollama_url}") from e
    out: list[dict] = []
    for m in payload.get("models", []) or []:
        out.append({
            "name": m.get("name", ""),
            "parameter_size": ((m.get("details") or {}).get("parameter_size") or ""),
            "size_gb": f"{(m.get('size') or 0) / 1e9:.1f}",
        })
    return out


def add_tenant_ollama(r, tenant_id: str, models: list[str]):
    """Add installed Ollama models for a tenant (no key needed)."""
    added: list[str] = []
    skipped: list[str] = []
    for m in models:
        try:
            add_tenant_provider(r, tenant_id, ProviderConfig(
                preset="ollama", name=f"ollama-{m}", model=m, priority=90, workers=1,
            ))
        except Exception as e:
            skipped.append(f"{m}: {e}")
            continue
        added.append(f"ollama-{m}")
    return added, skipped


__all__ = [
    "MAX_TENANT_WORKERS",
    "add_tenant_provider",
    "import_tenant_providers",
    "add_tenant_catalog",
    "update_tenant_limits",
    "set_tenant_provider_key",
    "set_tenant_provider_disabled",
    "reset_tenant_provider",
    "delete_tenant_provider",
    "list_tenant_providers",
    "export_tenant_providers",
    "ollama_models",
    "add_tenant_ollama",
]
