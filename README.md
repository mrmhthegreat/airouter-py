# airouter (Python port)

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](./LICENSE)
[![PyPI version](https://img.shields.io/pypi/v/airouter-py.svg)](https://pypi.org/project/airouter-py/)

> One Python package + CLI that routes your prompt across many free-tier AI
> providers (Groq, Gemini, Gemma, OpenRouter, Z.AI GLM, Mistral, SambaNova,
> Ollama, any OpenAI-compatible URL) and **auto-switches when one hits its
> limit**. Supports **text chat**, **text-to-speech (TTS)**, and
> **speech-to-text (STT)** — same failover behaviour for all three.

This is a faithful Python port of the Go project at
[github.com/mrmhthegreat/airouter](https://github.com/mrmhthegreat/airouter).
Every feature of the original is reproduced: per-provider per-model limits,
timezone-aware daily resets, three flavours of cooldown, SQLite persistence
with AES-256-GCM-encrypted provider keys, the curated free-pool catalog,
tenants with hashed API keys, JSON mode, the OpenAI-compatible HTTP API,
the web UI, the same CLI subcommands.


`airouter` is for: hobby projects, prototypes, internal tools, hackathons,
and anyone who wants free LLM access without managing API keys in every
script. Configure it once with the providers you have free keys for, then
send prompts from the CLI, the HTTP server, or any Python program. The
router picks the best available provider, sends the request, and on a
429 / 5xx / bad-key error fails over to the next one before your code
even notices.

## Three ways to use airouter

You can use any combination of these — they share the same SQLite DB file,
so provider configs, encrypted keys, daily-limit pauses, and usage counters
are consistent across all three.

### 1. As a published Python package (embedded library)

```bash
pip install airouter-py         # once published; or: pip install -e . from a clone
pip install airouter-py[server] # adds FastAPI / uvicorn for the HTTP server
```

```python
import airouter

c = airouter.open(airouter.Options(db="ai.db"))   # keys encrypted at rest
c.add_free_pool({"GROQ_API_KEY": "gsk_..."})      # one-time seed

text = c.chat("Write one sentence about rain.")   # auto-failover happens inside

# full control
resp = c.ask(airouter.Request(system="Be brief", prompt="hi", json=True))
print(resp.text, resp.provider, resp.model, resp.tokens, resp.attempts)
```

All request types are available: `c.generate()`, `c.speak()` (TTS),
`c.transcribe()` (STT). See [docs/EMBEDDED_USAGE.md](docs/EMBEDDED_USAGE.md)
for the full API including multi-turn conversations, custom Stores, and
typed error handling.

### 2. As a CLI

```bash
pip install airouter-py         # installs the `airouter` script on your PATH
airouter bootstrap              # seed the DB from the curated free-pool catalog
airouter provider list          # live status
airouter ask "Write one sentence about rain"
airouter speak -o hello.wav "Hello world"
airouter transcribe recording.mp3
airouter serve -addr :8080      # run the HTTP API + web UI
```

Every subcommand documented in [docs/CLI_CHEATSHEET.md](docs/CLI_CHEATSHEET.md).

### 3. As an HTTP server (standalone or mounted inside your own app)

**Standalone:**

```bash
airouter serve -addr :8080
```

**Mounted inside your own FastAPI / Starlette app** at `/airouter/`:

```python
import airouter
from airouter.server import new_handler
from fastapi import FastAPI
from starlette.routing import Mount

c = airouter.open(airouter.Options(db="ai.db"))
c.add_free_pool({"GROQ_API_KEY": "gsk_..."})
airouter_app = new_handler(c.router, admin_token="change-me")

app = FastAPI()
app.router.routes.append(Mount("/airouter", app=airouter_app))

# Use the Client directly from your own routes — no HTTP hop.
@app.get("/summarize/{text}")
def summarize(text: str):
    return {"summary": c.chat(f"Summarize in one sentence: {text}")}
```

See [examples/mount_in_existing_app.py](examples/mount_in_existing_app.py)
for a full working example.

## Features

- **Three request types, one router:** text chat (`r.generate`), text-to-speech
  (`r.speak`), speech-to-text (`r.transcribe`). Each one filters the provider
  pool by category so a TTS call never hits a chat model and vice versa.
- **Per-provider per-model limits** with RPM / RPD / TPM for chat and TTS,
  plus ASH (audio seconds per hour) and ASD (audio seconds per day) for TTS
  and STT. Every model entry is independent.
- **Per-provider timezone-aware daily resets.** Gemini's quota resets at PT
  midnight; the router knows that and unpauses the provider at the right time.
- **Three flavours of cooldown:** seconds (a transient 429), until-local-midnight
  (a daily cap), 24 hours (a bad key or model id).
- **Pauses persist** to SQLite — restarts and separate CLI invocations respect
  the same state.
- **Tenants** with hashed API keys and optional daily request caps. Provider
  keys are shared across tenants; a provider can also be restricted to one.
- **JSON mode** that asks the provider for JSON and validates the response,
  failing over if a model returns junk. Models without JSON mode or a system
  role (Gemma) are handled transparently.
- **Curated free-pool catalog** with three categories: `text` (20 models),
  `tts` (6 models), `stt` (3 models). One `bootstrap` command seeds whichever
  categories you want.
- **Same shape, three faces:** the CLI, the HTTP server, and the Python
  library all expose the same Request/Response types.

## Quick start

```bash
git clone https://github.com/mrmhthegreat/airouter   # original Go repo
# or this Python port
pip install -r requirements.txt
# or: pip install -e .   (with the [server] extra for FastAPI / uvicorn)

export GROQ_API_KEY=gsk_...  GEMINI_API_KEY=...  # set the ones you have
airouter bootstrap                          # import curated free-pool entries
airouter provider list                      # live status

airouter ask "Write one sentence about rain"
airouter ask -json -system 'Reply as {"emotion":"..."} only.' "I failed my exam today"
echo "long text..." | airouter ask -envelope # full Response JSON

airouter usage                              # today's usage per provider
```

The `bootstrap` command imports every entry from the curated free-pool catalog
whose env var is set. Entries without a key (Z.AI, OpenRouter, etc.) are
skipped with a clear message — set their env vars and re-run.

## Output format

The CLI auto-detects stdout:

- **stdout is a terminal** → text on stdout + a short `[provider / model, N tokens, Xms]` footer on stderr.
- **stdout is piped** → raw text only, no footer. So `airouter ask ... | grep` works cleanly.
- `-json` → ask the model for JSON and pretty-print only that JSON.
- `-envelope` → full Response as JSON (text + provider + model + tokens + attempts).
- `-text` → force raw text with no footer.

The HTTP server mirrors this with `?format=text` and the `Accept: text/plain`
header.

## Library usage

```python
import airouter

c = airouter.open(airouter.Options(db="ai.db"))  # keys encrypted at rest
# once: put your keys in the DB (encrypted). Afterwards no env vars are needed.
c.add_free_pool({"GROQ_API_KEY": "gsk_...", "GEMINI_API_KEY": "..."})

text = c.chat("Write one sentence about rain")   # auto-failover

# full control
resp = c.ask(airouter.Request(system="Be brief", prompt="hi", json=True))
print(resp.text, resp.provider, resp.model, resp.tokens, resp.attempts)

# expose OpenAI API + web UI from your own server
# c.handler(admin_token=os.environ["AIROUTER_ADMIN_TOKEN"])
```

`airouter.new_client(store)` takes any `airouter.Store` (memory, or your own
Postgres) if you do not want SQLite. All `Router` methods (`generate`,
`speak`, `transcribe`, `status`, `usage`, ...) are on the client. Share the
same DB file with the CLI and server: usage and pauses are shared.

## HTTP server, web UI and OpenAI-compatible API

```bash
export AIROUTER_ADMIN_TOKEN=change-me
export AIROUTER_SECRET=$(python3 -c "import secrets;print(secrets.token_hex(32))")  # encrypts provider keys
airouter tenant add acme -limit 2000       # prints the tenant API key once
airouter serve -addr :8080                 # add -cors https://myapp.com for browser apps
```

- **Web UI:** open `http://localhost:8080/`, paste a tenant key (or the admin
  token). Tabs: **Chat** (shows which provider answered and any failovers),
  **Usage** (live provider status, daily usage, what the provider itself
  reports as remaining), **Connect your app** (base URL + copy-paste
  Python/curl snippets), **Admin** (bootstrap, list providers / tenants).
- **OpenAI-compatible:** base URL `http://localhost:8080/v1`, API key = your
  airouter tenant key, `model: "auto"`.

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8080/v1", api_key="air_...")
print(client.chat.completions.create(model="auto",
      messages=[{"role": "user", "content": "Hello"}]).choices[0].message.content)
```

Supported: `POST /v1/chat/completions` (incl. `stream: true`, `response_format`
json, `max_tokens`, `temperature`), `GET /v1/models`, `POST /v1/audio/speech`,
`POST /v1/audio/transcriptions`. `model` is `auto` (router chooses and fails
over) or a provider name / exact model id to pin one. **Not supported yet:**
tool/function calling and image input (clear `400`). `stream: true` is
*buffered streaming*: the router needs the whole answer to fail over safely, so
chunks are sent after it completes.

Native API (richer: attempts, provider, latency):

```bash
curl -s localhost:8080/v1/generate -H "Authorization: Bearer air_..." -d '{"prompt":"Say hi","system":"Be brief"}'
curl -s localhost:8080/v1/usage    -H "Authorization: Bearer air_..."      # usage + live provider status
curl -s localhost:8080/v1/whoami   -H "Authorization: Bearer air_..."
```

### Status codes you will see

| Code | Meaning | Retry? |
|------|---------|--------|
| 400 | Your request is invalid (empty messages, every provider rejected it as bad) | No, fix it |
| 401 | Missing/invalid key | No |
| 403 | Tenant disabled | No |
| 429 | **Your tenant's** daily cap is used up (`Retry-After`) | After reset |
| 502 | Providers were tried and all failed (`attempts` lists why) | Yes, soon |
| 503 | Every provider is limited / cooling down right now (`Retry-After`, `reasons`) | Yes, after `Retry-After` |
| 504 | The whole request timed out (default 2 min) | Yes |

OpenAI SDKs retry 429/503 automatically, so they behave well against airouter.

Admin endpoints (`Authorization: Bearer <admin token>`):
`GET/POST /v1/providers`, `DELETE /v1/providers/{id}`,
`POST /v1/providers/{id}/enable|disable|reset`,
`POST /v1/providers/{id}/key` (`{"api_key": "..."}`),
`GET/POST /v1/tenants`, `POST /v1/tenants/{id}/rotate-key`,
`POST /v1/bootstrap?store_keys=1`.

Public endpoints: `GET /` (UI), `GET /healthz`, `GET /v1/presets`, `GET /v1/presets/free-pool`.

## How switching works

Providers are tried by `priority` (lower first; a tenant's own providers go
before shared ones). A provider is skipped when its daily cap is reached
(paused to its local midnight), its per-minute window is full (90% safety
margin), it is busy (`workers`), or it is paused/cooling down.

What happens when a provider fails:

| Upstream result | Reaction |
|-----------------|----------|
| 401, 403, 404 | Pause 24h (bad key / model id), then try the next provider |
| 402 | Pause 1h (out of credits) |
| 429 with daily hint (`PerDay`, "daily", Retry-After > 5 min, or headers say 0 left for hours) | Pause until the provider's local midnight |
| other 429 | Cool down for `Retry-After` / Gemini `retryDelay` (30s if none) |
| 400, 413, 422 | **No penalty** (it is usually your request). If every provider rejects it you get a 400. 3 config-looking 400s in a row (bad model / json mode) pause 24h; any success resets this |
| 408, 5xx, timeout, network error | Exponential backoff 20s, 40s, ... up to 5 min; reset on success |
| 200 but empty / invalid JSON when JSON was requested | Try the next provider, no penalty |

**When limits are hit it does not hammer or loop forever:** each provider is
tried at most once per request. If everything is only briefly limited,
`generate` waits up to `MaxWait` (20s) for the soonest provider; otherwise it
returns `503` with `Retry-After`. A single call is bounded by
`AttemptTimeout` (60s) and the whole request by `RequestTimeout` (2 min).

## Keys and secrets: what is stored how

| Secret | Storage | Why |
|--------|---------|-----|
| Tenant API keys (`air_...`) | **SHA-256 hash only** in the DB | Shown once; a DB leak reveals no working key. Rotate with `airouter tenant rotate NAME`. |
| Provider keys (Groq, Gemini, ...) | **AES-256-GCM encrypted** in the DB | They must be sent upstream, so they cannot be hashed. The DB alone is useless without the master secret. |
| Master secret | `$AIROUTER_SECRET`, else a `0600` file `<db>.key` | Back it up **separately** from the DB. Lose it and stored provider keys cannot be decrypted (re-enter them). |

```bash
airouter provider add -preset groq -model openai/gpt-oss-120b -key-stdin   # paste key; stored encrypted
airouter provider set-key 3                                                 # replace a key later
airouter bootstrap -store-keys        # copy keys from env vars into the DB, then unset the env vars
```

`provider list` shows where each key comes from (`db` / `env`) without ever
printing it. Databases from older versions with plaintext keys are encrypted
automatically on first open.

## Free-tier catalog

`airouter free-pool` lists the curated catalog. Every entry has a `Category`,
real model ID, RPM/RPD/TPM from the provider's console, and a `VerifiedOn`
date. Limits move — re-check before relying on them.

Three categories are supported:

| Category | What it is                                                | Routed?                              |
| -------- | --------------------------------------------------------- | ------------------------------------ |
| `text`   | Chat, OpenAI-compatible `/chat/completions`               | Yes — `r.generate` / `airouter ask` / `POST /v1/generate` |
| `tts`    | Text-to-speech                                            | Yes — `r.speak` / `airouter speak` / `POST /v1/speak`     |
| `stt`    | Speech-to-text                                            | Yes — `r.transcribe` / `airouter transcribe` / `POST /v1/transcribe` |

### Importing categories

```bash
airouter bootstrap                           # default = text only
airouter bootstrap -category text            # explicit text only
airouter bootstrap -category tts,stt         # only TTS + STT
airouter bootstrap -category all             # everything
airouter bootstrap -preset groq              # only Groq entries
airouter bootstrap -overwrite                # update existing same-name entries
```

## Project layout

```
airouter/
    __init__.py       one-import facade (airouter.open / Client.chat / .ask / .handler / .add_free_pool)
    types.py          public data shapes (ProviderConfig, Tenant, Request, ...)
    preset.py         built-in provider catalog + curated free-pool model list
    store.py          Store ABC, MemoryStore, SQLStore (sqlite3), EncryptedStore (AES-256-GCM)
    ratelimit.py      parse rate-limit headers / Retry-After / Gemini retryDelay / quotaDetail
    limiter.py        ProviderState, acquire/release, classify_failure, observe_rate_headers
    router.py         core Router: candidates, Generate, Options, TenantPolicy
    router_client.py  call_provider (OpenAI-compatible + Ollama)
    router_admin.py   CreateTenant, Authenticate, SaveProvider, BootstrapProviders, ...
    router_status.py  Status, Usage, ProviderStatus, UsageReport
    router_tenantprov.py  Tenant self-service (AddTenantProvider, OllamaModels, ...)
    speak.py          r.speak — TTS via Groq (OpenAI-compat) or Gemini (native)
    transcribe.py     r.transcribe — STT via Groq Whisper or Gemini Files API
    server.py         Starlette app: native + OpenAI-compatible + /v1/me/* + web UI
    cli.py            argparse dispatcher: ask / speak / transcribe / serve / provider / tenant / me / ...
    web/index.html    embedded web UI
examples/            runnable examples (chat.py, serve.py, audio.py)
tests/               end-to-end tests against a mock OpenAI-compatible server
pyproject.toml       pip install -e .[server]
```

## Differences from the Go original

The Python port is a 1:1 functional port. Notable implementation differences:

- HTTP server uses **FastAPI / Starlette / uvicorn** instead of `net/http`.
- HTTP client uses **`requests`** instead of `net/http`.
- Encryption uses **`cryptography`'s AESGCM** instead of `crypto/cipher`.
- SQLite uses the stdlib **`sqlite3`** module instead of `mattn/go-sqlite3` /
  `modernc.org/sqlite`.
- CLI uses **`argparse`** instead of `flag`.
- Provider state is protected by `threading.Lock` instead of `sync.Mutex`.

All public APIs (Request/Response shapes, error types, Router methods, CLI
subcommand flags, HTTP endpoint paths, encrypted-key storage format) match the
Go original so that databases created by either implementation work with the
other (assuming you set `AIROUTER_SECRET` to the same value).

## License

MIT — same as the original Go project. See [LICENSE](https://github.com/mrmhthegreat/airouter/blob/main/LICENSE).
