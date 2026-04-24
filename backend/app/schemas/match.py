from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ── Config (request side) ──────────────────────────────────────────


class MatchConfigIn(BaseModel):
    """All fields optional; backend fills defaults from settings."""

    n_players: int | None = Field(default=None, ge=3, le=12)
    n_undercover: int | None = Field(default=None, ge=1)
    n_blank: int | None = Field(default=None, ge=0)
    speak_timeout: int | None = Field(default=None, ge=10, le=600)
    vote_timeout: int | None = Field(default=None, ge=10, le=600)
    max_rounds: int | None = Field(default=None, ge=1, le=20)
    tie_break: Literal["random", "revote", "noop_then_random"] | None = None
    allow_whisper: bool | None = None
    fellow_roles_visible: bool | None = None
    wordpair_id: str | None = None
    wordpair_tag: str | None = None
    visibility: Literal["public", "unlisted", "private"] | None = None


# ── Common identity envelope (3C dev stub; 3D upgrades to real auth) ──


class PlayerIn(BaseModel):
    """Optional guest identity for callers without an api_key."""

    name: str | None = None
    display_name: str | None = None


# ── POST /api/matches ──────────────────────────────────────────────


class CreateMatchIn(BaseModel):
    config: MatchConfigIn = Field(default_factory=MatchConfigIn)
    player: PlayerIn | None = None


class CreateMatchOut(BaseModel):
    match_id: str
    status: str
    your_seat: int
    play_token: str
    join_url: str
    page_url: str
    config: dict[str, Any]


# ── POST /api/matches/{id}/join ────────────────────────────────────


class JoinMatchIn(BaseModel):
    player: PlayerIn | None = None


class JoinMatchOut(BaseModel):
    match_id: str
    status: str
    your_seat: int
    play_token: str
    config: dict[str, Any]


# ── POST /api/matches/{id}/action ──────────────────────────────────


class ActionIn(BaseModel):
    type: Literal["speak", "vote", "skip", "concede", "whisper"]
    text: str | None = None
    target_seat: int | None = None
    play_token: str | None = None


class ActionOut(BaseModel):
    accepted: bool
    summary: dict[str, Any] = Field(default_factory=dict)
    latest_seq: int


# ── GET /api/matches/{id} (viewer-scoped snapshot) ─────────────────


class PlayerView(BaseModel):
    """Per-seat info shown in the snapshot. Sensitive fields are stripped
    based on viewer role inside `services.views`."""

    seat: int
    name: str
    display_name: str | None = None
    agent_id: str | None = None
    is_official_bot: bool = False
    alive: bool
    eliminated_in_round: int | None = None
    eliminated_reason: str | None = None
    revealed_role: str | None = None
    role: str | None = None  # only for self / god view
    word: str | None = None  # only for self / god view
    has_voted_this_round: bool | None = None
    has_spoken_this_round: bool | None = None
    last_vote_target_seat: int | None = None


class SnapshotOut(BaseModel):
    match_id: str
    game: str
    status: str
    phase: str
    round_index: int
    current_speaker_seat: int | None
    deadline_ts: int | None
    config: dict[str, Any]
    players: list[PlayerView]
    result: dict[str, Any] | None
    events_total: int
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None

    # ── viewer metadata (per §2.4) ──
    viewer_role: Literal["anonymous", "spectator", "player_owner",
                          "player_agent", "replay"] = "anonymous"
    viewer_capabilities: dict[str, Any] = Field(default_factory=dict)
    your_seat: int | None = None
    your_role: str | None = None
    your_word: str | None = None
    your_turn: bool | None = None


# ── GET /api/matches/{id}/events (long-poll) ───────────────────────


class EventOut(BaseModel):
    seq: int
    type: str
    visibility: str
    data: dict[str, Any]
    ts: str


class EventsOut(BaseModel):
    match_id: str
    events: list[EventOut]
    latest_seq: int
    waited_ms: int = 0


# ── Lobby list ─────────────────────────────────────────────────────


class MatchListItem(BaseModel):
    match_id: str
    game: str
    status: str
    phase: str
    n_players: int
    n_filled: int
    visibility: str
    created_at: str
