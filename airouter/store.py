"""Persistence layer for airouter.

Python port of ``pkg/store``. Provides:

* :class:`Store` — the persistence interface used by the router. Methods
  correspond to what the router actually needs (tenants, providers, pauses,
  usage). If you want Postgres or Redis, implement this interface.
* :class:`MemoryStore` — in-process implementation for tests / one-shot CLI
  runs.
* :class:`SQLStore` — SQLite-backed implementation using the stdlib
  :mod:`sqlite3` module.
* :class:`EncryptedStore` — wraps any store and transparently encrypts
  provider API keys at rest with AES-256-GCM.
* :func:`new_api_key` / :func:`hash_key` / :func:`master_secret` — the
  key-management helpers from ``secret.go``.

How secrets are kept (mirrors the Go original):

* Tenant API keys (``air_...``) are random 192-bit tokens. Only their SHA-256
  hash is stored; the key itself is shown once and cannot be recovered.
* Provider API keys (Groq, Gemini, ...) must be sent upstream, so they cannot
  be hashed. They are encrypted with AES-256-GCM before they reach the
  database and decrypted in memory.
* The master secret comes from ``$AIROUTER_SECRET``, or (if unset) from a
  ``0600`` key file created next to the database.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from typing import Optional

from .types import (
    DEFAULT_TENANT_ID,
    ErrNotFound,
    ProviderConfig,
    Tenant,
    parse_iso,
)

# ---------------------------------------------------------------------------
# Key management helpers
# ---------------------------------------------------------------------------


def new_api_key() -> tuple[str, str]:
    """Return a new random tenant key and its SHA-256 hash.

    The key format is ``air_<48 hex chars>`` — the ``air_`` prefix lets users
    grep for leaked keys in logs.
    """
    key = "air_" + secrets.token_hex(24)
    return key, hash_key(key)


def hash_key(key: str) -> str:
    """SHA-256 hex hash of a tenant key."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Store interface
# ---------------------------------------------------------------------------


class Store(ABC):
    """The persistence interface used by the router."""

    # ---- Tenants ---------------------------------------------------------
    @abstractmethod
    def create_tenant(self, t: Tenant, key_hash: str) -> None: ...
    @abstractmethod
    def tenant_by_id(self, id: str) -> Optional[Tenant]: ...
    @abstractmethod
    def tenant_by_key_hash(self, hash: str) -> Optional[Tenant]: ...
    @abstractmethod
    def list_tenants(self) -> list[Tenant]: ...
    @abstractmethod
    def update_tenant_profile(self, id: str, name: str, tz: str) -> None: ...
    @abstractmethod
    def update_tenant_key_hash(self, id: str, key_hash: str) -> None: ...

    # ---- Providers -------------------------------------------------------
    @abstractmethod
    def save_provider(self, p: ProviderConfig) -> None: ...
    @abstractmethod
    def list_providers(self) -> list[ProviderConfig]: ...
    @abstractmethod
    def delete_provider(self, id: int) -> None: ...

    # ---- Pauses ----------------------------------------------------------
    # A pause survives restarts and separate CLI runs: a provider that hit
    # its daily limit stays skipped until reset, even if the process restarts.
    @abstractmethod
    def set_pause(self, provider_id: int, until: float, reason: str) -> None: ...
    @abstractmethod
    def get_pause(self, provider_id: int) -> tuple[float, str]: ...

    # ---- Usage -----------------------------------------------------------
    @abstractmethod
    def add_usage(self, tenant_id: str, provider_id: int, day: str,
                  requests: int, tokens: int, errs: int) -> None: ...
    @abstractmethod
    def provider_day_usage(self, provider_id: int, day: str) -> tuple[int, int]: ...
    @abstractmethod
    def tenant_provider_usage(self, tenant_id: str, provider_id: int,
                              day: str) -> tuple[int, int, int]: ...
    @abstractmethod
    def tenant_day_usage(self, tenant_id: str, day: str) -> int: ...

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class MemoryStore(Store):
    """In-process implementation of :class:`Store`."""

    def __init__(self) -> None:
        self._mu = threading.RLock()
        self._tenants: dict[str, Tenant] = {}
        self._hashes: dict[str, str] = {}                # hash -> tenant id
        self._providers: dict[int, ProviderConfig] = {}
        self._next_id = 0
        self._usage: dict[tuple[str, int, str], tuple[int, int, int]] = {}
        self._pauses: dict[int, tuple[float, str]] = {}

    def create_tenant(self, t: Tenant, key_hash: str) -> None:
        with self._mu:
            if t.id in self._tenants:
                raise ValueError(f"tenant {t.id!r} already exists")
            if not t.created_at:
                t.created_at = time.time()
            self._tenants[t.id] = t
            self._hashes[key_hash] = t.id

    def update_tenant_profile(self, id: str, name: str, tz: str) -> None:
        with self._mu:
            t = self._tenants.get(id)
            if t is None:
                raise ErrNotFound()
            t.name = name
            t.tz = tz

    def update_tenant_key_hash(self, id: str, key_hash: str) -> None:
        with self._mu:
            if id not in self._tenants:
                raise ErrNotFound()
            # remove old hashes pointing at this tenant
            for h, tid in list(self._hashes.items()):
                if tid == id:
                    self._hashes.pop(h, None)
            self._hashes[key_hash] = id

    def tenant_by_id(self, id: str) -> Optional[Tenant]:
        with self._mu:
            t = self._tenants.get(id)
            return Tenant(**t.__dict__) if t else None  # copy

    def tenant_by_key_hash(self, hash: str) -> Optional[Tenant]:
        with self._mu:
            id = self._hashes.get(hash)
        if not id:
            return None
        return self.tenant_by_id(id)

    def list_tenants(self) -> list[Tenant]:
        with self._mu:
            out = [Tenant(**t.__dict__) for t in self._tenants.values()]
        out.sort(key=lambda t: t.id)
        return out

    def save_provider(self, p: ProviderConfig) -> None:
        with self._mu:
            for q in self._providers.values():
                if q.name == p.name and q.tenant_id == p.tenant_id and q.id != p.id:
                    raise ValueError(f"provider {p.name!r} already exists")
            if p.id == 0:
                self._next_id += 1
                p.id = self._next_id
            # store a shallow copy so callers can't mutate stored state
            self._providers[p.id] = _copy_provider(p)

    def list_providers(self) -> list[ProviderConfig]:
        with self._mu:
            out = [_copy_provider(p) for p in self._providers.values()]
        out.sort(key=lambda p: p.id)
        return out

    def delete_provider(self, id: int) -> None:
        with self._mu:
            self._providers.pop(id, None)

    def add_usage(self, tenant_id: str, provider_id: int, day: str,
                  requests: int, tokens: int, errs: int) -> None:
        with self._mu:
            k = (tenant_id, provider_id, day)
            r, t, e = self._usage.get(k, (0, 0, 0))
            self._usage[k] = (r + requests, t + tokens, e + errs)

    def provider_day_usage(self, provider_id: int, day: str) -> tuple[int, int]:
        with self._mu:
            r = t = 0
            for (tid, pid, d), (req, tok, _) in self._usage.items():
                if pid == provider_id and d == day:
                    r += req
                    t += tok
            return r, t

    def tenant_provider_usage(self, tenant_id: str, provider_id: int,
                              day: str) -> tuple[int, int, int]:
        with self._mu:
            return self._usage.get((tenant_id, provider_id, day), (0, 0, 0))

    def tenant_day_usage(self, tenant_id: str, day: str) -> int:
        with self._mu:
            n = 0
            for (tid, _pid, d), (req, _, _) in self._usage.items():
                if tid == tenant_id and d == day:
                    n += req
            return n

    def set_pause(self, provider_id: int, until: float, reason: str) -> None:
        with self._mu:
            if until <= 0:
                self._pauses.pop(provider_id, None)
            else:
                self._pauses[provider_id] = (until, reason)

    def get_pause(self, provider_id: int) -> tuple[float, str]:
        with self._mu:
            return self._pauses.get(provider_id, (0.0, ""))


def _copy_provider(p: ProviderConfig) -> ProviderConfig:
    """Deep-enough copy: ProviderConfig is a dataclass with simple fields."""
    import dataclasses
    return dataclasses.replace(p)


# ---------------------------------------------------------------------------
# SQLite implementation
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, api_key_hash TEXT NOT NULL UNIQUE,
  tz TEXT NOT NULL DEFAULT 'UTC', daily_request_limit INTEGER NOT NULL DEFAULT 0,
  disabled INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS providers(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, tenant_id TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL, base_url TEXT NOT NULL, api_key_env TEXT NOT NULL DEFAULT '', api_key TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 100,
  rpm INTEGER NOT NULL DEFAULT 0, rpd INTEGER NOT NULL DEFAULT 0, tpm INTEGER NOT NULL DEFAULT 0,
  ash INTEGER NOT NULL DEFAULT 0, asd INTEGER NOT NULL DEFAULT 0,
  workers INTEGER NOT NULL DEFAULT 2, reset_tz TEXT NOT NULL DEFAULT 'UTC',
  json_mode INTEGER, system_role INTEGER, disabled INTEGER NOT NULL DEFAULT 0,
  category TEXT NOT NULL DEFAULT 'text',
  UNIQUE(tenant_id, name));
CREATE TABLE IF NOT EXISTS usage(
  tenant_id TEXT NOT NULL, provider_id INTEGER NOT NULL, day TEXT NOT NULL,
  requests INTEGER NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(tenant_id, provider_id, day));
CREATE INDEX IF NOT EXISTS usage_provider_day ON usage(provider_id, day);
CREATE TABLE IF NOT EXISTS provider_pause(
  provider_id INTEGER PRIMARY KEY, until TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '');
"""


class SQLStore(Store):
    """SQLite-backed implementation of :class:`Store`."""

    def __init__(self, path: str) -> None:
        # check_same_thread=False lets the router call from any worker thread.
        # We guard writes with our own lock so SQLite sees one writer at a time.
        self._path = path
        self._mu = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._migrate_columns()

    def _migrate_columns(self) -> None:
        existing = {row["name"] for row in self._db.execute("PRAGMA table_info(providers)")}
        for col, definition in (
            ("category", "TEXT NOT NULL DEFAULT 'text'"),
            ("ash", "INTEGER NOT NULL DEFAULT 0"),
            ("asd", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in existing:
                self._db.execute(f"ALTER TABLE providers ADD COLUMN {col} {definition}")
        self._db.commit()

    def close(self) -> None:
        with self._mu:
            self._db.close()

    # ---- tenants ---------------------------------------------------------
    def create_tenant(self, t: Tenant, key_hash: str) -> None:
        if not t.created_at:
            t.created_at = time.time()
        with self._mu:
            self._db.execute(
                "INSERT INTO tenants(id,name,api_key_hash,tz,daily_request_limit,disabled,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (t.id, t.name, key_hash, t.tz, t.daily_request_limit,
                 1 if t.disabled else 0, _iso(t.created_at)),
            )
            self._db.commit()

    def update_tenant_profile(self, id: str, name: str, tz: str) -> None:
        with self._mu:
            cur = self._db.execute(
                "UPDATE tenants SET name=?, tz=? WHERE id=?", (name, tz, id)
            )
            self._db.commit()
            if cur.rowcount == 0:
                raise ErrNotFound()

    def update_tenant_key_hash(self, id: str, key_hash: str) -> None:
        with self._mu:
            cur = self._db.execute(
                "UPDATE tenants SET api_key_hash=? WHERE id=?", (key_hash, id)
            )
            self._db.commit()
            if cur.rowcount == 0:
                raise ErrNotFound()

    def _scan_tenant(self, row: sqlite3.Row) -> Optional[Tenant]:
        if row is None:
            return None
        return Tenant(
            id=row["id"],
            name=row["name"],
            tz=row["tz"],
            daily_request_limit=row["daily_request_limit"],
            disabled=bool(row["disabled"]),
            created_at=parse_iso(row["created_at"]),
        )

    def tenant_by_id(self, id: str) -> Optional[Tenant]:
        with self._mu:
            row = self._db.execute(
                "SELECT id,name,tz,daily_request_limit,disabled,created_at FROM tenants WHERE id=?",
                (id,),
            ).fetchone()
            return self._scan_tenant(row)

    def tenant_by_key_hash(self, hash: str) -> Optional[Tenant]:
        with self._mu:
            row = self._db.execute(
                "SELECT id,name,tz,daily_request_limit,disabled,created_at FROM tenants WHERE api_key_hash=?",
                (hash,),
            ).fetchone()
            return self._scan_tenant(row)

    def list_tenants(self) -> list[Tenant]:
        with self._mu:
            rows = self._db.execute(
                "SELECT id,name,tz,daily_request_limit,disabled,created_at FROM tenants ORDER BY id"
            ).fetchall()
            return [self._scan_tenant(r) for r in rows]  # type: ignore[list-item]

    # ---- providers -------------------------------------------------------
    def save_provider(self, p: ProviderConfig) -> None:
        if not p.category:
            p.category = "text"
        jm = None if p.json_mode is None else (1 if p.json_mode else 0)
        sr = None if p.system_role is None else (1 if p.system_role else 0)
        dis = 1 if p.disabled else 0
        args = (
            p.name, p.tenant_id, p.kind, p.base_url, p.api_key_env, p.api_key, p.model,
            p.priority, p.rpm, p.rpd, p.tpm, p.ash, p.asd, p.workers, p.reset_tz,
            jm, sr, dis, p.category,
        )
        with self._mu:
            if p.id == 0:
                cur = self._db.execute(
                    "INSERT INTO providers(name,tenant_id,kind,base_url,api_key_env,api_key,"
                    "model,priority,rpm,rpd,tpm,ash,asd,workers,reset_tz,json_mode,system_role,"
                    "disabled,category) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    args,
                )
                p.id = cur.lastrowid or 0
            else:
                self._db.execute(
                    "UPDATE providers SET name=?,tenant_id=?,kind=?,base_url=?,api_key_env=?,"
                    "api_key=?,model=?,priority=?,rpm=?,rpd=?,tpm=?,ash=?,asd=?,workers=?,"
                    "reset_tz=?,json_mode=?,system_role=?,disabled=?,category=? WHERE id=?",
                    args + (p.id,),
                )
            self._db.commit()

    def list_providers(self) -> list[ProviderConfig]:
        with self._mu:
            rows = self._db.execute(
                "SELECT id,name,tenant_id,kind,base_url,api_key_env,api_key,model,priority,"
                "rpm,rpd,tpm,ash,asd,workers,reset_tz,json_mode,system_role,disabled,category "
                "FROM providers ORDER BY id"
            ).fetchall()
            out = []
            for r in rows:
                jm = None if r["json_mode"] is None else bool(r["json_mode"])
                sr = None if r["system_role"] is None else bool(r["system_role"])
                cat = r["category"] or "text"
                out.append(ProviderConfig(
                    id=r["id"], name=r["name"], tenant_id=r["tenant_id"], kind=r["kind"],
                    base_url=r["base_url"], api_key_env=r["api_key_env"], api_key=r["api_key"],
                    model=r["model"], priority=r["priority"], rpm=r["rpm"], rpd=r["rpd"],
                    tpm=r["tpm"], ash=r["ash"], asd=r["asd"], workers=r["workers"],
                    reset_tz=r["reset_tz"], json_mode=jm, system_role=sr,
                    disabled=bool(r["disabled"]), category=cat,
                ))
            return out

    def delete_provider(self, id: int) -> None:
        with self._mu:
            self._db.execute("DELETE FROM providers WHERE id=?", (id,))
            self._db.commit()

    # ---- pauses ----------------------------------------------------------
    def set_pause(self, provider_id: int, until: float, reason: str) -> None:
        with self._mu:
            if until <= 0:
                self._db.execute("DELETE FROM provider_pause WHERE provider_id=?", (provider_id,))
            else:
                self._db.execute(
                    "INSERT INTO provider_pause(provider_id,until,reason) VALUES(?,?,?) "
                    "ON CONFLICT(provider_id) DO UPDATE SET until=excluded.until, reason=excluded.reason",
                    (provider_id, _iso(until), reason),
                )
            self._db.commit()

    def get_pause(self, provider_id: int) -> tuple[float, str]:
        with self._mu:
            row = self._db.execute(
                "SELECT until, reason FROM provider_pause WHERE provider_id=?",
                (provider_id,),
            ).fetchone()
        if row is None:
            return 0.0, ""
        return parse_iso(row["until"]), row["reason"] or ""

    # ---- usage -----------------------------------------------------------
    def add_usage(self, tenant_id: str, provider_id: int, day: str,
                  requests: int, tokens: int, errs: int) -> None:
        with self._mu:
            self._db.execute(
                "INSERT INTO usage(tenant_id,provider_id,day,requests,tokens,errors) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(tenant_id,provider_id,day) DO UPDATE "
                "SET requests=requests+excluded.requests, tokens=tokens+excluded.tokens, "
                "errors=errors+excluded.errors",
                (tenant_id, provider_id, day, requests, tokens, errs),
            )
            self._db.commit()

    def provider_day_usage(self, provider_id: int, day: str) -> tuple[int, int]:
        with self._mu:
            row = self._db.execute(
                "SELECT COALESCE(SUM(requests),0), COALESCE(SUM(tokens),0) "
                "FROM usage WHERE provider_id=? AND day=?",
                (provider_id, day),
            ).fetchone()
        return int(row[0] or 0), int(row[1] or 0)

    def tenant_provider_usage(self, tenant_id: str, provider_id: int,
                              day: str) -> tuple[int, int, int]:
        with self._mu:
            row = self._db.execute(
                "SELECT COALESCE(SUM(requests),0), COALESCE(SUM(tokens),0), COALESCE(SUM(errors),0) "
                "FROM usage WHERE tenant_id=? AND provider_id=? AND day=?",
                (tenant_id, provider_id, day),
            ).fetchone()
        return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)

    def tenant_day_usage(self, tenant_id: str, day: str) -> int:
        with self._mu:
            row = self._db.execute(
                "SELECT COALESCE(SUM(requests),0) FROM usage WHERE tenant_id=? AND day=?",
                (tenant_id, day),
            ).fetchone()
        return int(row[0] or 0)


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


# ---------------------------------------------------------------------------
# EncryptedStore wrapper
# ---------------------------------------------------------------------------

ENC_PREFIX = "enc:v1:"

# Standard AES-GCM nonce length (96 bits = 12 bytes). cryptography's AESGCM
# class does not expose this as an attribute, so we hard-code the standard.
GCM_NONCE_SIZE = 12


def _aead_from_secret(secret: bytes):
    """Build an AES-256-GCM cipher from an arbitrary byte secret.

    The secret is stretched to an AES-256 key with SHA-256, salted with a
    fixed domain string (same as the Go original).
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = hashlib.sha256(b"airouter/provider-key/v1:" + secret).digest()
    return AESGCM(key)


class EncryptedStore(Store):
    """Wrap any :class:`Store` so provider API keys are encrypted at rest.

    Everything else passes straight through.
    """

    def __init__(self, inner: Store, secret: bytes) -> None:
        if not secret:
            raise ValueError("store: empty encryption secret")
        self._inner = inner
        self._aead = _aead_from_secret(secret)

    def _seal(self, plain: str) -> str:
        if not plain or plain.startswith(ENC_PREFIX):
            return plain
        nonce = secrets.token_bytes(GCM_NONCE_SIZE)
        ct = self._aead.encrypt(nonce, plain.encode("utf-8"), None)
        import base64
        return ENC_PREFIX + base64.b64encode(nonce + ct).decode("ascii")

    def _open(self, stored: str) -> str:
        if not stored.startswith(ENC_PREFIX):
            return stored  # legacy plaintext; EncryptExisting will upgrade it
        import base64
        raw = base64.b64decode(stored[len(ENC_PREFIX):])
        n = GCM_NONCE_SIZE
        if len(raw) < n:
            raise ValueError("store: corrupt encrypted provider key")
        try:
            pt = self._aead.decrypt(raw[:n], raw[n:], None)
        except Exception as e:
            raise ValueError(
                "store: cannot decrypt provider key — wrong AIROUTER_SECRET / key file for this database?"
            ) from e
        return pt.decode("utf-8")

    # transparent encryption on save / list --------------------------------
    def save_provider(self, p: ProviderConfig) -> None:
        cp = _copy_provider(p)
        cp.api_key = self._seal(cp.api_key)
        self._inner.save_provider(cp)
        p.id = cp.id

    def list_providers(self) -> list[ProviderConfig]:
        ps = self._inner.list_providers()
        for p in ps:
            p.api_key = self._open(p.api_key)
        return ps

    # passthrough ----------------------------------------------------------
    def create_tenant(self, t: Tenant, key_hash: str) -> None:
        return self._inner.create_tenant(t, key_hash)

    def update_tenant_profile(self, id: str, name: str, tz: str) -> None:
        return self._inner.update_tenant_profile(id, name, tz)

    def update_tenant_key_hash(self, id: str, key_hash: str) -> None:
        return self._inner.update_tenant_key_hash(id, key_hash)

    def tenant_by_id(self, id: str) -> Optional[Tenant]:
        return self._inner.tenant_by_id(id)

    def tenant_by_key_hash(self, hash: str) -> Optional[Tenant]:
        return self._inner.tenant_by_key_hash(hash)

    def list_tenants(self) -> list[Tenant]:
        return self._inner.list_tenants()

    def delete_provider(self, id: int) -> None:
        return self._inner.delete_provider(id)

    def set_pause(self, provider_id: int, until: float, reason: str) -> None:
        return self._inner.set_pause(provider_id, until, reason)

    def get_pause(self, provider_id: int) -> tuple[float, str]:
        return self._inner.get_pause(provider_id)

    def add_usage(self, tenant_id: str, provider_id: int, day: str,
                  requests: int, tokens: int, errs: int) -> None:
        return self._inner.add_usage(tenant_id, provider_id, day, requests, tokens, errs)

    def provider_day_usage(self, provider_id: int, day: str) -> tuple[int, int]:
        return self._inner.provider_day_usage(provider_id, day)

    def tenant_provider_usage(self, tenant_id: str, provider_id: int,
                              day: str) -> tuple[int, int, int]:
        return self._inner.tenant_provider_usage(tenant_id, provider_id, day)

    def tenant_day_usage(self, tenant_id: str, day: str) -> int:
        return self._inner.tenant_day_usage(tenant_id, day)

    def close(self) -> None:
        return self._inner.close()

    # re-encrypt legacy plaintext keys -------------------------------------
    def encrypt_existing(self) -> int:
        """Re-save any provider whose key is still plaintext.

        Safe to call on every start. Returns how many rows it upgraded.
        """
        # Bypass our own decryption by reading from the inner store directly.
        ps = self._inner.list_providers()
        n = 0
        for p in ps:
            if not p.api_key or p.api_key.startswith(ENC_PREFIX):
                continue
            p.api_key = self._seal(p.api_key)
            self._inner.save_provider(p)
            n += 1
        return n


def master_secret(key_file: str) -> tuple[bytes, str]:
    """Find the encryption secret.

    Order:

    1. ``$AIROUTER_SECRET`` if set.
    2. The contents of ``key_file`` if it exists (mode ``0600``).
    3. A new random 32-byte hex string written to ``key_file`` with mode
       ``0600``.

    ``source`` describes where it came from (safe to log). Never log the
    secret itself.
    """
    v = os.environ.get("AIROUTER_SECRET", "").strip()
    if v:
        return v.encode("utf-8"), "$AIROUTER_SECRET"
    try:
        with open(key_file, "r", encoding="utf-8") as f:
            s = f.read().strip()
        if s:
            return s.encode("utf-8"), key_file
    except FileNotFoundError:
        pass
    raw = secrets.token_bytes(32)
    s = raw.hex()
    # Create with restrictive perms (best-effort on Windows)
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (s + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return s.encode("utf-8"), key_file + " (new)"


def open_sqlite_store(path: str, logf=None) -> tuple[EncryptedStore, str]:
    """Open the SQLite database at ``path`` as an :class:`EncryptedStore`.

    The secret is ``$AIROUTER_SECRET`` or ``<path>.key`` (mode 0600, created
    on first use). Keys written by older versions in plaintext are encrypted
    on first open.
    """
    base = SQLStore(path)
    secret, source = master_secret(path + ".key")
    enc = EncryptedStore(base, secret)
    n = enc.encrypt_existing()
    if n > 0 and logf is not None:
        logf("encrypted %d provider key(s) that were stored in plaintext", n)
    return enc, source


__all__ = [
    "Store",
    "MemoryStore",
    "SQLStore",
    "EncryptedStore",
    "SCHEMA",
    "new_api_key",
    "hash_key",
    "master_secret",
    "open_sqlite_store",
]
