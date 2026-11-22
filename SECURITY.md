# SECURITY.md — reporting security vulnerabilities in airouter-py

## Reporting a vulnerability

If you discover a security vulnerability in **airouter-py**, please report
it **privately** — do **not** open a public GitHub issue.

Email: **airouter-py-security@example.com** (replace with your real address)
or use GitHub's private vulnerability reporting:

  <https://github.com/mrmhthegreat/airouter-py/security/advisories/new>

Please include:

1. A description of the issue and its potential impact.
2. A minimal reproduction (code, HTTP requests, or step-by-step).
3. The version of airouter-py you tested (`airouter --help` shows it).
4. Whether the issue is in the library, the CLI, the HTTP server, or the
   data layer (SQLite store, encrypted keys, tenant auth).

You will receive an acknowledgment within 72 hours. We will coordinate a
fix and disclosure timeline with you.

## Scope

In scope:

* The Python source under `airouter/`.
* The HTTP server (`airouter/server.py`) — request handling, auth, tenant
  isolation, OpenAI-compatible endpoints.
* The encryption layer (`airouter/store.py`) — AES-256-GCM encryption of
  provider keys at rest, tenant key hashing.
* The CLI (`airouter/cli.py`) — argument parsing, secret input via stdin.
* The curated catalog (`airouter/preset.py`) — connection defaults.

Out of scope (by design):

* Vulnerabilities in upstream providers (Groq, Gemini, etc.) — report those
  to the provider.
* Vulnerabilities in dependencies (`requests`, `cryptography`, `fastapi`,
  `uvicorn`, `starlette`) — report those upstream. We will still pin and
  update affected versions, though.
* Issues that require already-compromised provider keys or a compromised
  `AIROUTER_SECRET` / `<db>.key` file (these are the user's secrets —
  compromise of them is not a library bug).

## Security posture

* **Tenant API keys** (`air_...`) are stored as **SHA-256 hashes only**. A
  database leak reveals no working tenant key.
* **Provider API keys** (Groq, Gemini, ...) must be sent upstream so they
  cannot be hashed — they are **AES-256-GCM encrypted at rest** with a key
  derived from `$AIROUTER_SECRET` (or a `0600` key file). A stolen database
  alone is useless without the master secret.
* **HTTP admin endpoints** use constant-time comparison against
  `$AIROUTER_ADMIN_TOKEN`. Tenant endpoints use a hashed-key lookup.
* **Tenant isolation**: a tenant can only touch providers it owns —
  `owned_provider()` returns `ErrNotFound` rather than revealing that
  someone else's provider exists.
* **SSRF protection**: tenants cannot aim the server at arbitrary URLs
  unless the operator opts in with `serve -allow-tenant-urls`. The upstream
  URL always comes from the preset catalog.

## Automated scanning

This project is scanned automatically by GitHub Actions (see
[`.github/workflows/security.yml`](.github/workflows/security.yml)):

* **pip-audit** — every dependency is checked against the PyPI advisory
  database + OSV for known CVEs.
* **bandit** — the `airouter/` source tree is scanned for common Python
  security issues (assert usage, hardcoded passwords, shell injection, etc.).
* **CodeQL** — GitHub's semantic analyzer runs the `security-and-quality`
  query suite on every push and PR.

Scan results appear as annotations on the relevant commit / PR.
