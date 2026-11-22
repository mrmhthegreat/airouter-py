"""airouter — one-import way to use airouter from your own Python project.

Python port of the Go ``airouter.go`` facade. Free-tier AI providers behind
one call, with automatic failover, limit tracking and encrypted key storage.

Example::

    import airouter

    c = airouter.open(airouter.Options(db="ai.db"))

    # once: put your provider keys in the database (encrypted)
    c.add_free_pool({"GROQ_API_KEY": "gsk_...", "GEMINI_API_KEY": "..."})

    text = c.chat("Write one sentence about rain")

    # full control
    resp = c.ask(airouter.Request(system="Be brief", prompt="hi", json=True))

    # expose OpenAI API + web UI from your own server
    # c.handler(admin_token=os.environ["AIROUTER_ADMIN_TOKEN"])
"""
from __future__ import annotations

import os
from typing import Optional

from .router import (
    BootstrapOptions,
    Options,
    Router,
    TenantLimits,
    TenantPolicy,
    new,
)
from .store import (
    EncryptedStore,
    MemoryStore,
    SQLStore,
    Store,
    hash_key,
    master_secret,
    new_api_key,
    open_sqlite_store,
)
from .types import (
    Attempt,
    BadRequestError,
    ErrBadRequest,
    ErrNoProviders,
    ErrNotFound,
    ErrTenantDisabled,
    ErrTimeout,
    ErrUnauthorized,
    ErrUnknownTenant,
    ExhaustedError,
    AllFailedError,
    HTTPError,
    Message,
    ProviderConfig,
    Request,
    Response,
    SpeakRequest,
    SpeakResponse,
    Tenant,
    TenantLimitError,
    TranscribeRequest,
    TranscribeResponse,
)
from . import preset

__version__ = "0.1.0"


# ---------------------------------------------------------------------------
# Client + Open()
# ---------------------------------------------------------------------------


class Client:
    """A :class:`Router` plus conveniences.

    Every :class:`Router` method (``generate``, ``speak``, ``transcribe``,
    ``create_tenant``, ``status``, ``usage`` ...) is available on the client
    too.
    """

    def __init__(self, r: Router) -> None:
        self.router = r
        # Re-export every Router method on the client for convenience.
        for name in (
            "generate", "speak", "transcribe", "create_tenant", "authenticate",
            "update_tenant_profile", "rotate_tenant_key", "save_provider",
            "delete_provider", "clear_pause", "set_disabled", "set_provider_key",
            "bootstrap_providers", "bootstrap_providers_opts", "status", "usage",
            "list_tenant_providers", "export_tenant_providers",
            "add_tenant_provider", "import_tenant_providers",
            "add_tenant_catalog", "update_tenant_limits",
            "set_tenant_provider_key", "set_tenant_provider_disabled",
            "reset_tenant_provider", "delete_tenant_provider",
            "ollama_models", "add_tenant_ollama", "refresh_providers",
        ):
            setattr(self, name, getattr(r, name))

    @property
    def store(self) -> Store:
        return self.router.store

    @property
    def now(self):
        return self.router.now

    @property
    def session(self):
        return self.router.session

    @property
    def tpolicy(self) -> TenantPolicy:
        return self.router.tpolicy

    @property
    def ollama_url(self) -> str:
        return self.router.ollama_url

    @property
    def logf(self):
        return self.router.logf

    def _hydrate(self, p, st, now):
        return self.router._hydrate(p, st, now)

    def _acquire(self, p, st, now):
        return self.router._acquire(p, st, now)

    def _candidates(self, req):
        return self.router._candidates(req)

    def _snapshot(self):
        return self.router._snapshot()

    def _call_provider(self, p, msgs, req):
        return self.router._call_provider(p, msgs, req)

    def close(self) -> None:
        """Release the database."""
        self.store.close()

    def chat(self, prompt: str) -> str:
        """Send one prompt and return the answer text."""
        r = self.generate(Request(prompt=prompt))
        return r.text

    def ask(self, req: Request) -> Response:
        """:meth:`generate` with the full request/response (system prompt, JSON mode, ...)."""
        return self.generate(req)

    def add_free_pool(self, keys: dict[str, str], categories: Optional[list[str]] = None) -> tuple[list[str], list[str]]:
        """Import the curated free-tier catalog for every provider you supply a key for.

        ``keys`` is keyed by env var name (``"GROQ_API_KEY"``,
        ``"GEMINI_API_KEY"``, ``"OPENROUTER_API_KEY"``, ...). Keys are stored
        encrypted, so no environment variables are needed afterwards. Keys
        already set in the environment are used too.
        """
        return self.bootstrap_providers_opts(BootstrapOptions(
            keys=keys, store_keys=True, categories=categories,
        ))

    def handler(self, admin_token: str = ""):
        """Return the HTTP API: OpenAI-compatible ``/v1/chat/completions``, the native API, and the web UI at ``/``.

        ``admin_token`` enables admin endpoints (empty disables them).
        """
        from .server import new_handler
        return new_handler(self.router, admin_token)


def open(o: Options) -> Client:
    """Open the SQLite database and return a ready client.

    Provider keys are encrypted with ``$AIROUTER_SECRET`` or a key file next
    to the database.
    """
    if not o.store:
        db = o.db or os.environ.get("AIROUTER_DB", "") or "airouter.db"
        logf = o.logf
        st, _source = open_sqlite_store(db, logf)
        o.store = st
    return new_client(o.store, o)


def new_client(st: Store, o: Options) -> Client:
    """Build a client on any :class:`Store` (for example :class:`MemoryStore` in tests, or your own Postgres impl)."""
    ro = o
    ro.store = st
    r = new(ro)
    return Client(r)


__all__ = [
    "Client",
    "Options",
    "Router",
    "BootstrapOptions",
    "TenantLimits",
    "TenantPolicy",
    "open",
    "new_client",
    # stores
    "Store",
    "MemoryStore",
    "SQLStore",
    "EncryptedStore",
    "open_sqlite_store",
    "master_secret",
    "new_api_key",
    "hash_key",
    # types
    "Message",
    "Attempt",
    "Request",
    "Response",
    "SpeakRequest",
    "SpeakResponse",
    "TranscribeRequest",
    "TranscribeResponse",
    "ProviderConfig",
    "Tenant",
    # errors
    "BadRequestError",
    "ErrBadRequest",
    "ErrNoProviders",
    "ErrNotFound",
    "ErrTenantDisabled",
    "ErrTimeout",
    "ErrUnauthorized",
    "ErrUnknownTenant",
    "ExhaustedError",
    "AllFailedError",
    "HTTPError",
    "TenantLimitError",
    # subpackages
    "preset",
    "__version__",
]
