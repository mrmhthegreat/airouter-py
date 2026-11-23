# API Reference — Emotion Recognition & Islamic Guidance (Python port)

All `/api/*` and `/predict` routes speak JSON. The HTML pages (`/`, `/guidance.html`, `/admin.html`) and `/static/*` are browser-facing.

Run the server with:

```
emotion serve        # = uvicorn emotion_fyp.app:app --host 0.0.0.0 --port $PORT
```

Base URL defaults to **`http://127.0.0.1:8000`** (`PORT` env var, default `8000`).

---

## Swagger / OpenAPI (built-in)

FastAPI generates interactive docs automatically — nothing to enable:

| URL | What it is |
|---|---|
| `/docs` | **Swagger UI** — try every endpoint from the browser |
| `/redoc` | ReDoc view of the same schema |
| `/openapi.json` | Raw OpenAPI schema (usable with Postman / code generators) |

This markdown file adds the parts Swagger can't show for dict-returning routes: exact response bodies, examples, and error cases.

---

## Engine order: `ENGINE_ORDER` (no per-request mode)

The engine order is **not** sent in requests anymore. It is fixed at the server level by the env var:

| `ENGINE_ORDER` | What runs on `/api/analyze` |
|---|---|
| `ml,llm` | ML first; LLM only if ML fails |
| `llm,ml` | LLM first; ML only if LLM fails |
| unset / empty | **both**: ML gives the emotion scores, LLM always adds themes + needs + risk |

If everything fails, a small keyword fallback answers. `GET /api/engine` shows the active order.

---

## Shared objects

**`Emotion`**

```json
{ "label": "sadness", "score": 0.9, "intensity": "high", "evidence": "" }
```

- `score` — 0.0 … 1.0
- `intensity` — `high` (score ≥ 0.7), `medium` (≥ 0.4), else `low`
- `evidence` — quote from the story (LLM mode) or `""` (ML mode)
- `all_scores` entries have the same shape

**Guidance `Item`** (ayah / hadith / dua / sunnah / action) — free-form dict; the fields the frontend uses:

| Field | Type | Meaning |
|---|---|---|
| `id` | string | unique item id |
| `arabicText` | string | original Arabic text |
| `transliteration` | string | optional romanized Arabic |
| `translationUr` | string | Urdu translation |
| `translationEn` | string | English translation |
| `explanation` | string | short tafsir-style note |
| `applicationAdvice` | string | "Kya karein" practical advice |
| `reference` | string | e.g. `Al-Baqarah 2:153` |
| `source` | string | data source of the item |
| `verified` | bool | true = verified item |
| `why` | string[] | emotions/themes that matched this item |

---

## Core API

### `POST /api/analyze`

Analyze a story: emotions → themes/needs → guidance + risk check.

**Request body**

| Field | Type | Default | Notes |
|---|---|---|---|
| `text` | string | required | the story; **must be ≥ 5 words**, else HTTP 400 |
| `runs` | int | `0` | how many LLM runs to average; clamped to `1…5` (`0` → 1) |
| `debug` | bool | `false` | include raw LLM output + provider attempts; debug responses are never cached |

(The old `engine` field is gone. Old clients that still send it are accepted — the extra field is ignored.)

**Example request**

```bash
curl -s http://127.0.0.1:8000/api/analyze \
  -H 'Content-Type: application/json' \
  -d '{"text": "Aaj main bohat udaas hoon aur akeel mehsoos kar raha hoon", "runs": 1}'
```

**Response body (200)**

| Field | Type | Present |
|---|---|---|
| `analysisId` | string | always — sha256[:16] of the story (use it for `/api/feedback`) |
| `emotions` | Emotion[] | always — top 3, strongest first |
| `themes` | string[] | always — top 3 |
| `needs` | string[] | always — top 3 |
| `confidence` | float | always — score of the top emotion |
| `uncertain` | bool | always — true when `confidence < UNCERTAIN_BELOW` (default 0.5) |
| `follow_up` | string \| null | always — follow-up question when `uncertain` |
| `risk` | bool | always — risk keywords found or LLM risk votes |
| `engine` | string | always — which engines actually ran (see below) |
| `cached` | bool | only when `true` — answer came from the cache |
| `guidance` | object | always — `{ayahs: Item[], hadiths: Item[], duas: Item[], sunnah: Item[], actions: Item[]}` |
| `safety_message` | string | only when `risk: true` — Urdu helpline message |
| `disclaimer` | string | always |
| `all_scores` | Emotion[] | only when the ML ran — all 31 emotion scores |
| `debug_raw_llm_outputs` | string[] | only when `debug: true` |
| `debug_all_scores` | Emotion[] | only when `debug: true` |
| `debug_provider_attempts` | string[] | only when `debug: true` |

**`engine` values** — `ml+llm:<provider>:<model>` (both ran), `ml` (ML only), `llm:<provider>:<model>` (LLM only), `ml [LLM: <error>]` / `llm:... [ML: <error>]` (fallback happened), `fallback-keywords (ML: ...; LLM: ...)` (everything failed).

**Example response**

```json
{
  "analysisId": "9b1deb4d3b7d4bec",
  "emotions": [
    { "label": "sadness", "score": 0.9, "intensity": "high", "evidence": "" },
    { "label": "loneliness", "score": 0.72, "intensity": "high", "evidence": "akeel mehsoos kar raha hoon" }
  ],
  "themes": ["loss", "work"],
  "needs": ["comfort", "advice"],
  "confidence": 0.9,
  "uncertain": false,
  "follow_up": null,
  "risk": false,
  "engine": "ml+llm:groq:llama-3.1-8b-instant",
  "guidance": {
    "ayahs": [
      { "id": "ayah-2-153", "arabicText": "…", "translationUr": "…",
        "reference": "Al-Baqarah 2:153", "source": "guidance.json",
        "verified": true, "why": ["sadness"] }
    ],
    "hadiths": [], "duas": [], "sunnah": [], "actions": []
  },
  "disclaimer": "Spiritual support only, not a replacement for professional help.",
  "all_scores": [
    { "label": "sadness", "score": 0.9, "intensity": "high", "evidence": "" }
  ]
}
```

**Errors**

| Code | Body |
|---|---|
| 400 | `{"detail": "Please write a longer story (at least 5 words)."}` |

---

### `GET /api/engine`

Shows the active engine configuration.

**Response (200)**

```json
{
  "engine_order": "ml,llm",
  "engine_order_env": "ml,llm",
  "llm_backend": "airouter",
  "airouter_api": "/airouter/",
  "airouter_ui": "/airouter/",
  "airouter_cli": "emotion provider list | emotion bootstrap -store-keys | emotion ask ... | emotion presets | ...",
  "note": "The engine order comes from the ENGINE_ORDER env var: 'ml,llm' = ML first, LLM only if ML fails; 'llm,ml' = LLM first, ML only if LLM fails; unset = both (ML emotion scores + LLM themes/needs/risk). ..."
}
```

`engine_order` is the resolved chain (`ml,llm` / `llm,ml` / `ml` / `llm` / `both`); `engine_order_env` is the raw env value.

---

### `POST /api/feedback`

Rate an analysis.

**Request body**

| Field | Type | Notes |
|---|---|---|
| `analysisId` | string | from `/api/analyze` |
| `helpful` | bool | was the answer helpful |
| `correct_emotion` | string \| null | optional corrected top emotion |

**Response (200)**

```json
{ "ok": true }
```

**Errors**: 500 `{"detail": "could not save feedback: <reason>"}`.

---

### `POST /api/chat`

Free-form chat with the LLM (airouter backend).

**Request body**: `{ "text": "..." }`

**Response (200)**

```json
{ "reply": "...", "provider": "groq", "model": "llama-3.1-8b-instant", "attempts": ["groq:ok"] }
```

**Errors**

| Code | Body |
|---|---|
| 400 | `{"detail": "text is empty"}` |
| 503 | `{"detail": "airouter: no client configured"}` or the LLM error string |

---

### `GET /api/usage`

airouter provider usage (daily request/token counters).

**Response (200)**

```json
{
  "providers": [
    {
      "position": 1,
      "name": "groq",
      "model": "llama-3.1-8b-instant",
      "state": "ready",
      "usage": { "requests": 12, "tokens": 3400, "today_requests": 12, "today_tokens": 3400 },
      "remaining_today": { "requests": 88 }
    }
  ],
  "totals": { "requests": 12, "tokens": 3400, "errors": 0 },
  "note": "airouter in-process. Daily counters reset in each provider's own timezone (ResetTZ).",
  "ai_ready": true
}
```

`state` values: `ready`, `not_configured`, `cooling_down`, `limit_reached`. `detail` is added when the provider is not ready. When airouter is not initialized: empty `providers` list, `ai_ready: false`.

---

### `GET /api/emotions`

Emotion labels available in the guidance table (with item counts).

**Response (200)**

```json
{ "emotions": ["..."], "note": "Item counts come from the guidance table in the SQL database." }
```

---

### `GET /api/guidance`

Browse guidance items.

**Query params**

| Param | Type | Default | Notes |
|---|---|---|---|
| `type` | string | `ayah` | one of `ayah`, `hadith`, `dua`, `sunnah`, `action` |
| `emotion` | string | `""` | filter by emotion label |
| `limit` | int | `0` | max items (0 = server default) |
| `random` | bool | `false` | shuffle |

**Response (200)**

```json
{ "type": "ayah", "emotion": "sadness", "count": 2, "items": [ { "id": "...", "...": "Item fields" } ] }
```

**Errors**: 400 `{"detail": "invalid type (use: ayah, hadith, dua, sunnah, action)"}`.

---

### `GET /api/guidance/random`

A random mix (used by the Sabr loading screen).

**Query params**: `emotion` (string, default `""`), `hadiths` (int, default `0`).

**Response (200)**

```json
{ "ayahs": [Item], "hadiths": [Item], "duas": [Item], "sunnah": [Item], "actions": [Item] }
```

---

### `GET /api/health`

Overall server health.

**Response (200)**

```json
{
  "store": "data_store/emotion.db",
  "guidance_items": 128,
  "ml_server": "not configured (set EMOTION_CLASSIFIER_URL or load MODEL_DIR)",
  "providers": [
    { "name": "groq", "model": "llama-3.1-8b-instant", "state": "ready", "detail": "ok" }
  ],
  "ai_ready": true
}
```

`ml_server` is `reachable` when the ML engine (in-process or remote) is up, otherwise a message explaining what is missing. `guidance_error` appears only when the guidance count failed.

---

### `GET /api/samples`

The sample stories file (raw JSON, `Content-Type: application/json`):

```json
{ "stories": [ { "id": "s1", "expected_top": "sadness", "text": "..." } ] }
```

---

## ML endpoints

### `GET /health`

ML engine health (separate from `/api/health`).

**Response (200)** — loaded: `{ "ok": true, "emotions": 31, "themes": 12, "needs": 8 }`; not loaded: `{ "ok": false, "error": "MODEL_DIR=... not found; ML routes will 503 until you point at a real model" }`.

### `POST /predict`

Direct ML prediction (also used by a remote classifier server via `EMOTION_CLASSIFIER_URL`).

**Request body**

| Field | Type | Default | Notes |
|---|---|---|---|
| `text` | string | required | the story; 400 when empty |
| `guidance` | bool | `true` | attach matched guidance to the prediction |
| `counts` | object \| null | `null` | optional emotion counts for the guidance matcher |

**Response (200)** — the `EmotionEngine.predict()` dict: `{ "emotions": [{"label": "...", "score": 0.0}], "themes": [{"label": "..."}], "needs": [{"label": "..."}], ... }`. When `guidance: true` it also includes `story` and `guidance` objects (and `guidance_error` if matching failed).

**Errors**: 503 (ML engine not loaded), 400 (empty text).

### Mounted ML guidance routes

`/guidance/*`, `/recommend`, `/guidance/match`, `/guidance/audit` come from `ml/guidance_api.py` + `ml/guidance_v2.py`. Their exact request/response schemas are defined with their own FastAPI routers, so they appear fully documented in **`/docs`** (Swagger).

---

## Web pages and static files

| Route | Returns |
|---|---|
| `GET /` | main analysis page (`index.html`) |
| `GET /guidance.html` | guidance browser |
| `GET /admin.html` | provider/admin page |
| `GET /static/{name}` | files served from `web_data/` (or `STATIC_DIR` if set); 404 outside that directory |

---

## `/airouter/` mount

The airouter-py handler is mounted at `/airouter/`: a full OpenAI-compatible chat API plus its own web UI and provider-management JSON API. Admin endpoints (e.g. `/airouter/v1/providers*`) require `Authorization: Bearer $AIROUTER_ADMIN_TOKEN`.

---

## Remote ML server protocol (optional)

If the in-process model is not loaded, the engine can use a remote classifier via `EMOTION_CLASSIFIER_URL` (e.g. `http://127.0.0.1:9001`). That server must implement:

| Endpoint | Method | Body | Returns |
|---|---|---|---|
| `{base}/predict` | POST | `{"text": "...", "top_k": 3}` | `{"emotions": [{"label": "...", "score": 0.0}], "themes": [...], "needs": [...]}` |
| `{base}/health` | GET | — | HTTP 200 |
