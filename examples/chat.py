"""Minimal example: chat through airouter from your own Python code.

Mirrors ``examples/python/chat.py`` from the Go repo.

Set at least one provider key in the environment first, e.g.::

    export GROQ_API_KEY=gsk_...
    python examples/chat.py "Write one sentence about rain"
"""
from __future__ import annotations

import sys

import airouter


def main() -> int:
    # Open the SQLite database (creates airouter.db in the cwd if missing).
    # Provider keys are encrypted at rest with $AIROUTER_SECRET or a key file.
    c = airouter.open(airouter.Options())
    # Seed the curated free-pool catalog (text-only). Entries whose env vars
    # are not set are skipped with a clear message.
    added, skipped = c.add_free_pool({
        "GROQ_API_KEY": __import__("os").environ.get("GROQ_API_KEY", ""),
        "GEMINI_API_KEY": __import__("os").environ.get("GEMINI_API_KEY", ""),
        "ZAI_API_KEY": __import__("os").environ.get("ZAI_API_KEY", ""),
        "OPENROUTER_API_KEY": __import__("os").environ.get("OPENROUTER_API_KEY", ""),
    })
    if added:
        print(f"imported {len(added)} provider(s): {', '.join(added)}", file=sys.stderr)
    if skipped:
        print(f"skipped {len(skipped)}: {skipped[0]}", file=sys.stderr)

    prompt = " ".join(sys.argv[1:]) or "Write one sentence about rain."
    text = c.chat(prompt)
    print(text)
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
