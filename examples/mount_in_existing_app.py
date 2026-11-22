"""Mount the airouter HTTP API inside your OWN FastAPI / Starlette app.

This is the "embedded server" pattern: instead of running ``airouter serve``
as a standalone process, you mount the airouter handler at a sub-path of an
existing app so the same process serves your routes AND the airouter API +
web UI.

After ``pip install -e .[server]``::

    export GROQ_API_KEY=gsk_...
    export AIROUTER_ADMIN_TOKEN=change-me
    uvicorn examples.mount_in_existing_app:app --port 8000

Then:

* Your app's routes:    http://localhost:8000/         → "Hello from my app"
* airouter web UI:      http://localhost:8000/airouter/
* airouter OpenAI API:  http://localhost:8000/airouter/v1/chat/completions
* airouter native API:  http://localhost:8000/airouter/v1/generate
* airouter health:      http://localhost:8000/airouter/healthz

Point an OpenAI SDK at it::

    from openai import OpenAI
    client = OpenAI(base_url="http://localhost:8000/airouter/v1", api_key="air_...")
    print(client.chat.completions.create(model="auto",
          messages=[{"role":"user","content":"Hi"}]).choices[0].message.content)
"""
from __future__ import annotations

import os

import airouter
from airouter.server import new_handler

# FastAPI is built on Starlette, so we can mount the Starlette app from
# new_handler() directly. If you only use Starlette (no FastAPI), the same
# pattern works with Starlette.mount().
try:
    from fastapi import FastAPI
    _HAVE_FASTAPI = True
except ImportError:  # pragma: no cover
    from starlette.applications import Starlette as _StarletteApp  # type: ignore
    _HAVE_FASTAPI = False


# --- 1. Build the airouter Client once at module load ---------------------
# Provider keys are encrypted at rest with $AIROUTER_SECRET or <db>.key.
c = airouter.open(airouter.Options(db=os.environ.get("AIROUTER_DB", "airouter.db")))
# Import the curated free-pool catalog for every provider you have a key for.
# Run this once; subsequent process starts will see the providers already in the DB.
c.add_free_pool({
    "GROQ_API_KEY": os.environ.get("GROQ_API_KEY", ""),
    "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY", ""),
    "ZAI_API_KEY": os.environ.get("ZAI_API_KEY", ""),
    "OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY", ""),
})

# Build the airouter Starlette app. admin_token enables admin endpoints.
AIROUTER_APP = new_handler(c.router, os.environ.get("AIROUTER_ADMIN_TOKEN", ""))

# --- 2. Build YOUR app -----------------------------------------------------
if _HAVE_FASTAPI:
    app = FastAPI(title="My App")

    @app.get("/")
    def root() -> dict:
        return {"message": "Hello from my app", "airouter": "mounted at /airouter/"}

    # Mount airouter under /airouter/ — every endpoint becomes /airouter/<path>.
    # The trailing slash matters: with the path below, the web UI is at
    # /airouter/ and the OpenAI API is at /airouter/v1/chat/completions.
    from starlette.routing import Mount
    app.router.routes.append(Mount("/airouter", app=AIROUTER_APP))
else:
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route
    from starlette.responses import JSONResponse

    async def _root(req):
        return JSONResponse({"message": "Hello from my app", "airouter": "mounted at /airouter/"})

    app = Starlette(routes=[
        Route("/", _root),
        Mount("/airouter", app=AIROUTER_APP),
    ])


# --- 3. Use the Client directly from your own routes ----------------------
# Because the Client lives at module scope, your own route handlers can call
# it directly without going through HTTP. This is useful when your app needs
# an LLM call internally — you skip the network hop.
if _HAVE_FASTAPI:
    @app.get("/summarize/{text}")
    def summarize(text: str) -> dict:
        """Internal LLM call — no HTTP hop, just c.chat()."""
        # Auto-failover happens transparently inside c.chat().
        try:
            summary = c.chat(f"Summarize in one sentence: {text}")
            return {"summary": summary, "provider_used": "auto"}
        except Exception as e:
            return {"error": str(e)}, 500
