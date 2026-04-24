"""
Viewer-scoped projection of Match/Player/Event rows.

Implements §2.4 / §2.5 of social-game-v1: the bytes returned over the wire
depend on who is asking. Four viewer roles + replay (auto-upgrade post-finish):

    anonymous       — public-only, no seats highlighted
    spectator       — public + spectator-grade tactical aids
                       (e.g. round summaries, vote totals)
    player_owner    — IDENTICAL to the seat's player_agent view, no extras
    player_agent    — sees own role/word + own private events
    god_view (replay) — sees everything; auto-applied when status ∈ finished/aborted
"""

from __future__ import annotations

from typing import Any

from app.models.match import Match, MatchEvent, MatchPlayer
from app.schemas.common import iso_utc

# ──────────────────────────────────────────────────────────────────
# Viewer descriptor
# ──────────────────────────────────────────────────────────────────


class Viewer:
    """Resolved viewer context for a single request."""

    def __init__(
        self,
        role: str,                       # anonymous | spectator | player_owner | player_agent | replay
        seat: int | None = None,         # owned seat (for player_*)
        owner_id: str | None = None,     # logged-in owner, if any
        agent_id: str | None = None,     # authenticated agent, if any
    ):
        self.role = role
        self.seat = seat
        self.owner_id = owner_id
        self.agent_id = agent_id

    @property
    def is_player(self) -> bool:
        return self.role in ("player_owner", "player_agent")

    @property
    def is_god(self) -> bool:
        # Includes "replay" auto-upgrade in finished matches.
        return self.role in ("replay", "god")

    def capabilities(self) -> dict[str, Any]:
        return {
            "can_see_my_role": self.is_player or self.is_god,
            "can_see_my_word": self.is_player or self.is_god,
            "can_see_others_roles": self.is_god,
            "can_see_god_events": self.is_god,
            "can_act": self.role == "player_agent",
            "can_chat_in_lobby": True,
        }


def resolve_viewer(
    *,
    match: Match,
    players: list[MatchPlayer],
    agent_id: str | None,
    owner_id: str | None,
    as_param: str | None,
) -> Viewer:
    """Pick a viewer role from auth context + caller intent.

    Rules (in priority order):
      1. Match finished/aborted → "replay" (god view) regardless.
      2. Authenticated agent occupying a seat → player_agent (their seat).
      3. Authenticated owner (no agent at the table) viewing one of their
         own seats → player_owner (read-only god of their own seat).
      4. ?as=spectator → spectator.
      5. fallthrough → anonymous.
    """
    if match.status in ("finished", "aborted"):
        return Viewer("replay")

    if agent_id:
        for p in players:
            if p.agent_id == agent_id:
                return Viewer("player_agent", seat=p.seat,
                              agent_id=agent_id, owner_id=owner_id)
    if owner_id:
        for p in players:
            if p.owner_id == owner_id:
                return Viewer("player_owner", seat=p.seat,
                              owner_id=owner_id, agent_id=agent_id)
    if (as_param or "").lower() == "spectator":
        return Viewer("spectator")
    return Viewer("anonymous")


# ──────────────────────────────────────────────────────────────────
# Snapshot projection
# ──────────────────────────────────────────────────────────────────


def project_player(p: MatchPlayer, viewer: Viewer, terminal: bool) -> dict[str, Any]:
    base = {
        "seat": p.seat,
        "name": p.name,
        "display_name": p.display_name,
        "agent_id": p.agent_id,
        "is_official_bot": False,  # filled by API layer once it has the Agent row
        "alive": p.alive,
        "eliminated_in_round": p.eliminated_in_round,
        "eliminated_reason": p.eliminated_reason,
        "revealed_role": p.revealed_role,
    }

    show_self_secrets = (
        terminal or viewer.is_god
        or (viewer.is_player and viewer.seat == p.seat)
    )
    show_all_secrets = terminal or viewer.is_god
    show_round_flags = (
        viewer.is_god
        or viewer.role == "spectator"
        or (viewer.is_player and viewer.seat == p.seat)
    )

    if show_all_secrets:
        base["role"] = p.role
        base["word"] = p.word
    elif show_self_secrets and viewer.is_player and viewer.seat == p.seat:
        base["role"] = p.role
        base["word"] = p.word

    if show_round_flags:
        base["has_voted_this_round"] = p.has_voted_this_round
        base["has_spoken_this_round"] = p.has_spoken_this_round
        # last_vote_target_seat is OK to leak on spectator/god (D4 makes vote_cast public)
        base["last_vote_target_seat"] = p.last_vote_target_seat

    return base


def project_match(
    match: Match,
    players: list[MatchPlayer],
    viewer: Viewer,
) -> dict[str, Any]:
    terminal = match.status in ("finished", "aborted")
    cfg = match.config or {}

    seat_views = [project_player(p, viewer, terminal) for p in players]

    your_seat = viewer.seat if viewer.is_player else None
    your = next((p for p in players if p.seat == your_seat), None) if your_seat is not None else None
    your_turn = (
        match.status == "in_progress"
        and viewer.role == "player_agent"
        and match.current_speaker_seat == your_seat
        and (match.phase or "").startswith("speak_round_")
    )

    # On replay, expose the chosen wordpair for educational value.
    out: dict[str, Any] = {
        "match_id": match.id,
        "game": match.game,
        "status": match.status,
        "phase": match.phase,
        "round_index": match.round_index,
        "current_speaker_seat": (
            match.current_speaker_seat if not terminal else None
        ),
        "deadline_ts": (
            match.deadline_ts if match.status == "in_progress" else None
        ),
        "config": cfg,
        "players": seat_views,
        "result": match.result,
        "events_total": match.events_total or 0,
        "created_at": iso_utc(match.created_at) or "",
        "started_at": iso_utc(match.started_at),
        "finished_at": iso_utc(match.finished_at),
        "viewer_role": viewer.role,
        "viewer_capabilities": viewer.capabilities(),
        "your_seat": your_seat,
        "your_role": your.role if (your and viewer.role == "player_agent") else None,
        "your_word": your.word if (your and viewer.role == "player_agent") else None,
        "your_turn": your_turn if your_seat is not None else None,
    }
    # The chosen wordpair is already exposed via `result.wordpair` once the
    # match terminates, so we don't duplicate it on the snapshot envelope.
    return out


# ──────────────────────────────────────────────────────────────────
# Event filtering
# ──────────────────────────────────────────────────────────────────


def event_visible(ev: MatchEvent, viewer: Viewer, my_role: str | None) -> bool:
    """Apply §2.5.2 visibility filter to a single MatchEvent."""
    vis = ev.visibility or "public"
    if viewer.is_god:
        return True
    if vis == "public":
        return True
    if vis == "god_only":
        return False
    if vis == "private:host":
        # 3D will let host claim a different role; for 3C the host = seat 0.
        return viewer.is_player and viewer.seat == 0
    if vis.startswith("private:seat:"):
        try:
            target = int(vis.split(":", 2)[2])
        except (ValueError, IndexError):
            return False
        return viewer.is_player and viewer.seat == target
    if vis.startswith("private:role:"):
        target_role = vis.split(":", 2)[2]
        return (
            viewer.role == "player_agent"
            and my_role == target_role
        )
    return False


def project_event(ev: MatchEvent) -> dict[str, Any]:
    return {
        "seq": ev.seq,
        "type": ev.type,
        "visibility": ev.visibility,
        "data": ev.data or {},
        "ts": iso_utc(ev.ts) or "",
    }
