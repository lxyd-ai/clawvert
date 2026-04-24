"""Unit tests for scripts/officials/personas.py.

These cover ONLY the pure helpers (vote strategies + speech-pool sanity);
the runner state machine is exercised by the live smoke run documented in
scripts/officials/README.md (it needs a real backend + lobby).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make `scripts.officials.personas` importable from backend tests
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.officials.personas import (  # noqa: E402
    CAUTIOUS,
    CHATTY,
    CONTRARIAN,
    PERSONAS,
    get_persona,
    resolve_vote,
)

# ──────────────────────────────────────────────────────────────────
# Speech-pool sanity
# ──────────────────────────────────────────────────────────────────


def test_three_personas_registered_with_official_prefix():
    assert set(PERSONAS.keys()) == {
        "official-cautious-cat",
        "official-chatty-fox",
        "official-contrarian-owl",
    }
    for p in PERSONAS.values():
        assert p.name.startswith("official-")


def test_pools_are_non_empty_and_string_valued():
    for p in PERSONAS.values():
        assert len(p.opener_pool) >= 3
        assert len(p.speech_pool) >= 5
        for s in (*p.opener_pool, *p.speech_pool):
            assert isinstance(s, str) and s.strip()
            # Sanity: no template ever explicitly contains a wordpair label —
            # this is our last-line defence against accidental leaks.
            for forbidden in ("飞机", "高铁", "牙刷", "牙膏"):
                assert forbidden not in s, (
                    f"persona={p.name} template contained forbidden word "
                    f"{forbidden!r}: {s!r}"
                )


def test_get_persona_lookup_and_unknown():
    assert get_persona("official-cautious-cat") is CAUTIOUS
    with pytest.raises(KeyError):
        get_persona("official-nonexistent")


# ──────────────────────────────────────────────────────────────────
# Vote strategies
# ──────────────────────────────────────────────────────────────────


def _state(alive_seats: list[int], speeches: list[dict] | None = None) -> dict:
    return {
        "players": [
            {"seat": s, "alive": True} for s in alive_seats
        ] + [
            # Add some dead seats too to make sure they're filtered out.
            {"seat": 99, "alive": False},
        ],
        "speeches": speeches or [],
    }


def test_follow_majority_picks_leader():
    state = _state(alive_seats=[0, 1, 2, 3])
    votes = [
        {"seat": 0, "target_seat": 2},
        {"seat": 1, "target_seat": 2},
        {"seat": 3, "target_seat": 1},
    ]
    assert resolve_vote(CAUTIOUS, state, my_seat=4, recent_votes=votes) == 2


def test_follow_majority_breaks_tie_by_lowest_seat():
    state = _state(alive_seats=[1, 2, 3, 5])
    votes = [
        {"seat": 1, "target_seat": 3},
        {"seat": 2, "target_seat": 5},
    ]
    # 3 and 5 each have 1 vote; the deterministic tiebreak picks the lower seat.
    assert resolve_vote(CAUTIOUS, state, my_seat=10, recent_votes=votes) == 3


def test_follow_majority_falls_back_to_least_descriptive_when_no_votes():
    state = _state(
        alive_seats=[0, 1, 2, 3],
        speeches=[
            {"seat": 0, "round": 1, "text": "我说了好长一段话aaaaaaaaaaaaaaaaaaaaa"},
            {"seat": 1, "round": 1, "text": "我也说了一些"},
            {"seat": 2, "round": 1, "text": ""},
            # seat 3 never spoke
        ],
    )
    target = resolve_vote(CAUTIOUS, state, my_seat=99, recent_votes=[])
    # With 0 votes, fallback chain is least_descriptive → seat 2 OR seat 3 tie 0;
    # tiebreak by seat asc → seat 2.
    assert target == 2


def test_chatty_targets_silent_seat():
    state = _state(
        alive_seats=[0, 1, 2, 3],
        speeches=[
            {"seat": 0, "round": 1, "text": "blah blah blah"},
            {"seat": 1, "round": 1, "text": "yapping yapping"},
            {"seat": 2, "round": 1, "text": "okay"},
            # seat 3 silent → most suspicious to chatty
        ],
    )
    assert resolve_vote(CHATTY, state, my_seat=99, recent_votes=[]) == 3


def test_contrarian_targets_least_voted_alive():
    state = _state(alive_seats=[0, 1, 2, 3])
    votes = [
        {"seat": 0, "target_seat": 1},
        {"seat": 2, "target_seat": 1},
        {"seat": 3, "target_seat": 2},
    ]
    # seat 0 has 0 incoming votes (excluding self) → contrarian goes there.
    assert resolve_vote(CONTRARIAN, state, my_seat=99, recent_votes=votes) == 0


def test_resolve_vote_never_returns_self_or_dead():
    state = _state(alive_seats=[0, 1])
    target = resolve_vote(CAUTIOUS, state, my_seat=0, recent_votes=[])
    assert target == 1
    target = resolve_vote(CAUTIOUS, state, my_seat=1, recent_votes=[])
    assert target == 0
    # Dead seat 99 must never be picked.
    state2 = _state(alive_seats=[0, 1, 2])
    for _ in range(20):
        for persona in (CAUTIOUS, CHATTY, CONTRARIAN):
            t = resolve_vote(persona, state2, my_seat=0, recent_votes=[])
            assert t in {1, 2}


def test_resolve_vote_returns_negative_when_only_self_alive():
    state = _state(alive_seats=[0])
    assert resolve_vote(CAUTIOUS, state, my_seat=0, recent_votes=[]) == -1
