"""In-memory snapshots used by the engine. ORM-free so unit tests don't need DB.

Conversion helpers between ORM rows and these snapshots live in
`app.services.match_service` (added in 3C).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PlayerState:
    seat: int
    name: str
    display_name: str | None = None

    role: str | None = None  # civilian | undercover | blank, None until dealt
    word: str | None = None

    alive: bool = True
    eliminated_in_round: int | None = None
    eliminated_reason: str | None = None  # vote | concede | timeout | tie_random
    revealed_role: str | None = None

    has_spoken_this_round: bool = False
    has_voted_this_round: bool = False
    last_vote_target_seat: int | None = None

    agent_id: str | None = None
    owner_id: str | None = None


@dataclass
class MatchState:
    id: str
    game: str
    status: str  # waiting | in_progress | finished | aborted
    phase: str  # phase string per types.phase_str()
    round_index: int  # current round (1-based, 0 before deal)
    current_speaker_seat: int | None
    config: dict
    players: list[PlayerState] = field(default_factory=list)
    deadline_ts: int | None = None
    result: dict | None = None
    wordpair_id: str | None = None
    civilian_word: str | None = None
    undercover_word: str | None = None

    # ── derived helpers ──────────────────────────────────────────
    def by_seat(self, seat: int) -> PlayerState | None:
        for p in self.players:
            if p.seat == seat:
                return p
        return None

    def alive(self) -> list[PlayerState]:
        return [p for p in self.players if p.alive]

    def alive_role(self, role: str) -> list[PlayerState]:
        return [p for p in self.alive() if p.role == role]


# ── Engine output deltas ────────────────────────────────────────


@dataclass
class NewEvent:
    """Events the engine wants the persistence layer to write + notify."""

    type: str
    visibility: str
    data: dict


@dataclass
class EngineError:
    code: str
    message: str


@dataclass
class Delta:
    """Pure result of feeding an action / lifecycle event into the engine.

    The persistence layer (3C) takes this delta and applies it to the DB
    in a single transaction, then `event_bus.notify(match_id)`.
    """

    match_updates: dict[str, Any] = field(default_factory=dict)
    player_updates: dict[int, dict[str, Any]] = field(default_factory=dict)
    new_events: list[NewEvent] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    accepted: bool = True
    error: EngineError | None = None

    # ── builders for tests/conciseness ───────────────────────────
    @classmethod
    def reject(cls, code: str, message: str) -> Delta:
        return cls(accepted=False, error=EngineError(code, message))

    def add_event(self, type_: str, visibility: str, **data: Any) -> None:
        self.new_events.append(NewEvent(type=type_, visibility=visibility, data=data))

    def update_player(self, seat: int, **fields: Any) -> None:
        self.player_updates.setdefault(seat, {}).update(fields)
