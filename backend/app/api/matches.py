"""HTTP layer for social-game-v1 / undercover.

Eight core endpoints:
    POST   /api/matches                      create
    POST   /api/matches/{id}/join            join (auto-begins on full)
    POST   /api/matches/{id}/action          speak | vote | skip | concede | whisper
    GET    /api/matches/{id}                 viewer-scoped snapshot
    GET    /api/matches/{id}/events          long-poll w/ ?since=&wait=
    POST   /api/matches/{id}/abort           host cancels waiting room
    POST   /api/matches/{id}/resign          alias for action=concede
    GET    /api/matches                      lobby list

Plus a placeholder HTML page for `?html=1` rendering (real page lands in 3E).
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import optional_agent
from app.core.config import get_settings
from app.core.db import get_db
from app.models.agent import Agent
from app.models.match import MatchEvent, MatchPlayer
from app.schemas.common import ErrorResp
from app.schemas.match import (
    ActionIn,
    ActionOut,
    CreateMatchIn,
    CreateMatchOut,
    EventsOut,
    JoinMatchIn,
    JoinMatchOut,
    MatchListItem,
    SnapshotOut,
)
from app.services import event_bus, match_service
from app.services.match_service import MatchError, hash_token
from app.services.views import (
    event_visible,
    project_event,
    project_match,
    resolve_viewer,
)

router = APIRouter(prefix="/api", tags=["matches"])


def _http_error(exc: MatchError) -> HTTPException:
    detail: dict[str, Any] = {"error": exc.code, "message": exc.message}
    if exc.data:
        detail.update(exc.data)
    return HTTPException(status_code=exc.status_code, detail=detail)


def _resolve_caller_owner(agent: Agent | None) -> str | None:
    return agent.owner_id if agent else None


async def _load_seat_for_caller(
    db: AsyncSession, match_id: str, agent: Agent | None
) -> int | None:
    """Find which seat (if any) the bearer-authenticated agent occupies.

    Used by GET endpoints (snapshot/events) where a Bearer alone is enough
    to know "are you a player or just a viewer".
    """
    if agent is None:
        return None
    res = await db.execute(
        select(MatchPlayer.seat).where(
            MatchPlayer.match_id == match_id,
            MatchPlayer.agent_id == agent.id,
        )
    )
    row = res.first()
    return int(row[0]) if row else None


async def _require_seat(
    db: AsyncSession,
    match_id: str,
    agent: Agent | None,
    play_token: str | None,
) -> int:
    """Enforced seat lookup for write endpoints: action / abort / resign.

    Rules (per §1.2.5 / §7.1):
        - Bearer api_key required (agent != None).
        - X-Play-Token (or body.play_token) required.
        - The token's sha256 hash must match the seat the bearer agent
          occupies in this match — owners can't reuse another agent's
          token to dance into a seat.
    """
    if agent is None:
        raise HTTPException(status_code=401, detail={
            "error": "auth_required",
            "message": "Bearer api_key required for this action",
        })
    if not play_token:
        raise HTTPException(status_code=401, detail={
            "error": "play_token_required",
            "message": "X-Play-Token header (or body.play_token) is required",
        })
    res = await db.execute(
        select(MatchPlayer.seat, MatchPlayer.play_token_hash)
        .where(MatchPlayer.match_id == match_id,
               MatchPlayer.agent_id == agent.id)
    )
    row = res.first()
    if row is None:
        raise HTTPException(status_code=403, detail={
            "error": "agent_not_in_match",
            "message": "your agent does not hold a seat in this match",
        })
    seat, token_hash = int(row[0]), row[1]
    if hash_token(play_token) != token_hash:
        raise HTTPException(status_code=403, detail={
            "error": "play_token_mismatch",
            "message": "play_token does not match your seat in this match",
        })
    return seat


# ──────────────────────────────────────────────────────────────────
# POST /api/matches
# ──────────────────────────────────────────────────────────────────


@router.post(
    "/matches",
    response_model=CreateMatchOut,
    status_code=201,
    responses={400: {"model": ErrorResp}, 422: {"model": ErrorResp}},
)
async def create_match_endpoint(
    payload: CreateMatchIn,
    request: Request,
    agent: Agent | None = Depends(optional_agent),
    db: AsyncSession = Depends(get_db),
):
    if agent is None and not (payload.player and payload.player.name):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "identity_required",
                "message": "creator needs Authorization: Bearer dev-<name> "
                           "or body.player.name",
            },
        )
    try:
        match, host, play_token = await match_service.create_match(
            db,
            config_in=payload.config.model_dump(exclude_none=True),
            host_agent=agent,
            host_name=(payload.player.name if payload.player else None),
            host_display_name=(payload.player.display_name if payload.player else None),
        )
    except MatchError as e:
        raise _http_error(e) from e

    base = str(request.base_url).rstrip("/")
    return CreateMatchOut(
        match_id=match.id,
        status=match.status,
        your_seat=host.seat,
        play_token=play_token,
        join_url=f"{base}/api/matches/{match.id}/join",
        page_url=f"{base}/api/matches/{match.id}/page",
        config=match.config or {},
    )


# ──────────────────────────────────────────────────────────────────
# POST /api/matches/{id}/join
# ──────────────────────────────────────────────────────────────────


@router.post(
    "/matches/{match_id}/join",
    response_model=JoinMatchOut,
    responses={409: {"model": ErrorResp}, 422: {"model": ErrorResp}},
)
async def join_match_endpoint(
    match_id: str,
    payload: JoinMatchIn,
    agent: Agent | None = Depends(optional_agent),
    db: AsyncSession = Depends(get_db),
):
    if agent is None and not (payload.player and payload.player.name):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "identity_required",
                "message": "join needs Authorization: Bearer dev-<name> "
                           "or body.player.name",
            },
        )
    try:
        match, joined, play_token = await match_service.join_match(
            db,
            match_id,
            agent=agent,
            guest_name=(payload.player.name if payload.player else None),
            guest_display_name=(payload.player.display_name if payload.player else None),
        )
    except MatchError as e:
        raise _http_error(e) from e
    return JoinMatchOut(
        match_id=match.id,
        status=match.status,
        your_seat=joined.seat,
        play_token=play_token,
        config=match.config or {},
    )


# ──────────────────────────────────────────────────────────────────
# POST /api/matches/{id}/action
# ──────────────────────────────────────────────────────────────────


@router.post(
    "/matches/{match_id}/action",
    response_model=ActionOut,
    responses={401: {"model": ErrorResp}, 403: {"model": ErrorResp},
               409: {"model": ErrorResp}, 422: {"model": ErrorResp}},
)
async def action_endpoint(
    match_id: str,
    payload: ActionIn,
    agent: Agent | None = Depends(optional_agent),
    db: AsyncSession = Depends(get_db),
    x_play_token: str | None = Header(default=None, alias="X-Play-Token"),
):
    play_token = x_play_token or payload.play_token
    seat = await _require_seat(db, match_id, agent, play_token)
    # Anti-cheat (§7.1) — owner_id never acts directly; only the agent
    # they authenticated as may submit actions, and that's exactly what
    # `optional_agent` resolves. Owner-only sessions are read-only by design.
    try:
        latest_seq, summary = await match_service.submit_action(
            db, match_id,
            seat=seat,
            action_type=payload.type,
            text=payload.text,
            target_seat=payload.target_seat,
        )
    except MatchError as e:
        raise _http_error(e) from e
    await match_service.touch_seat(db, match_id, seat)
    return ActionOut(accepted=True, summary=summary, latest_seq=latest_seq)


# ──────────────────────────────────────────────────────────────────
# GET /api/matches/{id}
# ──────────────────────────────────────────────────────────────────


@router.get(
    "/matches/{match_id}",
    response_model=SnapshotOut,
    responses={404: {"model": ErrorResp}},
)
async def snapshot_endpoint(
    match_id: str,
    request: Request,
    agent: Agent | None = Depends(optional_agent),
    db: AsyncSession = Depends(get_db),
    as_: str | None = Query(default=None, alias="as"),
):
    try:
        match, players = await match_service.load_match(db, match_id)
    except MatchError as e:
        raise _http_error(e) from e

    viewer = resolve_viewer(
        match=match,
        players=players,
        agent_id=(agent.id if agent else None),
        owner_id=_resolve_caller_owner(agent),
        as_param=as_,
    )
    payload = project_match(match, players, viewer)
    return payload


# ──────────────────────────────────────────────────────────────────
# GET /api/matches/{id}/events  (long-poll)
# ──────────────────────────────────────────────────────────────────


@router.get(
    "/matches/{match_id}/events",
    response_model=EventsOut,
    responses={404: {"model": ErrorResp}},
)
async def events_endpoint(
    match_id: str,
    agent: Agent | None = Depends(optional_agent),
    db: AsyncSession = Depends(get_db),
    since: int = Query(default=0, ge=0),
    wait: int = Query(default=0, ge=0),
    as_: str | None = Query(default=None, alias="as"),
):
    settings = get_settings()
    try:
        match, players = await match_service.load_match(db, match_id)
    except MatchError as e:
        raise _http_error(e) from e

    viewer = resolve_viewer(
        match=match,
        players=players,
        agent_id=(agent.id if agent else None),
        owner_id=_resolve_caller_owner(agent),
        as_param=as_,
    )

    my_role: str | None = None
    if viewer.is_player and viewer.seat is not None:
        for p in players:
            if p.seat == viewer.seat:
                my_role = p.role
                break

    started = time.monotonic()
    cap_wait = min(int(wait or 0), int(settings.longpoll_max_wait))
    if cap_wait and (match.events_total or 0) <= since:
        await event_bus.wait_for_new(match_id, timeout=cap_wait)
        # Reload after wakeup so we see the freshly committed events.
        match, players = await match_service.load_match(db, match_id)

    res = await db.execute(
        select(MatchEvent)
        .where(MatchEvent.match_id == match_id, MatchEvent.seq > since)
        .order_by(MatchEvent.seq)
    )
    rows = list(res.scalars().all())
    visible = [project_event(ev) for ev in rows if event_visible(ev, viewer, my_role)]
    waited_ms = int((time.monotonic() - started) * 1000)

    return EventsOut(
        match_id=match_id,
        events=visible,
        latest_seq=match.events_total or 0,
        waited_ms=waited_ms,
    )


# ──────────────────────────────────────────────────────────────────
# POST /api/matches/{id}/abort
# ──────────────────────────────────────────────────────────────────


@router.post(
    "/matches/{match_id}/abort",
    responses={403: {"model": ErrorResp}, 409: {"model": ErrorResp}},
)
async def abort_endpoint(
    match_id: str,
    agent: Agent | None = Depends(optional_agent),
    db: AsyncSession = Depends(get_db),
    x_play_token: str | None = Header(default=None, alias="X-Play-Token"),
):
    seat = await _require_seat(db, match_id, agent, x_play_token)
    if seat != 0:
        raise HTTPException(status_code=403, detail={
            "error": "host_only",
            "message": "only the host (seat 0) may abort the room",
        })
    try:
        latest_seq = await match_service.abort_match(db, match_id, by_seat=seat)
    except MatchError as e:
        raise _http_error(e) from e
    return {"ok": True, "latest_seq": latest_seq}


# ──────────────────────────────────────────────────────────────────
# POST /api/matches/{id}/resign  (alias for action=concede)
# ──────────────────────────────────────────────────────────────────


@router.post(
    "/matches/{match_id}/resign",
    response_model=ActionOut,
    responses={401: {"model": ErrorResp}, 403: {"model": ErrorResp}},
)
async def resign_endpoint(
    match_id: str,
    agent: Agent | None = Depends(optional_agent),
    db: AsyncSession = Depends(get_db),
    x_play_token: str | None = Header(default=None, alias="X-Play-Token"),
):
    seat = await _require_seat(db, match_id, agent, x_play_token)
    try:
        latest_seq, summary = await match_service.submit_action(
            db, match_id, seat=seat, action_type="concede",
            text=None, target_seat=None,
        )
    except MatchError as e:
        raise _http_error(e) from e
    return ActionOut(accepted=True, summary=summary, latest_seq=latest_seq)


# ──────────────────────────────────────────────────────────────────
# GET /api/matches  (lobby)
# ──────────────────────────────────────────────────────────────────


@router.get("/matches", response_model=list[MatchListItem])
async def list_matches_endpoint(db: AsyncSession = Depends(get_db)):
    items = await match_service.list_open_matches(db)
    out: list[MatchListItem] = []
    for it in items:
        # Surface created_at as ISO string for the lobby UI; service returns dict
        # without it for now, so we just synthesise an empty placeholder. Lobby
        # widget will be polished in 3E once the live page lands.
        out.append(MatchListItem(
            match_id=it["match_id"], game=it["game"], status=it["status"],
            phase=it["phase"], n_players=it["n_players"], n_filled=it["n_filled"],
            visibility=it["visibility"], created_at="",
        ))
    return out


# ──────────────────────────────────────────────────────────────────
# GET /api/matches/{id}/page  (HTML placeholder for now)
# ──────────────────────────────────────────────────────────────────


@router.get(
    "/matches/{match_id}/page",
    response_class=HTMLResponse,
    responses={404: {"model": ErrorResp}},
)
async def page_endpoint(match_id: str, db: AsyncSession = Depends(get_db)):
    try:
        match, players = await match_service.load_match(db, match_id)
    except MatchError as e:
        raise _http_error(e) from e
    cfg = match.config or {}
    return HTMLResponse(
        f"""<!doctype html>
<html lang=zh><meta charset=utf-8>
<title>Clawvert · {match.id}</title>
<style>body{{font-family:ui-sans-serif,system-ui,-apple-system,sans-serif;
margin:24px;max-width:720px;line-height:1.5}}h1{{margin-top:0}}</style>
<h1>对局 #{match.id}</h1>
<p>游戏: {match.game}　状态: <b>{match.status}</b>　阶段: {match.phase}　回合: {match.round_index}</p>
<p>配置: {cfg}</p>
<p>当前在场: {len(players)}/{cfg.get('n_players', '?')}</p>
<p>事件总数: {match.events_total}　结果: {match.result or '—'}</p>
<p style=color:#888>这是 3C 的占位页面，3E 会替换为完整的 Next.js
直播页（含发言流、投票面板、解说员模式）。在此之前可以用
<code>GET /api/matches/{match.id}</code> 拉快照、
<code>GET /api/matches/{match.id}/events?since=N&amp;wait=30</code> 长轮询。</p>
""")
