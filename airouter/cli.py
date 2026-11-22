"""airouter command-line interface.

Python port of ``pkg/clicmd``. Every subcommand (ask, speak, transcribe,
serve, provider, tenant, usage, presets, free-pool, bootstrap, ollama, me)
is implemented as an argparse-based function. Run ``airouter --help`` for
the top-level usage.
"""
from __future__ import annotations

import argparse
import json as _json
import os
import sys
import time
from typing import Optional

from . import preset as _preset
from . import router as _router
from . import store as _store
from . import types as _types
from .router import BootstrapOptions, Options, TenantLimits, TenantPolicy
from .store import open_sqlite_store
from .types import (
    DEFAULT_TENANT_ID,
    ProviderConfig,
    Request,
    SpeakRequest,
    Tenant,
    TranscribeRequest,
)

USAGE_TEXT = """airouter — route prompts across free AI providers (Groq, Gemini, Ollama,
any OpenAI-compatible URL). Auto-switches when one hits a limit.
Supports text chat, text-to-speech (TTS) and speech-to-text (STT).

  ask [flags] PROMPT...                send a prompt (or pipe it on stdin)
  speak [flags] TEXT                   text → audio (WAV bytes; use -o to save to a file)
  transcribe [flags] FILE              audio file → text
  serve [-addr :8080]                  run the HTTP API standalone
  provider add|list|rm|enable|disable|reset|set-key|limits|import
  ollama list|add [-tenant ID] MODEL...
  tenant add|list|rotate
  me [-url U] [-key K] <command>       self-service for one user, over HTTP
  bootstrap [-preset NAME] [-category text|tts|stt|all] [-store-keys] [-tenant ID]
  usage [-tenant ID] [-day YYYY-MM-DD]
  presets                              list built-in provider presets
  free-pool [-category X]              list the curated free-tier model catalog

Database: -db FILE or $AIROUTER_DB (default ./airouter.db). Provider keys are stored
          encrypted; the secret is $AIROUTER_SECRET or <db>.key (back it up separately).
Output:   defaults to text when stdout is a pipe, pretty when interactive;
          -json / -envelope / -text override. Run '<command> -h' for flags.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _db_default() -> str:
    return os.environ.get("AIROUTER_DB", "") or "airouter.db"


def _open_router(db: str, policy: Optional[TenantPolicy] = None):
    logf = lambda fmt, *a: print(f"[airouter] {fmt.format(*a)}", file=sys.stderr) if a else print(f"[airouter] {fmt}", file=sys.stderr)
    st, _src = open_sqlite_store(db, logf)
    o = Options(store=st, logf=logf)
    if policy is not None:
        o.tenant_policy = policy
    from .router import new
    return new(o)


def _read_prompt_from_stdin_or_args(args: list[str]) -> str:
    prompt = " ".join(args)
    if not prompt or prompt == "-":
        prompt = sys.stdin.read()
    return prompt.strip()


def _stdout_is_terminal() -> bool:
    try:
        return os.isatty(sys.stdout.fileno())
    except Exception:
        return False


def _fmt_dur(sec: int) -> str:
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


def _fmt_limit(n: int) -> str:
    return "—" if n == 0 else str(n)


def _print_error(err: Exception) -> None:
    from .types import AllFailedError, ExhaustedError, TenantLimitError
    if isinstance(err, ExhaustedError):
        print(f"error: all providers are limited right now. Retry in {int(err.retry_after)}s", file=sys.stderr)
        for n, why in err.reasons.items():
            print(f"  - {n}: {why}", file=sys.stderr)
    elif isinstance(err, AllFailedError):
        print("error: every provider failed", file=sys.stderr)
        for a in err.attempts:
            print(f"  - {a.provider}: {a.error}", file=sys.stderr)
    elif isinstance(err, TenantLimitError):
        print(f"error: tenant daily limit reached; resets in {int(err.retry_after // 60)} min", file=sys.stderr)
    else:
        print("error:", err, file=sys.stderr)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_ask(args: list[str]) -> None:
    p = argparse.ArgumentParser(prog="airouter ask", add_help=False)
    p.add_argument("-db", default=_db_default())
    p.add_argument("-tenant", default=DEFAULT_TENANT_ID)
    p.add_argument("-system", default="")
    p.add_argument("-json", action="store_true")
    p.add_argument("-envelope", action="store_true")
    p.add_argument("-text", action="store_true")
    p.add_argument("-provider", default="")
    p.add_argument("-strategy", default="")
    p.add_argument("-max-tokens", type=int, default=0)
    ns, rest = p.parse_known_args(args)
    prompt = _read_prompt_from_stdin_or_args(rest)
    if not prompt:
        raise SystemExit("no prompt given (pass it as an argument or on stdin)")
    r = _open_router(ns.db)
    try:
        resp = r.generate(Request(
            tenant_id=ns.tenant, system=ns.system, prompt=prompt,
            json=ns.json, provider=ns.provider, strategy=ns.strategy, max_tokens=ns.max_tokens,
        ))
    except Exception as e:
        _print_error(e); raise SystemExit(1)
    if ns.envelope:
        print(_json.dumps(resp.to_dict(), indent=2))
    elif ns.json:
        print(_json.dumps(resp.json, indent=2) if resp.json is not None else resp.text)
    else:
        print(resp.text)
    if not ns.text and not ns.envelope and not ns.json and _stdout_is_terminal():
        print(f"[{resp.provider} / {resp.model}, {resp.tokens} tokens, {resp.latency_ms}ms]", file=sys.stderr)


def cmd_speak(args: list[str]) -> None:
    p = argparse.ArgumentParser(prog="airouter speak", add_help=False)
    p.add_argument("-db", default=_db_default())
    p.add_argument("-tenant", default=DEFAULT_TENANT_ID)
    p.add_argument("-voice", default="")
    p.add_argument("-format", default="wav")
    p.add_argument("-language", default="")
    p.add_argument("-provider", default="")
    p.add_argument("-o", default="")
    p.add_argument("-envelope", action="store_true")
    ns, rest = p.parse_known_args(args)
    text = " ".join(rest)
    if not text:
        raise SystemExit("no text given (pass it as an argument)")
    r = _open_router(ns.db)
    try:
        resp = r.speak(SpeakRequest(
            tenant_id=ns.tenant, text=text, voice=ns.voice, format=ns.format,
            language=ns.language, provider=ns.provider,
        ))
    except Exception as e:
        _print_error(e); raise SystemExit(1)
    if ns.o:
        with open(ns.o, "wb") as f:
            f.write(resp.audio)
    else:
        if _stdout_is_terminal():
            print("warning: writing binary audio to stdout. Use -o FILE to save to a file instead.", file=sys.stderr)
        sys.stdout.buffer.write(resp.audio)
    if ns.envelope or _stdout_is_terminal():
        print(f"[{resp.provider} / {resp.model}, voice={resp.voice}, {resp.content_type}, {len(resp.audio)} bytes, {resp.latency_ms}ms]", file=sys.stderr)


def cmd_transcribe(args: list[str]) -> None:
    p = argparse.ArgumentParser(prog="airouter transcribe", add_help=False)
    p.add_argument("-db", default=_db_default())
    p.add_argument("-tenant", default=DEFAULT_TENANT_ID)
    p.add_argument("-language", default="")
    p.add_argument("-prompt", default="")
    p.add_argument("-provider", default="")
    p.add_argument("-envelope", action="store_true")
    ns, rest = p.parse_known_args(args)
    if not rest:
        raise SystemExit("give an audio file path as an argument (e.g. `transcribe recording.mp3`)")
    path = rest[0]
    with open(path, "rb") as f:
        audio = f.read()
    r = _open_router(ns.db)
    try:
        resp = r.transcribe(TranscribeRequest(
            tenant_id=ns.tenant, audio=audio, filename=path,
            language=ns.language, prompt=ns.prompt, provider=ns.provider,
        ))
    except Exception as e:
        _print_error(e); raise SystemExit(1)
    print(resp.text)
    if ns.envelope or _stdout_is_terminal():
        lang = resp.language or "?"
        print(f"[{resp.provider} / {resp.model}, language={lang}, {resp.latency_ms}ms]", file=sys.stderr)


def cmd_serve(args: list[str]) -> None:
    p = argparse.ArgumentParser(prog="airouter serve", add_help=False)
    p.add_argument("-db", default=_db_default())
    p.add_argument("-addr", default=":8080")
    p.add_argument("-cors", default=os.environ.get("AIROUTER_CORS", ""))
    p.add_argument("-allow-tenant-urls", action="store_true",
                   default=os.environ.get("AIROUTER_ALLOW_TENANT_URLS") == "1")
    p.add_argument("-block-tenant-ollama", action="store_true")
    p.add_argument("-max-tenant-models", type=int, default=100)
    ns, _ = p.parse_known_args(args)
    r = _open_router(ns.db, TenantPolicy(
        allow_custom_base_url=ns.allow_tenant_urls,
        block_ollama=ns.block_tenant_ollama,
        max_providers=ns.max_tenant_models,
    ))
    admin = os.environ.get("AIROUTER_ADMIN_TOKEN", "")
    if not admin:
        print("note: AIROUTER_ADMIN_TOKEN is not set → admin endpoints are disabled; use the CLI.", file=sys.stderr)
    ts = r.store.list_tenants()
    if len(ts) <= 1:
        print("note: no tenant keys yet. Create one:  tenant add NAME", file=sys.stderr)
    import uvicorn
    from .server import new_handler
    app = new_handler(r, admin, cors=ns.cors)
    host, port = (ns.addr[1:] if ns.addr.startswith(":") else ns.addr).split(":") if ":" in ns.addr else ("0.0.0.0", ns.addr)
    print(f"airouter listening on {ns.addr} (db: {ns.db})\n  web UI:        http://localhost{ns.addr if ns.addr.startswith(':') else ':' + ns.addr}/\n  OpenAI base:   http://localhost{ns.addr if ns.addr.startswith(':') else ':' + ns.addr}/v1", file=sys.stderr)
    uvicorn.run(app, host=host, port=int(port), log_level="warning")


def cmd_provider(args: list[str]) -> None:
    if not args:
        raise SystemExit("usage: provider add|list|rm|enable|disable|reset|set-key|limits|import")
    sub, rest = args[0], args[1:]
    p = argparse.ArgumentParser(prog=f"airouter provider {sub}", add_help=False)
    p.add_argument("-db", default=_db_default())
    p.add_argument("-tenant", default="")
    if sub == "add":
        p.add_argument("-preset", default="")
        p.add_argument("-name", default="")
        p.add_argument("-kind", default="")
        p.add_argument("-base-url", default="")
        p.add_argument("-key-env", default="")
        p.add_argument("-key", default="")
        p.add_argument("-key-stdin", action="store_true")
        p.add_argument("-model", default="")
        p.add_argument("-tz", default="")
        p.add_argument("-category", default="")
        p.add_argument("-priority", type=int, default=0)
        p.add_argument("-rpm", type=int, default=0)
        p.add_argument("-rpd", type=int, default=0)
        p.add_argument("-tpm", type=int, default=0)
        p.add_argument("-ash", type=int, default=0)
        p.add_argument("-asd", type=int, default=0)
        p.add_argument("-workers", type=int, default=0)
        p.add_argument("-used-today", type=int, default=0)
        p.add_argument("-no-json-mode", action="store_true")
        p.add_argument("-no-system-role", action="store_true")
    elif sub == "limits":
        p.add_argument("-rpm", type=int, default=-1)
        p.add_argument("-rpd", type=int, default=-1)
        p.add_argument("-tpm", type=int, default=-1)
        p.add_argument("-workers", type=int, default=-1)
        p.add_argument("-priority", type=int, default=-1)
    elif sub == "set-key":
        p.add_argument("-key", default="")
    ns, rest = p.parse_known_args(rest)
    r = _open_router(ns.db, TenantPolicy(allow_custom_base_url=True))

    if sub == "add":
        cfg = ProviderConfig(
            name=ns.name, preset=ns.preset, kind=ns.kind, base_url=ns.base_url,
            api_key_env=ns.key_env, api_key=ns.key, model=ns.model, tenant_id=ns.tenant,
            reset_tz=ns.tz, category=ns.category, priority=ns.priority,
            rpm=ns.rpm, rpd=ns.rpd, tpm=ns.tpm, ash=ns.ash, asd=ns.asd,
            workers=ns.workers, used_today_hint=ns.used_today,
            json_mode=False if ns.no_json_mode else None,
            system_role=False if ns.no_system_role else None,
        )
        if ns.key_stdin:
            cfg.api_key = sys.stdin.readline().strip()
        r.save_provider(cfg)
        print(f'added provider #{cfg.id} "{cfg.name}" ({cfg.model})')

    elif sub == "list":
        rows = []
        for s in r.status(ns.tenant):
            if ns.tenant and s.tenant_id != ns.tenant:
                continue
            rpd = "-" if s.rpd == 0 else str(s.rpd)
            audio = "-"
            if s.category in ("tts", "stt"):
                ash = "-" if s.ash == 0 else str(s.ash)
                asd = "-" if s.asd == 0 else str(s.asd)
                audio = f"{s.audio_sec_today}/{ash}/{asd}"
            owner = s.tenant_id or "shared"
            rows.append(f"{s.id}\t{owner}\t{s.category}\t{s.name}\t{s.model}\t{s.state}\t{s.key_source}\t{s.used_today}/{rpd}\t{s.rpm}\t{s.tpm}\t{audio}\t{_fmt_dur(s.resets_in_seconds)}\t{s.reset_tz}")
        print("ID\tOWNER\tCAT\tNAME\tMODEL\tSTATE\tKEY\tUSED/RPD\tRPM\tTPM\tAUDIO SEC (ASH/ASD)\tRESETS IN\tTZ")
        for row in rows:
            print(row)

    elif sub == "set-key":
        if not rest:
            raise SystemExit("give the provider id (see: provider list)")
        id = int(rest[0])
        k = ns.key or sys.stdin.readline().strip()
        if ns.tenant:
            r.set_tenant_provider_key(ns.tenant, id, k)
        else:
            r.set_provider_key(id, k)
        print(f"key stored (encrypted) for provider #{id}; any pause was lifted")

    elif sub == "limits":
        if not rest:
            raise SystemExit("give the provider id (see: provider list)")
        id = int(rest[0])
        opt = lambda v: None if v < 0 else v
        owner = ns.tenant
        if not owner:
            for p in r.store.list_providers():
                if p.id == id:
                    owner = p.tenant_id
        if not owner:
            raise SystemExit("that model is shared; edit it with `provider import` or the admin API")
        r.update_tenant_limits(owner, id, TenantLimits(
            rpm=opt(ns.rpm), rpd=opt(ns.rpd), tpm=opt(ns.tpm),
            workers=opt(ns.workers), priority=opt(ns.priority),
        ))
        print(f"limits updated for provider #{id}")

    elif sub in ("rm", "reset", "enable", "disable"):
        if not rest:
            raise SystemExit("give the provider id (see: provider list)")
        id = int(rest[0])
        if sub == "rm":
            (r.delete_tenant_provider(ns.tenant, id) if ns.tenant else r.delete_provider(id))
        elif sub == "reset":
            (r.reset_tenant_provider(ns.tenant, id) if ns.tenant else r.clear_pause(id))
        else:
            dis = sub == "disable"
            (r.set_tenant_provider_disabled(ns.tenant, id, dis) if ns.tenant else r.set_disabled(id, dis))
        print(f"{sub} provider #{id}")

    elif sub == "import":
        if not rest:
            raise SystemExit("usage: provider import [-tenant ID] providers.json")
        with open(rest[0], "r", encoding="utf-8") as f:
            data = _json.load(f)
        providers_in = data["providers"] if isinstance(data, dict) else data
        if ns.tenant:
            added, failed = r.import_tenant_providers(ns.tenant, [
                ProviderConfig(
                    name=p.get("name", ""), preset=p.get("preset", ""), model=p.get("model", ""),
                    api_key=p.get("api_key", ""), base_url=p.get("base_url", ""), kind=p.get("kind", ""),
                    priority=int(p.get("priority", 0) or 0), rpm=int(p.get("rpm", 0) or 0),
                    rpd=int(p.get("rpd", 0) or 0), tpm=int(p.get("tpm", 0) or 0),
                    workers=int(p.get("workers", 0) or 0),
                ) for p in providers_in
            ])
            for n in added:
                print("imported", n, "for tenant", ns.tenant)
            for n, why in failed.items():
                print("skipped", n + ":", why, file=sys.stderr)
        else:
            existing = {p.name: p.id for p in r.store.list_providers()}
            for i, q in enumerate(providers_in):
                cfg = ProviderConfig(
                    name=q.get("name", ""), preset=q.get("preset", ""), model=q.get("model", ""),
                    api_key=q.get("api_key", ""), base_url=q.get("base_url", ""), kind=q.get("kind", ""),
                    tenant_id=q.get("tenant_id", ""), priority=int(q.get("priority", 0) or (i + 1) * 10),
                    rpm=int(q.get("rpm", 0) or 0), rpd=int(q.get("rpd", 0) or 0),
                    tpm=int(q.get("tpm", 0) or 0), workers=int(q.get("workers", 0) or 0),
                )
                if cfg.name in existing:
                    cfg.id = existing[cfg.name]
                r.save_provider(cfg)
                print(f"imported #{cfg.id} {cfg.name} ({cfg.model})")
    else:
        raise SystemExit(f"unknown provider command: {sub}")


def cmd_tenant(args: list[str]) -> None:
    if not args:
        raise SystemExit("usage: tenant add NAME [-limit N] [-tz ZONE] | tenant list | tenant rotate NAME")
    sub, rest = args[0], args[1:]
    p = argparse.ArgumentParser(prog=f"airouter tenant {sub}", add_help=False)
    p.add_argument("-db", default=_db_default())
    p.add_argument("-limit", type=int, default=0)
    p.add_argument("-tz", default="UTC")
    ns, rest = p.parse_known_args(rest)
    r = _open_router(ns.db)
    if sub == "add":
        if not rest:
            raise SystemExit("give a tenant id, e.g. tenant add acme")
        t, key = r.create_tenant(Tenant(id=rest[0], tz=ns.tz, daily_request_limit=ns.limit))
        print(f'tenant "{t.id}" created.\nAPI key (shown once): {key}')
    elif sub == "rotate":
        if not rest:
            raise SystemExit("give a tenant id, e.g. tenant rotate acme")
        key = r.rotate_tenant_key(rest[0])
        print(f'new API key for "{rest[0]}" (shown once; the old key no longer works): {key}')
    elif sub == "list":
        ts = r.store.list_tenants()
        print("ID\tTZ\tDAILY LIMIT\tCREATED")
        for t in ts:
            created = time.strftime("%Y-%m-%d", time.gmtime(t.created_at))
            print(f"{t.id}\t{t.tz}\t{t.daily_request_limit}\t{created}")
    else:
        raise SystemExit(f"unknown tenant command: {sub}")


def cmd_usage(args: list[str]) -> None:
    p = argparse.ArgumentParser(prog="airouter usage", add_help=False)
    p.add_argument("-db", default=_db_default())
    p.add_argument("-tenant", default=DEFAULT_TENANT_ID)
    p.add_argument("-day", default="")
    ns, _ = p.parse_known_args(args)
    r = _open_router(ns.db)
    rep = r.usage(ns.tenant, ns.day)
    print(f"tenant {rep.tenant}: {rep.requests_today} requests, {rep.tokens_today} tokens (tenant daily limit: {rep.daily_limit})\n")
    print("PROVIDER\tMODEL\tDAY\tYOURS\tTOKENS\tERRORS\tALL TENANTS\tLIMIT/DAY\tRESETS IN")
    for u in rep.providers:
        print(f"{u.provider}\t{u.model}\t{u.day}\t{u.tenant_requests}\t{u.tenant_tokens}\t{u.tenant_errors}\t{u.all_requests}\t{u.limit_per_day}\t{_fmt_dur(u.resets_in_seconds)}")


def cmd_presets(args: list[str]) -> None:
    print("NAME\tKIND\tBASE URL\tKEY ENV\tRESET TZ")
    for name in _preset.names():
        d = _preset.DEFINITIONS[name]
        print(f"{name}\t{d.kind}\t{d.base_url}\t{d.api_key_env or '(none)'}\t{d.reset_tz}")


def cmd_free_pool(args: list[str]) -> None:
    p = argparse.ArgumentParser(prog="airouter free-pool", add_help=False)
    p.add_argument("-category", default="all")
    p.add_argument("-json", action="store_true")
    ns, _ = p.parse_known_args(args)
    categories = None
    if ns.category.strip() != "all":
        categories = [c.strip() for c in ns.category.split(",") if c.strip()]
    models = _preset.free_pool_by_category(categories)
    if ns.json:
        print(_json.dumps({
            "models": [m.to_dict() for m in models],
            "verified_on": _preset.catalog_verified_on(),
            "summary": _preset.catalog_summary(),
        }, indent=2))
        return
    summary = _preset.catalog_summary()
    print(f"catalog: {len(_preset.FREE_POOL)} models across {len(_preset.CATEGORIES)} categories (verified {_preset.catalog_verified_on()})", file=sys.stderr)
    if summary:
        print("by category: " + ", ".join(f"{c}={summary[c]}" for c in _preset.CATEGORIES), file=sys.stderr)
    print(f"showing: {', '.join(categories) if categories else 'all'}\n", file=sys.stderr)
    print("CATEGORY\tPRESET\tMODEL\tRPM\tRPD\tTPM\tASH\tASD\tPRIO\tWORKERS\tVERIFIED\tNOTES")
    for m in models:
        print(f"{m.category}\t{m.preset}\t{m.model}\t{_fmt_limit(m.rpm)}\t{_fmt_limit(m.rpd)}\t{_fmt_limit(m.tpm)}\t{_fmt_limit(m.ash)}\t{_fmt_limit(m.asd)}\t{m.priority}\t{m.workers}\t{m.verified_on}\t{m.notes}")
    print("\nlimits move — re-check the provider's console before relying on these numbers.", file=sys.stderr)
    print("sources: aistudio.google.com/docs/rate-limits · console.groq.com/docs/rate-limits", file=sys.stderr)
    print("ASH = audio seconds per hour, ASD = audio seconds per day (TTS/STT only).", file=sys.stderr)


def cmd_bootstrap(args: list[str]) -> None:
    p = argparse.ArgumentParser(prog="airouter bootstrap", add_help=False)
    p.add_argument("-db", default=_db_default())
    p.add_argument("-preset", default="")
    p.add_argument("-category", default="text")
    p.add_argument("-overwrite", action="store_true")
    p.add_argument("-store-keys", action="store_true")
    p.add_argument("-tenant", default="")
    ns, _ = p.parse_known_args(args)
    if ns.category.strip() == "all":
        categories: Optional[list[str]] = []
    else:
        categories = [c.strip() for c in ns.category.split(",") if c.strip()]
    r = _open_router(ns.db)
    added, skipped = r.bootstrap_providers_opts(BootstrapOptions(
        overwrite=ns.overwrite, preset=ns.preset, categories=categories,
        store_keys=ns.store_keys, tenant_id=ns.tenant,
    ))
    if not added and not skipped:
        raise SystemExit("no providers matched — check the -preset and -category values")
    label = "text (default)" if categories is None else ("all" if not categories else ", ".join(categories))
    print(f"importing categories: {label}", file=sys.stderr)
    print(f"added {len(added)} provider(s):")
    for name in added:
        print(f"  + {name}")
    if skipped:
        print(f"\nskipped {len(skipped)}:")
        for why in skipped:
            print(f"  - {why}")
    print(f"\ncatalog verified: {_preset.catalog_verified_on()}")
    print("limits move — re-check the provider's console before relying on these numbers.")
    print("\nnext: set the env vars named above, then `provider list` to see live state.")


def cmd_ollama(args: list[str]) -> None:
    if not args:
        raise SystemExit("usage: ollama list | ollama add [-tenant ID] MODEL...")
    sub = args[0]
    p = argparse.ArgumentParser(prog=f"airouter ollama {sub}", add_help=False)
    p.add_argument("-db", default=_db_default())
    p.add_argument("-tenant", default="")
    ns, rest = p.parse_known_args(args[1:])
    r = _open_router(ns.db, TenantPolicy(allow_custom_base_url=True))
    if sub == "list":
        ms = r.ollama_models()
        for m in ms:
            print(f"{m['name']:<30} {m.get('parameter_size',''):>6}  {m.get('size_gb','')} GB")
        if not ms:
            print("no models installed; try: ollama pull gemma3:4b")
    elif sub == "add":
        if not rest:
            raise SystemExit("give at least one model name (see: ollama list)")
        if not ns.tenant:
            for m in rest:
                cfg = ProviderConfig(preset="ollama", name=f"ollama-{m}", model=m, priority=90, workers=1)
                r.save_provider(cfg)
                print(f'added shared provider #{cfg.id} "{cfg.name}"')
        else:
            added, skipped = r.add_tenant_ollama(ns.tenant, rest)
            for a in added:
                print("added", a, "for tenant", ns.tenant)
            for s in skipped:
                print("skipped", s)
    else:
        raise SystemExit(f"unknown ollama command: {sub}")


def cmd_me(args: list[str]) -> None:
    """Self-service CLI for one tenant over HTTP. Talks to a running server with the tenant's own key."""
    import urllib.request
    import urllib.error

    ME_USAGE = """usage: me [-url URL] [-key KEY] <command>

  whoami                                 who am I
  list                                   my models (+ shared ones, read-only)
  catalog                                providers and models I can pick from
  add -preset P -model M [-key-stdin]    add one model
  pick -preset P [-key-stdin] MODEL...   add several catalog models with one key
  ollama [add MODEL...]                  list server Ollama models, or add some
  limits ID [-rpm N -rpd N -tpm N -workers N -priority N]
  set-key ID [-key-stdin]                replace a model's API key
  enable|disable|reset|rm ID
  import FILE.json | export              JSON file in / out
  usage [-day YYYY-MM-DD]                my usage
  update [-name X] [-tz Z]               my profile
  rotate-key                             new API key (the old one stops working)

flags (before the command):
  -url  server (default $AIROUTER_URL or http://localhost:8080)
  -key  my tenant key (default $AIROUTER_KEY)"""

    p = argparse.ArgumentParser(prog="airouter me", add_help=False)
    p.add_argument("-url", default=os.environ.get("AIROUTER_URL", "http://localhost:8080"))
    p.add_argument("-key", default=os.environ.get("AIROUTER_KEY", ""))
    ns, rest = p.parse_known_args(args)
    if not rest:
        raise SystemExit(ME_USAGE)
    if not ns.key:
        raise SystemExit("missing API key: set $AIROUTER_KEY or pass -key air_...")
    cmd = rest[0]
    sub_args = rest[1:]
    base = ns.url.rstrip("/")
    headers = {"Authorization": f"Bearer {ns.key}", "Content-Type": "application/json"}

    def call(method: str, path: str, body=None) -> bytes:
        data = _json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")
            try:
                msg = _json.loads(txt).get("error", txt)
            except ValueError:
                msg = txt
            raise SystemExit(f"HTTP {e.code}: {msg}")

    def pretty(b: bytes) -> None:
        try:
            v = _json.loads(b)
            print(_json.dumps(v, indent=2))
        except ValueError:
            print(b.decode("utf-8", "replace"))

    sp = argparse.ArgumentParser(prog=f"airouter me {cmd}", add_help=False)
    sp.add_argument("-preset", default="")
    sp.add_argument("-model", default="")
    sp.add_argument("-name", default="")
    sp.add_argument("-tz", default="")
    sp.add_argument("-day", default="")
    sp.add_argument("-rpm", type=int, default=-1)
    sp.add_argument("-rpd", type=int, default=-1)
    sp.add_argument("-tpm", type=int, default=-1)
    sp.add_argument("-workers", type=int, default=-1)
    sp.add_argument("-priority", type=int, default=-1)
    sp.add_argument("-key-stdin", action="store_true")
    sns, srest = sp.parse_known_args(sub_args)

    if cmd == "whoami":
        pretty(call("GET", "/v1/me"))
    elif cmd == "list":
        out = call("GET", "/v1/me/providers")
        # Render as a simple table
        try:
            v = _json.loads(out)
            print("ID\tNAME\tMODEL\tSTATE\tUSED\tRPM\tRPD\tTPM")
            for m in v.get("own", []):
                print(f"{m.get('id','-')}\t{m.get('name','-')}\t{m.get('model','-')}\t{m.get('state','-')}\t{m.get('used_today','-')}\t{m.get('rpm','-')}\t{m.get('rpd','-')}\t{m.get('tpm','-')}")
            if not v.get("own"):
                print("(you have no models of your own yet: try `me catalog` or `me pick`)")
            if v.get("shared"):
                print(f"\n{len(v['shared'])} shared model(s) from the server also serve your requests.")
        except ValueError:
            print(out.decode("utf-8", "replace"))
    elif cmd == "catalog":
        pretty(call("GET", "/v1/me/catalog"))
    elif cmd == "add":
        body = {}
        if sns.preset: body["preset"] = sns.preset
        if sns.model: body["model"] = sns.model
        if sns.name: body["name"] = sns.name
        if sns.rpm >= 0: body["rpm"] = sns.rpm
        if sns.rpd >= 0: body["rpd"] = sns.rpd
        if sns.tpm >= 0: body["tpm"] = sns.tpm
        if sns.workers >= 0: body["workers"] = sns.workers
        if sns.priority >= 0: body["priority"] = sns.priority
        if sns.key_stdin:
            body["api_key"] = sys.stdin.readline().strip()
        pretty(call("POST", "/v1/me/providers", body))
    elif cmd == "pick":
        if not srest:
            raise SystemExit("give at least one model id")
        key = sys.stdin.readline().strip() if sns.key_stdin else ""
        pretty(call("POST", "/v1/me/catalog/add", {"preset": sns.preset, "api_key": key, "models": srest}))
    elif cmd == "ollama":
        if srest and srest[0] == "add":
            pretty(call("POST", "/v1/me/ollama/add", {"models": srest[1:]}))
        else:
            pretty(call("GET", "/v1/me/ollama"))
    elif cmd == "limits":
        if not srest:
            raise SystemExit("expected one model id (see `me list`)")
        body = {}
        if sns.rpm >= 0: body["rpm"] = sns.rpm
        if sns.rpd >= 0: body["rpd"] = sns.rpd
        if sns.tpm >= 0: body["tpm"] = sns.tpm
        if sns.workers >= 0: body["workers"] = sns.workers
        if sns.priority >= 0: body["priority"] = sns.priority
        pretty(call("POST", f"/v1/me/providers/{srest[0]}/limits", body))
    elif cmd == "set-key":
        if not srest:
            raise SystemExit("expected one model id (see `me list`)")
        k = sys.stdin.readline().strip() if sns.key_stdin else input("New provider API key: ").strip()
        pretty(call("POST", f"/v1/me/providers/{srest[0]}/key", {"api_key": k}))
    elif cmd in ("enable", "disable", "reset"):
        if not srest:
            raise SystemExit("expected one model id (see `me list`)")
        pretty(call("POST", f"/v1/me/providers/{srest[0]}/{cmd}"))
    elif cmd == "rm":
        if not srest:
            raise SystemExit("expected one model id (see `me list`)")
        pretty(call("DELETE", f"/v1/me/providers/{srest[0]}"))
    elif cmd == "import":
        if not srest:
            raise SystemExit("usage: me import FILE.json")
        with open(srest[0], "r", encoding="utf-8") as f:
            data = _json.load(f)
        if isinstance(data, list):
            data = {"providers": data}
        pretty(call("POST", "/v1/me/import", data))
    elif cmd == "export":
        pretty(call("GET", "/v1/me/export"))
    elif cmd == "usage":
        path = "/v1/me/usage" + (f"?day={sns.day}" if sns.day else "")
        pretty(call("GET", path))
    elif cmd == "update":
        body = {}
        if sns.name: body["name"] = sns.name
        if sns.tz: body["tz"] = sns.tz
        pretty(call("PATCH", "/v1/me", body))
    elif cmd == "rotate-key":
        pretty(call("POST", "/v1/me/rotate-key"))
    else:
        raise SystemExit(ME_USAGE)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


COMMANDS = {
    "ask": cmd_ask,
    "speak": cmd_speak,
    "transcribe": cmd_transcribe,
    "serve": cmd_serve,
    "provider": cmd_provider,
    "tenant": cmd_tenant,
    "usage": cmd_usage,
    "presets": cmd_presets,
    "free-pool": cmd_free_pool,
    "bootstrap": cmd_bootstrap,
    "ollama": cmd_ollama,
    "me": cmd_me,
}


def main(argv: Optional[list[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE_TEXT)
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    handler = COMMANDS.get(cmd)
    if handler is None:
        print(USAGE_TEXT)
        return 2
    try:
        handler(rest)
    except SystemExit:
        raise
    except Exception as e:
        _print_error(e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
