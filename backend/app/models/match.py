from __future__ import annotations

import secrets
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def _short_id() -> str:
    """8-char hex id for matches (URL-friendly, ~4B space)."""
    return secrets.token_hex(4)


def _ulid() -> str:
    return secrets.token_hex(8)


class Match(Base):
    __tablename__ = "matches"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=_short_id)
    game: Mapped[str] = mapped_column(String(32), default="undercover")

    # Top-level status (waiting | in_progress | finished | aborted)
    status: Mapped[str] = mapped_column(String(16), default="waiting", index=True)

    # Phase string per §4: waiting | dealing | speak_round_N | vote_round_N
    #                    | reveal_round_N | finished
    phase: Mapped[str] = mapped_column(String(32), default="waiting")
    round_index: Mapped[int] = mapped_column(Integer, default=0)
    current_speaker_seat: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deadline_ts: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Full config (n_players, n_undercover, timeouts, tie_break, etc.)
    config: Mapped[dict] = mapped_column(JSON, default=dict)

    # Wordpair selected at deal time (kept inline so UI can reveal at terminal)
    wordpair_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    civilian_word: Mapped[str | None] = mapped_column(String(64), nullable=True)
    undercover_word: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Result blob (winner_camp, winning_seats, losing_seats, reason, summary…)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Monotonic seq counter for events; bumped at every notify().
    events_total: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MatchPlayer(Base):
    __tablename__ = "match_players"
    __table_args__ = (
        UniqueConstraint("match_id", "seat", name="uq_match_seat"),
        UniqueConstraint("match_id", "name", name="uq_match_name"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_ulid)
    match_id: Mapped[str] = mapped_column(
        String(16), ForeignKey("matches.id"), index=True
    )
    seat: Mapped[int] = mapped_column(Integer)

    # Identity (agent + owner is what matters for anti-cheat dedup)
    agent_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    owner_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Role + word are populated at deal time (n_players satisfied).
    role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    word: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Lifecycle
    alive: Mapped[bool] = mapped_column(Boolean, default=True)
    eliminated_in_round: Mapped[int | None] = mapped_column(Integer, nullable=True)
    eliminated_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    revealed_role: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Auth (per-match scoped token, sha256 hashed)
    play_token_hash: Mapped[str] = mapped_column(String(64), index=True)

    # Round-local flags (reset by engine at phase transition)
    has_voted_this_round: Mapped[bool] = mapped_column(Boolean, default=False)
    has_spoken_this_round: Mapped[bool] = mapped_column(Boolean, default=False)
    last_vote_target_seat: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Heartbeat for janitor / lobby attendance light
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Provider transparency (X-Provider-Id, X-Provider-Agent-Meta) — never echoed publicly
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class MatchEvent(Base):
    __tablename__ = "match_events"
    __table_args__ = (
        UniqueConstraint("match_id", "seq", name="uq_match_event_seq"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(
        String(16), ForeignKey("matches.id"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer, index=True)
    type: Mapped[str] = mapped_column(String(48))

    # Visibility per §2.5.2 (public | private:seat:N | private:role:* |
    #                       private:host | god_only)
    visibility: Mapped[str] = mapped_column(String(48), default="public")

    # Payload (any JSON; round, seat, text, target_seat, eliminated_seat,
    # vote_breakdown, deadline_ts, etc.)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
