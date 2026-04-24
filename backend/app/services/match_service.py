"""
Persistence orchestrator for social-game-v1.

Bridges:
    DB rows  ←→  app.core.state.MatchState
    engine.Delta ──persist──▶ DB rows + event_bus.notify

All exceptions raised here are `MatchError` (with HTTP status_code) and the
API layer converts them to JSON error envelopes.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.engine import GameEngine
from app.core.state import Delta, MatchState, PlayerState
from app.core.wordpair import WordPair, get_wordpairs
from app.models.agent import Agent
from app.models.match import Match, MatchEvent, MatchPlayer
from app.services import event_bus

log = logging.getLogger("clawvert.match")

# ──────────────────────────────────────────────────────────────────
# Domain exceptions
# ──────────────────────────────────────────────────────────────────


class MatchError(Exception):
    status_code: int = 400

    def __init__(self, code: str, message: str,
                 status_code: int | None = None,
                 data: dict[str, Any] | None = None):
        self.code = code
        self.message = message
        self.data = data or {}
        if status_code is not None:
            self.status_code = status_code
        super().__init__(message)


class NotFound(MatchError):
    status_code = 404


class Conflict(MatchError):
    status_code = 409


class Unauthorized(MatchError):
    status_code = 401


class Forbidden(MatchError):
    status_code = 403


class Unprocessable(MatchError):
    status_code = 422


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────


def hash_token(tok: str) -> str:
    return hashlib.sha256(tok.encode("utf-8")).hexdigest()


def new_play_token() -> str:
    """24 hex chars = 96 random bits — plenty for per-match tokens."""
    return secrets.token_hex(12)


def _resolve_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    s = get_settings()
    cfg = dict(raw or {})
    cfg.setdefault("n_players", s.default_n_players)
    cfg.setdefault("n_undercover", s.default_n_undercover)
    cfg.setdefault("n_blank", s.default_n_blank)
    cfg.setdefault("speak_timeout", s.default_speak_timeout)
    cfg.setdefault("vote_timeout", s.default_vote_timeout)
    cfg.setdefault("tie_break", s.default_tie_break)
    cfg.setdefault("allow_whisper", False)
    cfg.setdefault("fellow_roles_visible", False)
    cfg.setdefault("max_rounds", cfg["n_players"])
    cfg.setdefault("visibility", "public")
    n, u, b = cfg["n_players"], cfg["n_undercover"], cfg["n_blank"]
    if not (1 <= u < n / 2):
        raise Unprocessable("invalid_config", "n_undercover must satisfy 1 ≤ U < N/2")
    if not (0 <= b <= n - u - 1):
        raise Unprocessable("invalid_config", "n_blank too large")
    return cfg


def _state_from_orm(match: Match, players: list[MatchPlayer]) -> MatchState:
    """Lift ORM rows to an in-memory MatchState the engine can consume."""
    state = MatchState(
        id=match.id,
        game=match.game,
        status=match.status,
        phase=match.phase,
        round_index=match.round_index,
        current_speaker_seat=match.current_speaker_seat,
        config=match.config or {},
        deadline_ts=match.deadline_ts,
        result=match.result,
        wordpair_id=match.wordpair_id,
        civilian_word=match.civilian_word,
        undercover_word=match.undercover_word,
        players=[
            PlayerState(
                seat=p.seat,
                name=p.name,
                display_name=p.display_name,
                role=p.role,
                word=p.word,
                alive=p.alive,
                eliminated_in_round=p.eliminated_in_round,
                eliminated_reason=p.eliminated_reason,
                revealed_role=p.revealed_role,
                has_spoken_this_round=p.has_spoken_this_round,
                has_voted_this_round=p.has_voted_this_round,
                last_vote_target_seat=p.last_vote_target_seat,
                agent_id=p.agent_id,
                owner_id=p.owner_id,
            )
            for p in sorted(players, key=lambda x: x.seat)
        ],
    )
    return state


# ──────────────────────────────────────────────────────────────────
# Loaders
# ──────────────────────────────────────────────────────────────────


async def load_match(db: AsyncSession, match_id: str) -> tuple[Match, list[MatchPlayer]]:
    match = await db.get(Match, match_id)
    if match is None:
        raise NotFound("match_not_found", f"match {match_id!r} does not exist")
    res = await db.execute(
        select(MatchPlayer).where(MatchPlayer.match_id == match_id)
        .order_by(MatchPlayer.seat)
    )
    players = list(res.scalars().all())
    return match, players


# ──────────────────────────────────────────────────────────────────
# Delta persistence (engine.Delta → DB + notify)
# ──────────────────────────────────────────────────────────────────


_MATCH_FIELDS = {
    "status", "phase", "round_index", "current_speaker_seat",
    "deadline_ts", "wordpair_id", "civilian_word", "undercover_word",
    "result", "config",
}

_PLAYER_FIELDS = {
    "role", "word", "alive", "eliminated_in_round", "eliminated_reason",
    "revealed_role", "has_spoken_this_round", "has_voted_this_round",
    "last_vote_target_seat",
}


async def persist_delta(
    db: AsyncSession, match: Match, players: list[MatchPlayer], delta: Delta
) -> int:
    """Apply Delta to ORM rows and append events. Returns latest_seq.

    Caller commits. event_bus.notify happens after commit (in API layer).
    """
    # 1) match column updates
    started = match.status != "in_progress" and \
        delta.match_updates.get("status") == "in_progress"
    finished_now = match.status not in ("finished", "aborted") and \
        delta.match_updates.get("status") in ("finished", "aborted")
    for k, v in delta.match_updates.items():
        if k in _MATCH_FIELDS:
            setattr(match, k, v)
    if started and match.started_at is None:
        match.started_at = datetime.utcnow()
    if finished_now and match.finished_at is None:
        match.finished_at = datetime.utcnow()

    # 2) per-player updates
    by_seat = {p.seat: p for p in players}
    for seat, fields in delta.player_updates.items():
        p = by_seat.get(seat)
        if p is None:
            continue
        for k, v in fields.items():
            if k in _PLAYER_FIELDS:
                setattr(p, k, v)

    # 3) events with monotonic seq
    base_seq = match.events_total or 0
    for i, ev in enumerate(delta.new_events, start=1):
        seq = base_seq + i
        db.add(MatchEvent(
            match_id=match.id,
            seq=seq,
            type=ev.type,
            visibility=ev.visibility,
            data=ev.data,
        ))
    match.events_total = base_seq + len(delta.new_events)
    return match.events_total


# ──────────────────────────────────────────────────────────────────
# Match creation
# ──────────────────────────────────────────────────────────────────


async def create_match(
    db: AsyncSession,
    *,
    config_in: dict[str, Any] | None,
    host_agent: Agent | None,
    host_name: str | None,
    host_display_name: str | None,
    host_meta: dict[str, Any] | None = None,
) -> tuple[Match, MatchPlayer, str]:
    cfg = _resolve_config(config_in)

    # owner_id dedup: when a logged-in agent creates a match, no other seat in
    # any concurrent match is allowed for the same owner. (Hard constraint per §7.1)
    if host_agent and host_agent.owner_id:
        await _assert_owner_not_busy(db, host_agent.owner_id)

    name = (host_agent.name if host_agent else host_name) or "anon"
    display = (host_agent.display_name if host_agent else host_display_name) or name

    match = Match(
        config=cfg,
        status="waiting",
        phase="waiting",
        round_index=0,
        events_total=0,
    )
    db.add(match)
    await db.flush()  # populate match.id

    play_token = new_play_token()
    host = MatchPlayer(
        match_id=match.id,
        seat=0,
        agent_id=host_agent.id if host_agent else None,
        owner_id=host_agent.owner_id if host_agent else None,
        name=name,
        display_name=display,
        play_token_hash=hash_token(play_token),
        meta=host_meta or {},
        joined_at=datetime.utcnow(),
        last_seen_at=datetime.utcnow(),
    )
    db.add(host)

    # match_created event (public).
    db.add(MatchEvent(
        match_id=match.id, seq=1,
        type="match_created", visibility="public",
        data={
            "n_players": cfg["n_players"], "n_undercover": cfg["n_undercover"],
            "host_seat": 0, "host_name": name,
        },
    ))
    db.add(MatchEvent(
        match_id=match.id, seq=2,
        type="player_joined", visibility="public",
        data={"seat": 0, "name": name, "display_name": display,
              "is_official_bot": bool(host_agent and host_agent.is_official_bot)},
    ))
    match.events_total = 2

    await db.commit()
    event_bus.notify(match.id)
    return match, host, play_token


# ──────────────────────────────────────────────────────────────────
# Join + auto-begin when full
# ──────────────────────────────────────────────────────────────────


async def join_match(
    db: AsyncSession,
    match_id: str,
    *,
    agent: Agent | None,
    guest_name: str | None,
    guest_display_name: str | None,
    meta: dict[str, Any] | None = None,
) -> tuple[Match, MatchPlayer, str]:
    match, players = await load_match(db, match_id)
    if match.status != "waiting":
        raise Conflict("match_not_waiting",
                       f"cannot join, status is {match.status}")

    cfg = match.config or {}
    n_players = int(cfg.get("n_players", 6))
    if len(players) >= n_players:
        raise Conflict("seats_full", "match is already full")

    name = (agent.name if agent else guest_name) or f"anon-{len(players)}"
    display = (agent.display_name if agent else guest_display_name) or name

    # Anti-cheat (§7.1): same owner cannot occupy ≥ 2 seats in the same match.
    if agent and agent.owner_id:
        for p in players:
            if p.owner_id == agent.owner_id:
                raise Conflict(
                    "same_owner_already_in_match",
                    "your owner already occupies a seat in this match",
                    data={"existing_seat": p.seat},
                )
        await _assert_owner_not_busy(db, agent.owner_id, exclude_match_id=match_id)

    # Reject duplicate name in the same match.
    for p in players:
        if p.name == name:
            raise Conflict("duplicate_name",
                           f"name {name!r} already taken in this match",
                           data={"existing_seat": p.seat})

    # Allocate the lowest free seat (gives a stable seat order).
    used = {p.seat for p in players}
    seat = next(s for s in range(n_players) if s not in used)

    play_token = new_play_token()
    new_player = MatchPlayer(
        match_id=match.id,
        seat=seat,
        agent_id=agent.id if agent else None,
        owner_id=agent.owner_id if agent else None,
        name=name,
        display_name=display,
        play_token_hash=hash_token(play_token),
        meta=meta or {},
        joined_at=datetime.utcnow(),
        last_seen_at=datetime.utcnow(),
    )
    db.add(new_player)
    players.append(new_player)

    base_seq = match.events_total or 0
    db.add(MatchEvent(
        match_id=match.id, seq=base_seq + 1,
        type="player_joined", visibility="public",
        data={"seat": seat, "name": name, "display_name": display,
              "is_official_bot": bool(agent and agent.is_official_bot)},
    ))
    match.events_total = base_seq + 1

    # Auto-begin when full.
    if len(players) == n_players:
        wordpair = _select_wordpair(cfg)
        snapshot = _state_from_orm(match, players)
        delta = GameEngine.begin_match(snapshot, wordpair)
        if not delta.accepted:
            await db.rollback()
            raise Unprocessable(delta.error.code, delta.error.message)
        await persist_delta(db, match, players, delta)

    await db.commit()
    event_bus.notify(match.id)
    return match, new_player, play_token


def _select_wordpair(cfg: dict[str, Any]) -> WordPair:
    lib = get_wordpairs()
    pid = cfg.get("wordpair_id")
    if pid:
        pair = lib.get(pid)
        if pair is None:
            raise Unprocessable("invalid_config", f"wordpair_id {pid!r} not found")
        return pair
    return lib.random_pair(cfg.get("wordpair_tag"))


# ──────────────────────────────────────────────────────────────────
# Action dispatch
# ──────────────────────────────────────────────────────────────────


async def submit_action(
    db: AsyncSession,
    match_id: str,
    *,
    seat: int,
    action_type: str,
    text: str | None,
    target_seat: int | None,
) -> tuple[int, dict[str, Any]]:
    match, players = await load_match(db, match_id)
    if match.status != "in_progress":
        raise Conflict("match_not_in_progress",
                       f"current status is {match.status}")
    state = _state_from_orm(match, players)

    if action_type == "speak":
        delta = GameEngine.apply_speak(state, seat=seat, text=text or "")
    elif action_type == "skip":
        delta = GameEngine.apply_skip(state, seat=seat)
    elif action_type == "vote":
        if target_seat is None:
            raise Unprocessable("invalid_target", "vote requires target_seat")
        delta = GameEngine.apply_vote(state, seat=seat, target_seat=target_seat)
    elif action_type == "concede":
        delta = GameEngine.apply_concede(state, seat=seat)
    elif action_type == "whisper":
        if target_seat is None:
            raise Unprocessable("invalid_target", "whisper requires target_seat")
        delta = GameEngine.apply_whisper(state, from_seat=seat,
                                         to_seat=target_seat, text=text or "")
    else:
        raise Unprocessable("unknown_action", f"unsupported action: {action_type!r}")

    if not delta.accepted:
        # Map engine error codes to HTTP status.
        code = delta.error.code if delta.error else "rejected"
        msg = delta.error.message if delta.error else "rejected"
        status = _engine_error_to_http(code)
        raise MatchError(code, msg, status_code=status)

    latest_seq = await persist_delta(db, match, players, delta)
    if match.status in ("finished", "aborted"):
        await _bump_stats_on_terminal(db, match, players)
    await db.commit()
    event_bus.notify(match.id)
    return latest_seq, delta.summary


async def _bump_stats_on_terminal(
    db: AsyncSession, match: Match, players: list[MatchPlayer]
) -> None:
    """Apply wins/losses/aborts to Agent rows once a match enters a terminal."""
    from app.services import agent_service
    if match.status == "aborted":
        all_aids = [p.agent_id for p in players if p.agent_id]
        await agent_service.record_match_result(
            db, winning_agent_ids=all_aids, losing_agent_ids=[], aborted=True,
        )
        return
    if match.status != "finished":
        return
    result = match.result or {}
    winning_seats = set(result.get("winning_seats") or [])
    losing_seats = set(result.get("losing_seats") or [])
    win_aids = [p.agent_id for p in players
                if p.seat in winning_seats and p.agent_id]
    lose_aids = [p.agent_id for p in players
                 if p.seat in losing_seats and p.agent_id]
    await agent_service.record_match_result(
        db, winning_agent_ids=win_aids, losing_agent_ids=lose_aids,
    )


def _engine_error_to_http(code: str) -> int:
    if code in ("not_your_turn_to_speak", "you_are_eliminated", "already_voted",
                "wrong_phase", "match_not_in_progress",
                "whisper_not_enabled", "whisper_target_not_fellow"):
        return 403
    if code in ("speech_contains_secret_word", "invalid_target",
                "invalid_config", "not_full"):
        return 422
    if code == "match_not_waiting":
        return 409
    return 400


# ──────────────────────────────────────────────────────────────────
# Abort / Resign
# ──────────────────────────────────────────────────────────────────


async def abort_match(
    db: AsyncSession, match_id: str, *, reason: str = "host_cancelled"
) -> int:
    match, players = await load_match(db, match_id)
    state = _state_from_orm(match, players)
    delta = GameEngine.apply_abort(state, reason=reason)
    if not delta.accepted:
        raise MatchError(delta.error.code, delta.error.message,
                         status_code=_engine_error_to_http(delta.error.code))
    latest_seq = await persist_delta(db, match, players, delta)
    await _bump_stats_on_terminal(db, match, players)
    await db.commit()
    event_bus.notify(match.id)
    return latest_seq


async def force_timeout(db: AsyncSession, match_id: str) -> int:
    """Engine timeout entry-point used by janitor when deadline_ts elapses."""
    match, players = await load_match(db, match_id)
    state = _state_from_orm(match, players)
    delta = GameEngine.apply_timeout(state)
    if not delta.accepted:
        # Ignore "nothing to timeout" — caller won't bubble it up.
        raise MatchError(delta.error.code, delta.error.message,
                         status_code=_engine_error_to_http(delta.error.code))
    latest_seq = await persist_delta(db, match, players, delta)
    if match.status in ("finished", "aborted"):
        await _bump_stats_on_terminal(db, match, players)
    await db.commit()
    event_bus.notify(match.id)
    return latest_seq


async def _assert_owner_not_busy(
    db: AsyncSession, owner_id: str, exclude_match_id: str | None = None,
) -> None:
    """Owner is 'busy' if they hold a seat in any waiting/in_progress match."""
    q = (
        select(MatchPlayer.match_id, MatchPlayer.seat)
        .join(Match, Match.id == MatchPlayer.match_id)
        .where(MatchPlayer.owner_id == owner_id,
               Match.status.in_(("waiting", "in_progress")))
    )
    if exclude_match_id:
        q = q.where(MatchPlayer.match_id != exclude_match_id)
    res = await db.execute(q)
    row = res.first()
    if row is not None:
        raise Conflict(
            "same_owner_already_in_match",
            "your owner is already occupying a seat in another open match",
            data={"existing_match_id": row[0], "existing_seat": row[1]},
        )


# ──────────────────────────────────────────────────────────────────
# Lobby list
# ──────────────────────────────────────────────────────────────────


async def list_open_matches(db: AsyncSession, *, limit: int = 50) -> list[dict[str, Any]]:
    res = await db.execute(
        select(Match,
               func.count(MatchPlayer.id).label("filled"))
        .outerjoin(MatchPlayer, MatchPlayer.match_id == Match.id)
        .where(Match.status.in_(("waiting", "in_progress")))
        .group_by(Match.id)
        .order_by(Match.created_at.desc())
        .limit(limit)
    )
    items = []
    for m, filled in res.all():
        cfg = m.config or {}
        items.append({
            "match_id": m.id,
            "game": m.game,
            "status": m.status,
            "phase": m.phase,
            "n_players": int(cfg.get("n_players", 0)),
            "n_filled": int(filled or 0),
            "visibility": cfg.get("visibility", "public"),
        })
    return items


# Mark a seat as recently seen (heartbeat for janitor / lobby light).
async def touch_seat(db: AsyncSession, match_id: str, seat: int) -> None:
    await db.execute(
        update(MatchPlayer)
        .where(MatchPlayer.match_id == match_id, MatchPlayer.seat == seat)
        .values(last_seen_at=datetime.utcnow())
    )
    await db.commit()
