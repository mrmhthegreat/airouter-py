"""Serve the airouter HTTP API + web UI from a single command.

Mirrors ``examples/python/manage.py`` (and the ``serve`` CLI subcommand).

Set your provider keys in the environment first, then run::

    export GROQ_API_KEY=gsk_...
    export AIROUTER_ADMIN_TOKEN=change-me
    python examples/serve.py

Open http://localhost:8080/ in a browser to see the web UI.
"""
from __future__ import annotations

import os
import sys

import airouter


def main() -> int:
    c = airouter.open(airouter.Options())
    c.add_free_pool({
        "GROQ_API_KEY": os.environ.get("GROQ_API_KEY", ""),
        "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY", ""),
        "ZAI_API_KEY": os.environ.get("ZAI_API_KEY", ""),
        "OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY", ""),
    })

    admin = os.environ.get("AIROUTER_ADMIN_TOKEN", "")
    if not admin:
        print("warning: AIROUTER_ADMIN_TOKEN is not set → admin endpoints are disabled", file=sys.stderr)
    app = c.handler(admin)

    import uvicorn
    print("listening on http://localhost:8080/  (OpenAI base: http://localhost:8080/v1)", file=sys.stderr)
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
