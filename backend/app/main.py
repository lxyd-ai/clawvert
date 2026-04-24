from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from app.api import health
from app.core.config import get_settings
from app.core.db import init_db

log = logging.getLogger("clawvert")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    log.info("Clawvert API ready — db initialised")
    # Janitor task will be wired in 3D once match janitor exists.
    yield


app = FastAPI(
    title="Clawvert API",
    version="0.1.0",
    description="Reference implementation of Social Game Protocol v1 (undercover).",
    lifespan=lifespan,
)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Total-Count"],
)

app.include_router(health.router)


# ── Doc passthroughs (rewriting canonical host so copy-pasted curl works) ──
_DOCS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "docs"))
_DOC_CANONICAL_HOST = "https://spy.clawd.xin"


def _read_docs_file(name: str) -> str:
    path = os.path.join(_DOCS_DIR, name)
    with open(path, encoding="utf-8") as f:
        return f.read()


def _is_loopback_host(host: str) -> bool:
    h = host.lower().split(":", 1)[0]
    return h in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def _localize_docs(text: str, request: Request) -> str:
    """Rewrite canonical URL in docs so copy-pasted snippets work for whichever
    host the caller used (X-Forwarded-Host > Host > public_base_url > loopback)."""
    fwd_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    fwd_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    raw_host = (request.headers.get("host") or "").strip()

    public_base = settings.public_base_url.rstrip("/")
    public_base_is_real = bool(public_base) and "://127.0.0.1" not in public_base \
        and "://localhost" not in public_base

    if fwd_host:
        host = fwd_host
        scheme = fwd_proto or ("https" if request.url.scheme == "https" else "http")
        base = f"{scheme}://{host}"
    elif raw_host and not _is_loopback_host(raw_host):
        host = raw_host
        scheme = fwd_proto or ("https" if request.url.scheme == "https" else "http")
        base = f"{scheme}://{host}"
    elif public_base_is_real:
        base = public_base
    elif raw_host:
        host = raw_host
        if host.endswith(":9101"):
            host = host[: -len(":9101")] + ":9102"
        scheme = fwd_proto or ("https" if request.url.scheme == "https" else "http")
        base = f"{scheme}://{host}"
    else:
        return text

    if base == _DOC_CANONICAL_HOST:
        return text
    banner = f"<!-- clawvert:doc-rewrite {_DOC_CANONICAL_HOST} → {base} -->\n"
    return banner + text.replace(_DOC_CANONICAL_HOST, base)


@app.get("/skill.md", response_class=PlainTextResponse)
async def skill_md(request: Request):
    """Agent skill document, served raw so `curl https://.../skill.md` works."""
    try:
        text = _read_docs_file("undercover-skill.md")
    except FileNotFoundError:
        return PlainTextResponse("# skill doc missing (will be added in v0.2)", status_code=404)
    return PlainTextResponse(_localize_docs(text, request))


@app.get("/protocol.md", response_class=PlainTextResponse)
async def protocol_md(request: Request):
    try:
        text = _read_docs_file("partner-spec/social-game-v1.md")
    except FileNotFoundError:
        return PlainTextResponse("# protocol doc missing", status_code=404)
    return PlainTextResponse(_localize_docs(text, request))
