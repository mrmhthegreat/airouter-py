"""Embedded usage — import airouter as a library in your own Python code.

This example shows every shape the library exposes:

* ``airouter.open(Options(db=...))``    — open the SQLite-backed client
* ``airouter.new_client(store, opts)``   — bring your own Store (memory, postgres, ...)
* ``c.chat(prompt)``                     — quick prompt → text
* ``c.ask(Request(...))``                — full request/response (system, JSON mode, max_tokens, ...)
* ``c.generate(Request(...))``           — same as ``ask``, returns a Response with attempts/latency
* ``c.speak(SpeakRequest(...))``         — text → audio bytes
* ``c.transcribe(TranscribeRequest(...))`` — audio bytes → text
* ``c.status()`` / ``c.usage(tenant)``   — live provider status / usage report

Run it after ``pip install -e .`` and with at least one provider key set::

    export GROQ_API_KEY=gsk_...
    python examples/embedded.py
"""
from __future__ import annotations

import os
import sys

import airouter


def main() -> int:
    # --- 1. Open the client ------------------------------------------------
    # airouter.open() opens the SQLite database (default ./airouter.db) with
    # provider keys encrypted at rest ($AIROUTER_SECRET or <db>.key).
    c = airouter.open(airouter.Options(db="ai.db"))

    # --- 2. One-time: seed the curated free-pool catalog -------------------
    # Pass the keys you have. Entries whose key is empty are skipped.
    added, skipped = c.add_free_pool({
        "GROQ_API_KEY": os.environ.get("GROQ_API_KEY", ""),
        "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY", ""),
        "ZAI_API_KEY": os.environ.get("ZAI_API_KEY", ""),
        "OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY", ""),
    })
    print(f"imported {len(added)} provider(s): {', '.join(added) or '(none)'}")
    if skipped:
        print(f"skipped {len(skipped)} (set their env vars to import them too)")

    if not added:
        print("\nno providers imported — set at least one of GROQ_API_KEY / "
              "GEMINI_API_KEY / ZAI_API_KEY / OPENROUTER_API_KEY and re-run.")
        return 2

    # --- 3. Quick chat: prompt → text --------------------------------------
    print("\n--- c.chat(prompt) ---")
    text = c.chat("Write one sentence about rain.")
    print(text)

    # --- 4. Full control: Request → Response -------------------------------
    print("\n--- c.ask(Request(system=..., prompt=..., json=True)) ---")
    resp = c.ask(airouter.Request(
        system="Reply as a JSON object with one key 'emotion'.",
        prompt="I failed my exam today.",
        json=True,
        max_tokens=100,
    ))
    print("text:", resp.text)
    print("json:", resp.json)
    print("provider:", resp.provider, "/ model:", resp.model)
    print("tokens:", resp.tokens, "/ latency_ms:", resp.latency_ms)
    print("attempts (failed providers before the successful one):", resp.attempts)

    # --- 5. Pin a specific provider ----------------------------------------
    print("\n--- c.generate(Request(provider='groq-openai/gpt-oss-120b', ...)) ---")
    try:
        resp = c.generate(airouter.Request(
            prompt="Say hi in 5 words.",
            provider="groq-openai/gpt-oss-120b",
        ))
        print(f"[{resp.provider}] {resp.text}")
    except Exception as e:
        print(f"could not pin that provider: {e}")

    # --- 6. Live status / usage report -------------------------------------
    print("\n--- c.status() ---")
    for s in c.status():
        print(f"  #{s.id} {s.name:30s} state={s.state:12s} used={s.used_today}/{s.rpd or '-'}")

    print("\n--- c.usage('default') ---")
    rep = c.usage("default")
    print(f"  tenant={rep.tenant} requests={rep.requests_today} tokens={rep.tokens_today}")
    for u in rep.providers[:3]:
        print(f"    {u.provider}: {u.tenant_requests} req, {u.tenant_tokens} tok")

    # --- 7. TTS (text → audio) ---------------------------------------------
    # Uncomment after `airouter bootstrap -category tts` (or pass categories
    # above) and setting GEMINI_API_KEY.
    #
    # print("\n--- c.speak(SpeakRequest(text='Hello world', voice='Kore')) ---")
    # resp = c.speak(airouter.SpeakRequest(text="Hello world", voice="Kore", format="wav"))
    # with open("hello.wav", "wb") as f:
    #     f.write(resp.audio)
    # print(f"wrote {len(resp.audio)} bytes via {resp.provider}/{resp.model} voice={resp.voice}")

    # --- 8. STT (audio → text) ---------------------------------------------
    # print("\n--- c.transcribe(TranscribeRequest(audio=..., filename='rec.wav')) ---")
    # with open("rec.wav", "rb") as f:
    #     audio = f.read()
    # resp = c.transcribe(airouter.TranscribeRequest(audio=audio, filename="rec.wav", language="en"))
    # print(f"text: {resp.text!r}  provider={resp.provider}")

    # --- 9. Custom Store (memory only, no SQLite) --------------------------
    # Useful in tests or short-lived scripts. The EncryptedStore wrapper is
    # only relevant when keys hit disk — MemoryStore keeps them in RAM.
    print("\n--- airouter.new_client(airouter.MemoryStore(), airouter.Options()) ---")
    mem = airouter.new_client(airouter.MemoryStore(), airouter.Options())
    print(f"in-memory client created; default tenant: {mem.store.tenant_by_id('default').id}")

    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
