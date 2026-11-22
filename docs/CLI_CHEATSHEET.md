# CLI cheatsheet

Every `airouter` subcommand with copy-paste examples. After `pip install -e .`
(or `pip install airouter` once published), the `airouter` script is on your
PATH.

```
$ airouter --help
airouter — route prompts across free AI providers ...
```

Database: `-db FILE` or `$AIROUTER_DB` (default `./airouter.db`).
Provider keys are stored encrypted; the secret is `$AIROUTER_SECRET` or
`<db>.key` (mode 0600, created on first use — back it up separately).

---

## bootstrap — seed the DB from the curated free-pool catalog

```bash
export GROQ_API_KEY=gsk_...      # set the ones you have
export GEMINI_API_KEY=AIza...
export ZAI_API_KEY=...
airouter bootstrap                                  # default = text only
airouter bootstrap -category text                   # explicit text only
airouter bootstrap -category tts,stt                # only TTS + STT
airouter bootstrap -category all                    # everything
airouter bootstrap -preset groq                     # only Groq entries
airouter bootstrap -overwrite                       # update existing same-name entries
airouter bootstrap -store-keys                      # copy keys from env into DB (encrypted), then unset env vars
airouter bootstrap -tenant acme                     # import models as PRIVATE to one tenant
```

## ask — send a prompt (or pipe it on stdin)

```bash
airouter ask "Write one sentence about rain"
airouter ask -system "Be terse." "What is 2+2?"
airouter ask -json -system 'Reply as {"emotion":"..."} only.' "I failed my exam today"
airouter ask -provider groq-openai/gpt-oss-120b "Hello"
airouter ask -tenant acme "..."                     # use this tenant's quota
airouter ask -strategy spread "..."                 # balance load across providers
echo "long text..." | airouter ask -envelope        # full Response JSON on stdout
airouter ask -text "..."                            # raw text only, no [provider] footer
airouter ask -max-tokens 100 "..."
```

Output format (in priority order):

- `-envelope` — full Response as JSON (text + provider + model + tokens + attempts)
- `-json` — ask the model for JSON and pretty-print only that JSON
- `-text` — raw text only, no `[provider]` footer on stderr
- default — text on stdout + a `[provider / model, N tokens, Xms]` footer on stderr when stdout is a terminal

If stdout is piped, no footer is emitted by default — so `airouter ask ... | grep` works cleanly.

## speak — text → audio (WAV bytes)

```bash
airouter speak -o hello.wav "Hello world"
airouter speak -o hello.wav -voice Kore -provider gemini-tts-gemini-3.8-flash-tts "Hello from Gemini"
airouter speak -o arabic.wav -voice hassan "مرحبا بالعالم"
airouter speak -format mp3 -o out.mp3 "Hello"
airouter speak -language ar "..."                   # language hint
```

Default voices when the caller does not pass `-voice`:

- Gemini TTS → `Kore`
- Groq Orpheus English → `tara`
- Groq Orpheus Arabic (Saudi) → `hassan`

## transcribe — audio file → text

```bash
airouter transcribe recording.mp3
airouter transcribe -language en recording.wav
airouter transcribe -provider groq-whisper-large-v3-turbo recording.mp3
airouter transcribe -prompt " Interview with Dr. Smith" recording.mp3   # spelling/style hint
airouter transcribe -envelope recording.wav         # full response as JSON on stderr
```

## provider — add / list / rm / enable / disable / reset / set-key / limits / import

```bash
airouter provider list
airouter provider list -tenant acme                 # only acme's own models
airouter provider add -preset groq -model openai/gpt-oss-120b -rpm 30 -rpd 1000 -tpm 8000 -priority 10
airouter provider add -preset gemini -model gemini-3.5-flash-lite -key-stdin
airouter provider add -preset ollama -model llama3:8b -priority 90
airouter provider add -preset groq -model canopylabs/orpheus-v1-english -category tts -rpm 10 -rpd 100
airouter provider add -name my-groq -kind openai -base-url https://api.groq.com/openai/v1 -key gsk_... -model openai/gpt-oss-120b
airouter provider set-key 3                         # replace a key, prompts on stdin
airouter provider set-key 3 -key gsk_newkey...
airouter provider limits 7 -rpm 20 -rpd 500 -tenant acme
airouter provider enable 3
airouter provider disable 3
airouter provider reset 3                           # lift a pause after fixing a key
airouter provider rm 3
airouter provider import providers.json             # JSON file with {"providers":[...]}
airouter provider import -tenant acme providers.json
```

`provider list` columns:

```
ID  OWNER  CAT  NAME  MODEL  STATE  KEY  USED/RPD  RPM  TPM  AUDIO SEC (ASH/ASD)  RESETS IN  TZ
```

## tenant — add / list / rotate

```bash
airouter tenant add acme -limit 1000 -tz UTC        # prints the API key once
airouter tenant list
airouter tenant rotate acme                         # new key; the old one stops working
```

## usage — per-tenant usage report

```bash
airouter usage                                      # today, default tenant
airouter usage -tenant acme
airouter usage -day 2026-01-15                      # historical day (per provider's own TZ)
```

## ollama — list / add local Ollama models

```bash
airouter ollama list                                # what's installed locally
airouter ollama add gemma3:4b                       # add as a shared provider
airouter ollama add -tenant acme gemma3:4b qwen2.5:3b   # private to one tenant
```

## me — self-service for ONE tenant, over HTTP

The `me` subcommand talks to a **running server** with the tenant's own API
key. It never opens the local database, so it works against any airouter
server.

```bash
export AIROUTER_URL=http://localhost:8080
export AIROUTER_KEY=air_...

airouter me whoami
airouter me list                                    # my models + shared ones
airouter me catalog                                 # providers and models I can pick from
airouter me pick -preset groq -key-stdin openai/gpt-oss-20b openai/gpt-oss-120b
airouter me add -preset gemini -model gemini-2.5-flash -key-stdin -rpm 10
airouter me ollama                                  # what's installed on the server
airouter me ollama add gemma3:4b
airouter me limits 7 -rpm 20 -rpd 500
airouter me set-key 7 -key-stdin
airouter me enable 7 / disable 7 / reset 7 / rm 7
airouter me import my-models.json
airouter me export                                  # your models as importable JSON (no keys)
airouter me usage
airouter me update -name Alice -tz Asia/Karachi
airouter me rotate-key                              # new key; the old one stops working
```

## presets — list built-in provider presets

```bash
airouter presets
```

## free-pool — list the curated free-tier model catalog

```bash
airouter free-pool                                  # all categories
airouter free-pool -category text                   # only text models
airouter free-pool -category text,tts,stt
airouter free-pool -category all                    # same as no flag
airouter free-pool -json                            # JSON output instead of a table
```

## serve — run the HTTP API + web UI as a standalone server

```bash
export AIROUTER_ADMIN_TOKEN=change-me
export AIROUTER_SECRET=$(python3 -c "import secrets;print(secrets.token_hex(32))")
airouter serve -addr :8080                          # web UI: http://localhost:8080/
airouter serve -cors https://myapp.com              # allow browser apps
airouter serve -allow-tenant-urls                   # let tenants add providers with any base URL
airouter serve -block-tenant-ollama                 # don't let tenants use this server's Ollama
airouter serve -max-tenant-models 100
```

---

## Status codes the HTTP server returns

| Code | Meaning | Retry? |
|------|---------|--------|
| 400  | Your request is invalid (empty messages, every provider rejected it as bad) | No, fix it |
| 401  | Missing/invalid key | No |
| 403  | Tenant disabled | No |
| 429  | **Your tenant's** daily cap is used up (`Retry-After`) | After reset |
| 502  | Providers were tried and all failed (`attempts` lists why) | Yes, soon |
| 503  | Every provider is limited / cooling down right now (`Retry-After`, `reasons`) | Yes, after `Retry-After` |
| 504  | The whole request timed out (default 2 min) | Yes |

OpenAI SDKs retry 429/503 automatically, so they behave well against airouter.
