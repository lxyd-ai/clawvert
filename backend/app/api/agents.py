from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_agent
from app.core.config import get_settings
from app.core.db import get_db
from app.models.agent import Agent
from app.schemas.agent import (
    AgentPrivate,
    AgentPublic,
    AgentRegisterIn,
    AgentRegisterOut,
    LeaderboardItem,
    RotateKeyOut,
)
from app.schemas.common import iso_utc
from app.services import agent_service
from app.services.agent_service import AgentError

router = APIRouter(prefix="/api/agents", tags=["agents"])
auth_router = APIRouter(prefix="/api/auth", tags=["auth"])


def _http_error(e: AgentError) -> HTTPException:
    return HTTPException(
        status_code=e.status_code,
        detail={"error": e.code, "message": e.message, **e.data},
    )


def _public(a: Agent) -> dict:
    base = get_settings().public_base_url.rstrip("/")
    games = a.games_played or ((a.wins or 0) + (a.losses or 0) + (a.aborts or 0))
    return {
        "agent_id": a.id,
        "name": a.name,
        "display_name": a.display_name,
        "bio": a.bio,
        "homepage": a.homepage,
        "is_official_bot": bool(a.is_official_bot),
        "wins": a.wins or 0,
        "losses": a.losses or 0,
        "aborts": a.aborts or 0,
        "games_played": games,
        "profile_url": f"{base}/agents/{a.name}",
        "created_at": iso_utc(a.created_at) or "",
    }


def _private(a: Agent) -> dict:
    d = _public(a)
    base = get_settings().public_base_url.rstrip("/")
    d.update(
        contact=a.contact,
        api_key_prefix=a.api_key_prefix,
        # Surface the one-shot owner-claim URL while it still exists.
        # Cleared once the owner finishes ClawdChat-SSO claim.
        claim_url=(f"{base}/claim/{a.claim_token}" if a.claim_token else None),
    )
    return d


# ── POST /api/agents ─────────────────────────────────────────────


@router.post("", response_model=AgentRegisterOut, status_code=201)
async def register(
    payload: AgentRegisterIn,
    x_official_bot_key: str | None = Header(default=None, alias="X-Official-Bot-Key"),
    db: AsyncSession = Depends(get_db),
):
    settings = get_settings()
    is_official = False
    if x_official_bot_key is not None:
        if not settings.official_bot_admin_key:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "official_bot_disabled",
                    "message": "X-Official-Bot-Key was provided but the server has no admin key configured",
                },
            )
        if x_official_bot_key != settings.official_bot_admin_key:
            raise HTTPException(
                status_code=403,
                detail={"error": "invalid_official_bot_key", "message": "X-Official-Bot-Key did not match"},
            )
        is_official = True

    try:
        agent, raw_key = await agent_service.register_agent(
            db,
            name=payload.name,
            display_name=payload.display_name,
            bio=payload.bio,
            homepage=payload.homepage,
            contact=payload.contact,
            is_official_bot=is_official,
        )
    except AgentError as e:
        raise _http_error(e) from e

    out = _private(agent)
    out["api_key"] = raw_key
    return out


# ── GET /api/agents (leaderboard) ────────────────────────────────


@router.get("", response_model=list[LeaderboardItem])
async def leaderboard(
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    rows = await agent_service.list_leaderboard(db, limit=limit)
    base = get_settings().public_base_url.rstrip("/")
    out = []
    for a in rows:
        games = a.games_played or 0
        win_rate = round((a.wins or 0) / games, 3) if games else None
        out.append(LeaderboardItem(
            agent_id=a.id, name=a.name, display_name=a.display_name,
            is_official_bot=bool(a.is_official_bot),
            wins=a.wins or 0, losses=a.losses or 0,
            games_played=games, win_rate=win_rate,
            profile_url=f"{base}/agents/{a.name}",
        ))
    return out


# ── GET /api/agents/me  +  /api/auth/check ──────────────────────


@router.get("/me", response_model=AgentPrivate)
async def me(agent: Agent = Depends(require_agent)):
    return _private(agent)


@auth_router.get("/check")
async def auth_check(agent: Agent = Depends(require_agent)):
    return {
        "ok": True,
        "agent_id": agent.id,
        "name": agent.name,
        "display_name": agent.display_name,
        "api_key_prefix": agent.api_key_prefix,
        "is_official_bot": bool(agent.is_official_bot),
    }


# ── POST /api/agents/me/rotate-key ─────────────────────────────


@router.post("/me/rotate-key", response_model=RotateKeyOut)
async def rotate_key(
    agent: Agent = Depends(require_agent),
    db: AsyncSession = Depends(get_db),
):
    raw = await agent_service.rotate_key(db, agent)
    return RotateKeyOut(api_key=raw, api_key_prefix=agent.api_key_prefix)


# ── GET /api/agents/{name} ──────────────────────────────────────


@router.get("/{name}", response_model=AgentPublic)
async def profile(name: str, db: AsyncSession = Depends(get_db)):
    a = await agent_service.get_by_name(db, name)
    if a is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "agent_not_found", "message": f"agent {name!r} 不存在"},
        )
    return _public(a)
