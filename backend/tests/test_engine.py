"""Pure-function unit tests for the social-game-v1 engine.

These tests don't touch DB, asyncio or HTTP — they apply Deltas in-memory.
"""

from __future__ import annotations

from app.core.engine import GameEngine
from app.core.state import Delta, MatchState, PlayerState
from app.core.types import phase_str
from app.core.wordpair import WordPair

# ──────────────────────────────────────────────────────────────────
# Test helpers
# ──────────────────────────────────────────────────────────────────


_MATCH_FIELDS = {
    "status", "phase", "round_index", "current_speaker_seat",
    "deadline_ts", "config", "result",
    "wordpair_id", "civilian_word", "undercover_word",
}


def apply_delta(state: MatchState, delta: Delta) -> None:
    """Mutate state in-place to mimic what the persistence layer will do."""
    assert delta.accepted, f"delta rejected: {delta.error}"
    for k, v in delta.match_updates.items():
        if k in _MATCH_FIELDS:
            setattr(state, k, v)
    for seat, upd in delta.player_updates.items():
        p = state.by_seat(seat)
        assert p is not None
        for k, v in upd.items():
            setattr(p, k, v)


def make_post_deal_state(
    roles: list[str],
    *,
    civilian_word: str = "咖啡",
    undercover_word: str = "奶茶",
    n_undercover: int | None = None,
    tie_break: str = "random",
    allow_whisper: bool = False,
    fellow_roles_visible: bool = False,
) -> MatchState:
    """Skip begin_match and craft a state already in speak_round_1."""
    n = len(roles)
    n_under = n_undercover if n_undercover is not None else roles.count("undercover")
    cfg = {
        "n_players": n,
        "n_undercover": n_under,
        "n_blank": roles.count("blank"),
        "speak_timeout": 60,
        "vote_timeout": 90,
        "max_rounds": n,
        "tie_break": tie_break,
        "allow_whisper": allow_whisper,
        "fellow_roles_visible": fellow_roles_visible,
    }
    players: list[PlayerState] = []
    for i, r in enumerate(roles):
        word = (
            civilian_word
            if r == "civilian"
            else (undercover_word if r == "undercover" else None)
        )
        players.append(PlayerState(seat=i, name=f"p{i}", role=r, word=word))
    return MatchState(
        id="t",
        game="undercover",
        status="in_progress",
        phase=phase_str("speak", 1),
        round_index=1,
        current_speaker_seat=0,
        config=cfg,
        players=players,
        civilian_word=civilian_word,
        undercover_word=undercover_word,
    )


def make_waiting_state(n: int = 6, n_undercover: int = 2) -> MatchState:
    cfg = {"n_players": n, "n_undercover": n_undercover, "n_blank": 0,
           "speak_timeout": 60, "vote_timeout": 90, "max_rounds": n,
           "tie_break": "random"}
    players = [PlayerState(seat=i, name=f"p{i}") for i in range(n)]
    return MatchState(id="w", game="undercover", status="waiting",
                      phase="waiting", round_index=0,
                      current_speaker_seat=None, config=cfg,
                      players=players)


# ──────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────


def test_begin_match_assigns_roles_and_words():
    state = make_waiting_state(n=6, n_undercover=2)
    pair = WordPair(id="t", civilian="咖啡", undercover="奶茶", tags=())
    delta = GameEngine.begin_match(state, pair)
    assert delta.accepted
    apply_delta(state, delta)

    assert state.status == "in_progress"
    assert state.phase == phase_str("speak", 1)
    assert state.round_index == 1
    assert state.current_speaker_seat == 0
    assert state.civilian_word == "咖啡"
    assert state.undercover_word == "奶茶"

    civ = [p for p in state.players if p.role == "civilian"]
    und = [p for p in state.players if p.role == "undercover"]
    assert len(civ) == 4
    assert len(und) == 2
    assert all(p.word == "咖啡" for p in civ)
    assert all(p.word == "奶茶" for p in und)

    types = [e.type for e in delta.new_events]
    assert "match_started" in types
    assert "phase_started" in types
    assert "your_turn_to_speak" in types
    assert types.count("role_assigned") == 6


def test_speak_advances_to_next_seat_then_vote_phase():
    state = make_post_deal_state(["civilian"] * 4 + ["undercover"] * 2)

    for seat in range(6):
        delta = GameEngine.apply_speak(state, seat=seat, text=f"speech-{seat}")
        apply_delta(state, delta)
    assert state.phase == phase_str("vote", 1)
    assert state.current_speaker_seat is None
    # After phase change we expect 6 your_turn_to_vote private events in the
    # last delta — that's already produced inside the wrap-around speak call.


def test_speak_rejects_secret_word_in_text():
    state = make_post_deal_state(["civilian"] * 4 + ["undercover"] * 2,
                                 civilian_word="咖啡")
    delta = GameEngine.apply_speak(state, seat=0, text="我喜欢喝咖啡")
    assert not delta.accepted
    assert delta.error and delta.error.code == "speech_contains_secret_word"


def test_speak_rejects_when_not_your_turn():
    state = make_post_deal_state(["civilian"] * 4 + ["undercover"] * 2)
    delta = GameEngine.apply_speak(state, seat=2, text="ahead of turn")
    assert not delta.accepted
    assert delta.error and delta.error.code == "not_your_turn_to_speak"


def test_full_round_civilian_wins_when_all_undercover_voted_out():
    """4 civilians + 1 undercover (3v1 simplified) — everyone votes the lone undercover."""
    state = make_post_deal_state(["civilian"] * 4 + ["undercover"])
    state.config["n_undercover"] = 1

    # Speak round 1
    for seat in range(5):
        apply_delta(state, GameEngine.apply_speak(state, seat=seat,
                                                  text=f"r1-{seat}"))
    assert state.phase == phase_str("vote", 1)

    # 4 civilians vote seat 4 (the lone undercover); seat 4 votes seat 0
    # (can't vote self). seat 4 still gets 4 votes ⇒ eliminated.
    for seat in range(4):
        apply_delta(state, GameEngine.apply_vote(state, seat=seat, target_seat=4))
    apply_delta(state, GameEngine.apply_vote(state, seat=4, target_seat=0))

    assert state.status == "finished"
    assert state.result is not None
    assert state.result["winner_camp"] == "civilian"
    assert state.result["reason"] == "all_undercovers_eliminated"
    assert 4 in state.result["losing_seats"]


def test_undercover_wins_when_majority_reached():
    """2 civ + 2 under, civilians cluster-vote one civilian out → 1v2 → under wins."""
    state = make_post_deal_state(
        ["civilian", "civilian", "undercover", "undercover"],
        n_undercover=2,
    )
    state.config["n_undercover"] = 2
    state.config["n_players"] = 4

    # Round 1: 4 speeches in seat order
    for seat in range(4):
        apply_delta(state, GameEngine.apply_speak(state, seat=seat,
                                                  text=f"r1-{seat}"))
    assert state.phase == phase_str("vote", 1)

    # Both undercovers + one civilian vote seat 0 (civilian); seat 0 votes seat 2
    apply_delta(state, GameEngine.apply_vote(state, seat=0, target_seat=2))
    apply_delta(state, GameEngine.apply_vote(state, seat=1, target_seat=0))
    apply_delta(state, GameEngine.apply_vote(state, seat=2, target_seat=0))
    apply_delta(state, GameEngine.apply_vote(state, seat=3, target_seat=0))

    # seat 0 (civilian) eliminated → 1 civ + 2 und alive → undercover wins
    assert state.status == "finished"
    assert state.result["winner_camp"] == "undercover"
    assert state.result["reason"] == "undercover_majority"
    assert state.by_seat(0).alive is False
    assert state.by_seat(0).revealed_role == "civilian"


def test_concede_eliminates_self_and_can_terminate():
    state = make_post_deal_state(["civilian", "civilian", "undercover"],
                                 n_undercover=1)
    state.config["n_undercover"] = 1
    state.config["n_players"] = 3

    # Undercover concedes immediately → all_undercovers_conceded → civilian wins
    apply_delta(state, GameEngine.apply_concede(state, seat=2))
    assert state.status == "finished"
    assert state.result["winner_camp"] == "civilian"
    assert state.by_seat(2).alive is False
    assert state.by_seat(2).revealed_role == "undercover"


def test_vote_rejects_self_and_dead_targets():
    state = make_post_deal_state(["civilian", "civilian", "undercover", "undercover"])
    # Run through round 1 speeches
    for seat in range(4):
        apply_delta(state, GameEngine.apply_speak(state, seat=seat, text=f"r1-{seat}"))

    bad = GameEngine.apply_vote(state, seat=0, target_seat=0)
    assert not bad.accepted and bad.error.code == "invalid_target"

    bad = GameEngine.apply_vote(state, seat=0, target_seat=99)
    assert not bad.accepted and bad.error.code == "invalid_target"


def test_whisper_disabled_by_default():
    state = make_post_deal_state(["civilian", "civilian", "undercover", "undercover"])
    bad = GameEngine.apply_whisper(state, from_seat=2, to_seat=3, text="hi fellow")
    assert not bad.accepted and bad.error.code == "whisper_not_enabled"


def test_whisper_works_when_enabled_and_within_camp():
    state = make_post_deal_state(["civilian", "civilian", "undercover", "undercover"],
                                 allow_whisper=True, fellow_roles_visible=True)
    delta = GameEngine.apply_whisper(state, from_seat=2, to_seat=3, text="we are 卧底")
    assert delta.accepted
    assert any(e.type == "whisper_received" for e in delta.new_events)

    # Cross-camp whisper rejected
    bad = GameEngine.apply_whisper(state, from_seat=2, to_seat=0, text="psst")
    assert not bad.accepted and bad.error.code == "whisper_target_not_fellow"


def test_abort_only_in_waiting():
    state = make_waiting_state()
    delta = GameEngine.apply_abort(state)
    assert delta.accepted
    apply_delta(state, delta)
    assert state.status == "aborted"


def test_skip_advances_speaker_and_records_skipped_event():
    state = make_post_deal_state(["civilian"] * 4 + ["undercover"] * 2)
    delta = GameEngine.apply_skip(state, seat=0)
    apply_delta(state, delta)
    assert state.current_speaker_seat == 1
    assert any(e.type == "speech_skipped" for e in delta.new_events)


def test_timeout_in_speak_phase_skips_current_speaker():
    """janitor entry: if the speaker doesn't show up before deadline_ts,
    apply_timeout = apply_skip + emits a speech_timeout breadcrumb."""
    state = make_post_deal_state(["civilian"] * 4 + ["undercover"] * 2)
    delta = GameEngine.apply_timeout(state)
    apply_delta(state, delta)
    assert state.current_speaker_seat == 1
    types = [e.type for e in delta.new_events]
    assert "speech_skipped" in types
    assert "speech_timeout" in types


def test_timeout_in_vote_phase_abstains_and_resolves_round():
    state = make_post_deal_state(["civilian"] * 3 + ["undercover"])
    state.config["n_undercover"] = 1
    state.config["n_players"] = 4
    # Do all four speeches → enter vote_round_1
    for seat in range(4):
        apply_delta(state, GameEngine.apply_speak(state, seat=seat, text=f"r1-{seat}"))
    assert state.phase == phase_str("vote", 1)

    # Only seat 0 votes (target seat 3 = the undercover); others time out.
    apply_delta(state, GameEngine.apply_vote(state, seat=0, target_seat=3))
    delta = GameEngine.apply_timeout(state)
    apply_delta(state, delta)

    types = [e.type for e in delta.new_events]
    assert types.count("vote_timeout") == 3  # 3 unvoted alive seats
    assert "round_resolved" in types
    # seat 3 had 1 vote, no other candidate → eliminated
    assert state.by_seat(3).alive is False
    # 1 undercover gone → civilian wins
    assert state.status == "finished"
    assert state.result["winner_camp"] == "civilian"
