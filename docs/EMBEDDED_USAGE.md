# Embedded usage — using airouter as a Python library

Three ways to use airouter:

1. **CLI** — `airouter ask "..."`, `airouter serve`, ... (see [CLI_CHEATSHEET.md](CLI_CHEATSHEET.md))
2. **Embedded library** — `import airouter` in your own Python code
3. **HTTP server (mounted or standalone)** — `airouter serve`, or mount the handler inside your own FastAPI / Starlette app

All three share the same SQLite DB file: usage, pauses and provider configs
are consistent across them.

---

## Install

```bash
pip install -e .                  # editable install from a clone
pip install -e .[server]          # adds FastAPI / uvicorn / starlette / python-multipart
# or, once published:
pip install airouter-py           # library only
pip install airouter-py[server]   # library + server extras
```

After install, the `airouter` CLI script is on your PATH and `import airouter`
works from any directory. (The package is published under the name
`airouter-py` because the bare name `airouter` was already taken on PyPI;
the CLI script and the import name are both still `airouter`.)

---

## Embedded library

Open a client once, then call `chat` / `ask` / `generate` / `speak` /
`transcribe` on it.

```python
import airouter

c = airouter.open(airouter.Options(db="ai.db"))   # keys encrypted at rest

# Once: seed the curated free-pool catalog with your keys.
c.add_free_pool({
    "GROQ_API_KEY": "gsk_...",
    "GEMINI_API_KEY": "AIza...",
})

# Quick prompt → text. Auto-failover happens inside.
text = c.chat("Write one sentence about rain.")

# Full control: Request → Response
resp = c.ask(airouter.Request(
    system="Be brief.",
    prompt="What is 2+2?",
    json=True,                  # ask for and validate JSON
    max_tokens=100,
    temperature=0.7,
    provider="",                # "" = auto (router chooses); or pin one by name
    strategy="priority",        # or "spread" to balance load
))
print(resp.text, resp.json, resp.provider, resp.model, resp.tokens, resp.attempts)
```

### Three request types

```python
# 1. Text chat
resp = c.generate(airouter.Request(prompt="Hello"))

# 2. Text → speech (TTS)
resp = c.speak(airouter.SpeakRequest(
    text="Hello world",
    voice="Kore",               # default for Gemini; "tara" for Groq Orpheus English
    format="wav",               # wav (default), mp3, pcm
))
# resp.audio is raw WAV bytes; resp.content_type is "audio/wav"
with open("hello.wav", "wb") as f:
    f.write(resp.audio)

# 3. Speech → text (STT)
with open("recording.mp3", "rb") as f:
    audio = f.read()
resp = c.transcribe(airouter.TranscribeRequest(
    audio=audio,
    filename="recording.mp3",   # used for MIME detection + WAV duration parsing
    language="en",              # optional hint
    prompt="Interview with Dr. Smith",   # optional style/spelling hint
))
print(resp.text)
```

### Multi-turn conversations

The router takes a flat message list — manage your conversation history
yourself and pass it as `messages`:

```python
history = [
    airouter.Message(role="system", content="You are a helpful assistant."),
    airouter.Message(role="user", content="My name is Alice."),
    airouter.Message(role="assistant", content="Hello Alice!"),
]
history.append(airouter.Message(role="user", content="What's my name?"))
resp = c.generate(airouter.Request(messages=history))
print(resp.text)   # "Alice"
```

### Bring your own Store

If you do not want SQLite (e.g. tests, short-lived scripts, or you have your
own Postgres backend), implement the `airouter.Store` interface and pass it to
`new_client`:

```python
import airouter

# Built-in: MemoryStore keeps everything in RAM (no disk file).
c = airouter.new_client(airouter.MemoryStore(), airouter.Options())

# Or implement the Store ABC yourself (e.g. Postgres, Redis, ...).
class MyStore(airouter.Store):
    def create_tenant(self, t, key_hash): ...
    def tenant_by_id(self, id): ...
    def tenant_by_key_hash(self, hash): ...
    # ... (see airouter/store.py for the full interface)
c = airouter.new_client(MyStore(), airouter.Options())
```

### Inspect live state

```python
# Live status of every provider: state, used_today, tokens_today, audio_sec_today,
# upstream rate-limit headers, what the provider itself reports as remaining.
for s in c.status():
    print(f"#{s.id} {s.name:30s} state={s.state:12s} used={s.used_today}/{s.rpd}")

# Per-tenant usage report: requests/tokens today, per-provider breakdown,
# all-tenants combined (because provider quota is shared).
rep = c.usage("default")
print(rep.requests_today, rep.tokens_today, rep.daily_limit)
for u in rep.providers:
    print(f"  {u.provider}: {u.tenant_requests} req, {u.tenant_tokens} tok, all={u.all_requests}")
```

### Error handling

Router errors are typed — catch the specific ones you care about:

```python
import airouter

try:
    resp = c.generate(airouter.Request(prompt="..."))
except airouter.ExhaustedError as e:
    # Every provider is limited / cooling down right now.
    print(f"retry in {int(e.retry_after)}s; reasons: {e.reasons}")
except airouter.AllFailedError as e:
    # Providers were tried and each one failed (network error, bad response, ...).
    for a in e.attempts:
        print(f"  - {a.provider}: {a.error}")
except airouter.BadRequestError as e:
    # Every provider rejected the request itself (HTTP 400/413/422).
    print(f"fix your request: {e.message}")
except airouter.TenantLimitError as e:
    # Your tenant's daily cap is used up.
    print(f"tenant limit reached; resets in {int(e.retry_after // 60)} min")
except airouter.ErrNoProviders:
    # No enabled provider matches the request. Run `airouter bootstrap`.
    print("no providers configured")
except airouter.ErrTimeout:
    # The whole request ran out of time (default 2 min).
    print("request timed out")
```

---

## HTTP server: standalone

```bash
export AIROUTER_ADMIN_TOKEN=change-me
airouter serve -addr :8080
```

Web UI at `http://localhost:8080/`. OpenAI base URL `http://localhost:8080/v1`.

---

## HTTP server: mounted inside your own app

The handler returned by `c.handler(admin_token)` is a Starlette app. Mount
it inside any FastAPI / Starlette app at any sub-path:

```python
# examples/mount_in_existing_app.py
import airouter
from airouter.server import new_handler
from fastapi import FastAPI
from starlette.routing import Mount

c = airouter.open(airouter.Options(db="ai.db"))
c.add_free_pool({"GROQ_API_KEY": "gsk_..."})

airouter_app = new_handler(c.router, admin_token="change-me")

app = FastAPI(title="My App")

@app.get("/")
def root():
    return {"message": "Hello from my app", "airouter": "mounted at /airouter/"}

# Mount airouter under /airouter/ — every endpoint becomes /airouter/<path>.
app.router.routes.append(Mount("/airouter", app=airouter_app))

# Use the Client directly from your own routes — no HTTP hop.
@app.get("/summarize/{text}")
def summarize(text: str):
    summary = c.chat(f"Summarize in one sentence: {text}")
    return {"summary": summary}
```

Run it:

```bash
uvicorn examples.mount_in_existing_app:app --port 8000
```

Then:

* Your app:               http://localhost:8000/
* airouter web UI:        http://localhost:8000/airouter/
* airouter OpenAI API:    http://localhost:8000/airouter/v1/chat/completions
* airouter native API:    http://localhost:8000/airouter/v1/generate
* airouter health:        http://localhost:8000/airouter/healthz

Point an OpenAI SDK at the mounted URL:

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/airouter/v1", api_key="air_...")
print(client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Hello"}],
).choices[0].message.content)
```

---

## Three faces, one DB

The CLI, the HTTP server, and the Python library all read and write the same
SQLite file. Provider configs, encrypted keys, daily-limit pauses, and usage
counters are shared across them. So you can:

```bash
airouter bootstrap -store-keys          # CLI seeds the DB
airouter provider list                  # CLI sees the live state
airouter serve &                        # server starts, picks up the same DB
```

```python
import airouter
c = airouter.open(airouter.Options(db="airouter.db"))   # library opens the same DB
print(c.chat("Hi"))                  # sees the same providers, same usage
```
