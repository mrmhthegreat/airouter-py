"""Built-in provider catalog and curated free-tier model list.

Python port of ``pkg/preset/preset.go``. The module contains two things:

* ``DEFINITIONS`` — connection defaults (base URL, env var name, timezone,
  behaviour flags) for every OpenAI-compatible and Ollama provider we know
  about.
* ``FREE_POOL`` — a curated list of known-free (provider, model, limits)
  tuples grouped by category (text / tts / stt). Every entry carries a
  ``verified_on`` date because free-tier limits move.

Presets deliberately contain ONLY connection defaults and behaviour flags,
never model ids or limits: those live in ``FreePool`` because they change
more often. Limits in ``FreePool`` carry a "verified" date — when in doubt,
re-check the provider's console.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .types import (
    CATEGORY_TEXT,
    CATEGORY_TTS,
    CATEGORY_STT,
    ProviderConfig,
)


@dataclass
class Definition:
    """Connection defaults for one provider (no model ids or limits)."""

    kind: str = "openai"          # "openai" | "ollama"
    base_url: str = ""
    api_key_env: str = ""         # env var name (empty for ollama)
    reset_tz: str = "UTC"
    json_mode: Optional[bool] = None
    system_role: Optional[bool] = None
    category: str = ""            # default category for this preset's models


# Catalog of provider connection defaults. Mirrors ``Definitions`` in Go.
DEFINITIONS: dict[str, Definition] = {
    "groq": Definition(
        kind="openai",
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        reset_tz="UTC",
    ),
    "gemini": Definition(
        kind="openai",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key_env="GEMINI_API_KEY",
        # Google's free tier resets at PT midnight
        reset_tz="America/Los_Angeles",
    ),
    # Gemma via Gemini API: no JSON mode, no system role
    "gemma": Definition(
        kind="openai",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key_env="GEMINI_API_KEY",
        reset_tz="America/Los_Angeles",
        json_mode=False,
        system_role=False,
    ),
    # Gemini native generateContent endpoint for TTS + STT.
    # r.speak uses POST {base}/models/{model}:generateContent with audio modality
    # config. r.transcribe on Gemini uses Files API upload + /v1beta/interactions.
    "gemini-tts": Definition(
        kind="openai",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key_env="GEMINI_API_KEY",
        reset_tz="America/Los_Angeles",
        json_mode=False,
        system_role=False,
        category=CATEGORY_TTS,
    ),
    "openrouter": Definition(
        kind="openai",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        reset_tz="UTC",
    ),
    "mistral": Definition(
        kind="openai",
        base_url="https://api.mistral.ai/v1",
        api_key_env="MISTRAL_API_KEY",
        reset_tz="UTC",
    ),
    # Z.AI (Zhipu) GLM. The Flash models are free; they are limited by
    # concurrency (1 request at a time per model), not by tokens or RPM.
    "zai": Definition(
        kind="openai",
        base_url="https://api.z.ai/api/paas/v4",
        api_key_env="ZAI_API_KEY",
        reset_tz="UTC",
    ),
    "sambanova": Definition(
        kind="openai",
        base_url="https://api.sambanova.ai/v1",
        api_key_env="SAMBANOVA_API_KEY",
        reset_tz="UTC",
    ),
    # HuggingFace — Router endpoint that dispatches to many models.
    "huggingface": Definition(
        kind="openai",
        base_url="https://router.huggingface.co/v1",
        api_key_env="HF_TOKEN",
        reset_tz="UTC",
    ),
    # Ollama — local. No key needed by default; OLLAMA_API_KEY is OPTIONAL.
    "ollama": Definition(
        kind="ollama",
        base_url="http://localhost:11434",
        api_key_env="OLLAMA_API_KEY",
        reset_tz="UTC",
    ),
    "ollama-remote": Definition(
        kind="ollama",
        base_url="",
        api_key_env="OLLAMA_API_KEY",
        reset_tz="UTC",
    ),
    "openai": Definition(
        kind="openai",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        reset_tz="UTC",
    ),
}


def names() -> list[str]:
    """Sorted list of preset names."""
    return sorted(DEFINITIONS)


def apply(p: ProviderConfig) -> None:
    """Fill preset defaults into ``p`` for every empty field.

    Also fills ``p.name`` from preset+model when no name was given.
    Raises ``ValueError`` if the preset is unknown.
    """
    if not p.preset:
        return
    try:
        pre = DEFINITIONS[p.preset]
    except KeyError as e:
        raise ValueError(f"unknown preset {p.preset!r} (have: {', '.join(names())})") from e
    if not p.kind:
        p.kind = pre.kind
    if not p.base_url:
        p.base_url = pre.base_url
    if not p.api_key_env and not p.api_key:
        p.api_key_env = pre.api_key_env
    if not p.reset_tz:
        p.reset_tz = pre.reset_tz
    if p.json_mode is None:
        p.json_mode = pre.json_mode
    if p.system_role is None:
        p.system_role = pre.system_role
    if not p.category:
        p.category = pre.category
    if not p.name and p.model:
        p.name = f"{p.preset}-{p.model}"


# ---------------------------------------------------------------------------
# Free-pool catalog
# ---------------------------------------------------------------------------


@dataclass
class FreeModel:
    """One row of the curated free-pool catalog."""

    preset: str
    model: str
    category: str
    rpm: int = 0
    rpd: int = 0
    tpm: int = 0
    ash: int = 0   # audio seconds per hour (TTS/STT only)
    asd: int = 0   # audio seconds per day  (TTS/STT only)
    priority: int = 0
    workers: int = 0
    notes: str = ""
    verified_on: str = ""

    def to_dict(self) -> dict:
        return {
            "preset": self.preset,
            "model": self.model,
            "category": self.category,
            "rpm": self.rpm,
            "rpd": self.rpd,
            "tpm": self.tpm,
            "ash": self.ash,
            "asd": self.asd,
            "priority": self.priority,
            "workers": self.workers,
            "notes": self.notes,
            "verified_on": self.verified_on,
        }

    def to_provider_config(self) -> ProviderConfig:
        """Convert one free-pool entry into a ProviderConfig ready to be saved."""
        p = ProviderConfig(
            preset=self.preset,
            model=self.model,
            category=self.category,
            rpm=self.rpm,
            rpd=self.rpd,
            tpm=self.tpm,
            ash=self.ash,
            asd=self.asd,
            priority=self.priority,
            workers=self.workers,
        )
        apply(p)
        if self.category:
            p.category = self.category
        if p.priority == 0:
            p.priority = 100
        if p.workers == 0:
            p.workers = 2
        if not p.category:
            p.category = CATEGORY_TEXT
        return p


# The curated free-pool catalog. Sources:
#   - Gemini: https://aistudio.google.com/docs/rate-limits
#   - Groq:   https://console.groq.com/docs/rate-limits
# Limits move — every entry has a VerifiedOn date.
FREE_POOL: list[FreeModel] = [
    # ---- text chat ---------------------------------------------------------
    # Groq — generous free tier, very fast inference.
    FreeModel("groq", "openai/gpt-oss-120b", CATEGORY_TEXT, rpm=30, rpd=1000, tpm=8000,
              priority=10, workers=3, notes="OpenAI's 120B open-weight flagship. Best Groq free model for general reasoning.",
              verified_on="2026-11-17"),
    FreeModel("groq", "openai/gpt-oss-20b", CATEGORY_TEXT, rpm=30, rpd=1000, tpm=8000,
              priority=15, workers=3, notes="Smaller/faster GPT-OSS variant. Good second-line model.",
              verified_on="2026-11-17"),
    FreeModel("groq", "qwen/qwen3.8-27b", CATEGORY_TEXT, rpm=30, rpd=1000, tpm=8000,
              priority=20, workers=3, notes="Qwen 3.8 27B — strong multilingual + tool use.",
              verified_on="2026-11-17"),

    # Gemini — Google AI Studio free tier. Resets at PT midnight.
    FreeModel("gemini", "gemini-3.5-flash-lite", CATEGORY_TEXT, rpm=15, rpd=500, tpm=250000,
              priority=25, workers=2, notes="Best Gemini free chat model. Note: sampling params (temp/top_p/top_k) are deprecated on 3.x.",
              verified_on="2026-11-17"),
    FreeModel("gemini", "gemini-3.1-flash-lite", CATEGORY_TEXT, rpm=15, rpd=500, tpm=250000,
              priority=30, workers=2, notes="Previous-gen Flash-Lite. Still solid; 3.5 is preferred.",
              verified_on="2026-11-17"),
    FreeModel("gemini", "gemini-3.8-flash", CATEGORY_TEXT, rpm=5, rpd=20, tpm=250000,
              priority=35, workers=1, notes="Newest Flash. Very tight free-tier RPD (20/day) — use as burst, not baseline.",
              verified_on="2026-11-17"),
    FreeModel("gemini", "gemini-3.6-flash", CATEGORY_TEXT, rpm=5, rpd=20, tpm=250000,
              priority=40, workers=1, notes="Previous-gen Flash with computer-use support. Same tight free RPD.",
              verified_on="2026-11-17"),
    FreeModel("gemini", "gemini-3-flash", CATEGORY_TEXT, rpm=5, rpd=20, tpm=250000,
              priority=45, workers=1, notes="Gemini 3 Flash (preview).",
              verified_on="2026-11-17"),
    FreeModel("gemini", "gemini-2.5-flash-lite", CATEGORY_TEXT, rpm=10, rpd=20, tpm=250000,
              priority=50, workers=1, notes="Auto-routes to gemini-3.5-flash-lite on Dec 1 2026 — add the latter instead.",
              verified_on="2026-11-17"),
    FreeModel("gemini", "gemini-2.5-flash", CATEGORY_TEXT, rpm=5, rpd=20, tpm=250000,
              priority=55, workers=1, notes="DEPRECATED — auto-routes to gemini-3.5-flash-lite on Dec 1 2026. Avoid.",
              verified_on="2026-11-17"),
    FreeModel("gemini", "gemini-4-hydrogen", CATEGORY_TEXT, rpm=5, rpd=20, tpm=250000,
              priority=60, workers=1, notes="Gemini 4 Hydrogen (preview). Free tier is very tight.",
              verified_on="2026-11-17"),

    # Gemma via Gemini API — no JSON mode, no system role. airouter handles both.
    FreeModel("gemma", "gemma-4-31b-it", CATEGORY_TEXT, rpm=30, rpd=14400, tpm=16000,
              priority=65, workers=2, notes="Gemma 4 31B via Gemini API. No system role, no response_format — airouter handles both.",
              verified_on="2026-11-17"),
    FreeModel("gemma", "gemma-4-26b-a4b-it", CATEGORY_TEXT, rpm=30, rpd=14400, tpm=16000,
              priority=70, workers=2, notes="Smaller Gemma 4. Same Gemini-API quirks as 31B.",
              verified_on="2026-11-17"),

    # OpenRouter — free models cap at 20 RPM / 50 RPD by default
    # (1000 RPD if you've ever paid OpenRouter anything).
    FreeModel("openrouter", "meta-llama/llama-3.3-70b-instruct:free", CATEGORY_TEXT,
              rpm=20, rpd=50, tpm=0, priority=75, workers=2,
              notes=":free suffix = OpenRouter's free variant. RPD rises to 1000 after any payment.",
              verified_on="2026-11-17"),
    FreeModel("openrouter", "google/gemma-3-27b-it:free", CATEGORY_TEXT,
              rpm=20, rpd=50, tpm=0, priority=80, workers=2,
              notes="Free Gemma via OpenRouter — different route from Gemini direct.",
              verified_on="2026-11-17"),

    # Z.AI GLM Flash — free, no daily token cap and no fixed RPM. Only limit is
    # ONE concurrent request per model (a second one gets HTTP 429, code 1302).
    FreeModel("zai", "glm-4.7-flash", CATEGORY_TEXT, priority=55, workers=1,
              notes="Free. 1 concurrent request. 200K context, 128K output. Reasoning model.",
              verified_on="2026-11-20"),
    FreeModel("zai", "glm-4.5-flash", CATEGORY_TEXT, priority=78, workers=1,
              notes="Free. 1 concurrent request. 128K context, 96K output. Z.AI announced its retirement.",
              verified_on="2026-11-20"),
    FreeModel("zai", "glm-4.6v-flash", CATEGORY_TEXT, priority=82, workers=1,
              notes="Free. 1 concurrent request. 128K context, 32K output. Multimodal model; airouter sends text only.",
              verified_on="2026-11-20"),

    # SambaNova — dev tier has 20 RPM per the community thread.
    FreeModel("sambanova", "Meta-Llama-3.1-8B-Instruct", CATEGORY_TEXT, rpm=20, priority=90, workers=2,
              notes="0 = unknown. airouter will discover real limits from 429s.",
              verified_on="2026-11-17"),

    # Ollama local — no quota, but limited by your hardware. Last-resort fallback.
    FreeModel("ollama", "gemma3:4b", CATEGORY_TEXT, priority=95, workers=1,
              notes="Local fallback. Requires `ollama serve` on localhost:11434.",
              verified_on="2026-11-17"),

    # ---- text-to-speech (routed by r.speak / `airouter speak` / POST /v1/speak) ----
    # Sources:
    #   Groq:   https://console.groq.com/docs/text-to-speech (POST /openai/v1/audio/speech)
    #   Gemini: https://aistudio.google.com/docs/speech-generation (POST /v1beta/models/{model}:generateContent)
    # Default voices: Groq Orpheus English = "tara"; Gemini = "Kore".
    FreeModel("gemini-tts", "gemini-3.8-flash-tts", CATEGORY_TTS, rpm=3, rpd=10, tpm=10000,
              priority=100, workers=1,
              notes="Flagship Gemini TTS. 30 prebuilt voices (Kore, Puck, Charon, Aoede, Fenrir, Leda, Orus, Zephyr, ...). Returns WAV.",
              verified_on="2026-11-17"),
    FreeModel("gemini-tts", "gemini-3.8-flash-lite-tts", CATEGORY_TTS, rpm=3, rpd=10, tpm=10000,
              priority=105, workers=1,
              notes="Fast/cost-efficient Gemini TTS for high-volume production. Same voice set.",
              verified_on="2026-11-17"),
    FreeModel("gemini-tts", "gemini-3.1-flash-tts", CATEGORY_TTS, rpm=3, rpd=10, tpm=10000,
              priority=110, workers=1,
              notes="Legacy Gemini TTS preview. Update to 3.8 Flash TTS if possible.",
              verified_on="2026-11-17"),
    FreeModel("gemini-tts", "gemini-2.5-flash-preview-tts", CATEGORY_TTS, rpm=3, rpd=10, tpm=10000,
              priority=115, workers=1,
              notes="Gemini 2.5 Flash TTS preview. Same prebuilt voices as 3.x.",
              verified_on="2026-11-17"),
    FreeModel("groq", "canopylabs/orpheus-v1-english", CATEGORY_TTS, rpm=10, rpd=100, tpm=1200,
              priority=120, workers=1,
              notes="Orpheus English TTS via Groq. Voices: troy, hannah, austin, alex, bryan, eli, emma, jess, julia, tara. Returns WAV.",
              verified_on="2026-11-17"),
    FreeModel("groq", "canopylabs/orpheus-arabic-saudi", CATEGORY_TTS, rpm=10, rpd=100, tpm=1200,
              priority=125, workers=1,
              notes="Orpheus Arabic (Saudi) TTS via Groq. Same envelope as English variant.",
              verified_on="2026-11-17"),

    # ---- speech-to-text (routed by r.transcribe / `airouter transcribe` / POST /v1/transcribe) ----
    FreeModel("groq", "whisper-large-v3-turbo", CATEGORY_STT, rpm=20, tpm=2000, ash=7200, asd=28800,
              priority=200, workers=2,
              notes="Whisper Large v3 Turbo via Groq. 7.2K audio-sec/hour (2h), 28.8K audio-sec/day (8h). Audio seconds tracked by airouter.",
              verified_on="2026-11-17"),
    FreeModel("groq", "whisper-large-v3", CATEGORY_STT, rpm=20, tpm=2000, ash=7200, asd=28800,
              priority=205, workers=2,
              notes="Whisper Large v3 via Groq. Same audio-sec envelope as turbo variant.",
              verified_on="2026-11-17"),
    FreeModel("gemini-tts", "gemini-3.5-transcribe", CATEGORY_STT, rpm=3, rpd=25, tpm=10000,
              priority=210, workers=1,
              notes="Gemini 3.5 Transcribe. Two-step: upload file via Files API, then POST to /v1beta/interactions. Word timestamps + speaker diarization available.",
              verified_on="2026-11-17"),
]

# Categories in display order.
CATEGORIES = [CATEGORY_TEXT, CATEGORY_TTS, CATEGORY_STT]

# Categories that bootstrap imports by default — only chat-capable text models.
DEFAULT_BOOTSTRAP_CATEGORIES = [CATEGORY_TEXT]


def free_pool_for(preset: str = "") -> list[FreeModel]:
    """Entries of FREE_POOL whose preset matches. Empty preset returns everything."""
    if not preset:
        return list(FREE_POOL)
    return [m for m in FREE_POOL if m.preset == preset]


def free_pool_by_category(categories: Optional[list[str]] = None) -> list[FreeModel]:
    """Entries whose category is in ``categories``.

    Pass None/empty to get the whole list. Comparison is case-insensitive.
    """
    if not categories:
        return list(FREE_POOL)
    want = {c.strip().lower() for c in categories}
    return [m for m in FREE_POOL if m.category.lower() in want]


def catalog_verified_on() -> str:
    """Newest verified_on date in the catalog, as YYYY-MM-DD."""
    latest = ""
    for m in FREE_POOL:
        if m.verified_on and m.verified_on > latest:
            latest = m.verified_on
    return latest


def catalog_summary() -> dict[str, int]:
    out = {c: 0 for c in CATEGORIES}
    for m in FREE_POOL:
        out[m.category] = out.get(m.category, 0) + 1
    return out


__all__ = [
    "Definition",
    "DEFINITIONS",
    "FreeModel",
    "FREE_POOL",
    "CATEGORIES",
    "CATEGORY_TEXT",
    "CATEGORY_TTS",
    "CATEGORY_STT",
    "DEFAULT_BOOTSTRAP_CATEGORIES",
    "names",
    "apply",
    "free_pool_for",
    "free_pool_by_category",
    "catalog_verified_on",
    "catalog_summary",
]
