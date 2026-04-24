"""Shared enums / constants / type aliases for the social-game-v1 engine."""

from __future__ import annotations

from typing import Literal, TypeAlias

# ── Match status (top-level lifecycle) ────────────────────────────
MatchStatus: TypeAlias = Literal["waiting", "in_progress", "finished", "aborted"]

# ── Phase (intra in_progress sub-state) ───────────────────────────
# We don't enumerate every "speak_round_N" — phase strings are produced
# dynamically as f"speak_round_{n}" / f"vote_round_{n}" / f"reveal_round_{n}".
PhaseKind: TypeAlias = Literal["waiting", "dealing", "speak", "vote", "reveal", "finished"]

# ── Roles ─────────────────────────────────────────────────────────
Role: TypeAlias = Literal["civilian", "undercover", "blank"]
Camp: TypeAlias = Literal["civilian", "undercover", "blank"]

ROLE_CIVILIAN: Role = "civilian"
ROLE_UNDERCOVER: Role = "undercover"
ROLE_BLANK: Role = "blank"

# ── Action types ──────────────────────────────────────────────────
ActionType: TypeAlias = Literal["speak", "vote", "skip", "concede", "whisper"]

# ── Event visibility ──────────────────────────────────────────────
# private:seat:N / private:role:<role> / private:host / public / god_only
Visibility: TypeAlias = str

VIS_PUBLIC = "public"
VIS_HOST = "private:host"
VIS_GOD = "god_only"


def vis_seat(seat: int) -> str:
    return f"private:seat:{seat}"


def vis_role(role: Role) -> str:
    return f"private:role:{role}"


# ── Tie-break strategies (vote_resolver) ──────────────────────────
TieBreak: TypeAlias = Literal["random", "revote", "noop_then_random"]

# ── Termination reasons ───────────────────────────────────────────
TerminationReason: TypeAlias = Literal[
    "all_undercovers_eliminated",
    "all_undercovers_conceded_or_eliminated",
    "undercover_majority",
    "max_rounds_reached",
    "host_cancelled",
    "janitor_timeout",
]

# ── Phase helpers ─────────────────────────────────────────────────


def phase_str(kind: PhaseKind, round_index: int = 0) -> str:
    """Produce the `phase` string stored on Match.

    `dealing` / `waiting` / `finished` are static; speak/vote/reveal carry round.
    """
    if kind in ("waiting", "dealing", "finished"):
        return kind
    return f"{kind}_round_{round_index}"


def parse_phase(phase: str) -> tuple[PhaseKind, int]:
    """Inverse of phase_str.

    Returns ("dealing", 0) for static phases."""
    if phase in ("waiting", "dealing", "finished"):
        return phase, 0  # type: ignore[return-value]
    if "_round_" in phase:
        kind, _, idx = phase.partition("_round_")
        return kind, int(idx)  # type: ignore[return-value]
    raise ValueError(f"unrecognised phase string: {phase}")
