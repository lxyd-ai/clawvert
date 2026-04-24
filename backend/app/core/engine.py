"""Pure game engine for social-game-v1 / undercover.

All methods take a `MatchState` snapshot + an action payload and return a
`Delta`. The engine never touches DB or asyncio.

Phase transitions handled here:
    waiting              ─(begin_match)─▶  speak_round_1
    speak_round_N        ─(speak/skip)──▶  vote_round_N         (when round done)
    vote_round_N         ─(vote)────────▶  reveal_round_N       (when round resolved)
    reveal_round_N       ─(internal)────▶  speak_round_(N+1)    (or finished)
    *                    ─(concede)─────▶  same phase or finished

The persistence layer is responsible for:
    1. Generating monotonic `seq` numbers for each NewEvent
    2. Calling `event_bus.notify(match_id)` after the transaction commits
    3. Wiring deadline_ts timer tasks (engine emits the deadline value;
       firing the timer is the caller's job — usually a janitor / asyncio.Task)
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Sequence

from app.core.state import Delta, MatchState, PlayerState
from app.core.types import (
    ROLE_BLANK,
    ROLE_CIVILIAN,
    ROLE_UNDERCOVER,
    VIS_GOD,
    VIS_PUBLIC,
    phase_str,
    vis_role,
    vis_seat,
)
from app.core.wordpair import WordPair


def _now_ts() -> int:
    return int(time.time())


# ──────────────────────────────────────────────────────────────────
# Public engine surface
# ──────────────────────────────────────────────────────────────────


class GameEngine:
    """Stateless façade — every method is a pure transformation."""

    # ── Lifecycle ────────────────────────────────────────────────

    @staticmethod
    def begin_match(state: MatchState, wordpair: WordPair) -> Delta:
        """Transition `waiting` → `in_progress` + `speak_round_1`.

        Caller must have already validated that `len(state.players) == n_players`.
        Roles are assigned in this call using `secrets.SystemRandom`.
        """
        if state.status != "waiting":
            return Delta.reject("wrong_status", f"cannot begin from status={state.status}")
        cfg = state.config
        n = int(cfg.get("n_players", 6))
        n_under = int(cfg.get("n_undercover", 2))
        n_blank = int(cfg.get("n_blank", 0))
        n_civ = n - n_under - n_blank
        if len(state.players) != n:
            return Delta.reject("not_full", f"expected {n} players, got {len(state.players)}")
        if n_under >= n / 2 or n_civ < 1 or n_under < 1:
            return Delta.reject("invalid_config", "n_undercover must satisfy 1 ≤ U < N/2")
        if n_blank < 0 or n_civ < n_blank:
            return Delta.reject("invalid_config", "blank seats must fit in civilian count")

        roles: list[str] = (
            [ROLE_CIVILIAN] * n_civ
            + [ROLE_UNDERCOVER] * n_under
            + [ROLE_BLANK] * n_blank
        )
        rng = secrets.SystemRandom()
        rng.shuffle(roles)

        speak_timeout = int(cfg.get("speak_timeout", 60))
        deadline = _now_ts() + speak_timeout

        delta = Delta(
            match_updates={
                "status": "in_progress",
                "phase": phase_str("speak", 1),
                "round_index": 1,
                "current_speaker_seat": _first_alive_seat(state.players),
                "deadline_ts": deadline,
                "wordpair_id": wordpair.id,
                "civilian_word": wordpair.civilian,
                "undercover_word": wordpair.undercover,
            },
            summary={
                "phase": phase_str("speak", 1),
                "current_speaker_seat": _first_alive_seat(state.players),
            },
        )

        # Public match_started (no role/word leakage).
        delta.add_event(
            "match_started",
            VIS_PUBLIC,
            n_players=n,
            n_undercover=n_under,
            n_blank=n_blank,
            first_speaker_seat=delta.match_updates["current_speaker_seat"],
        )

        # God-only event for spectator/replay UI to show wordpair.
        delta.add_event(
            "wordpair_revealed",
            VIS_GOD,
            wordpair_id=wordpair.id,
            civilian=wordpair.civilian,
            undercover=wordpair.undercover,
        )

        # Per-seat private role assignment + word.
        for p, role in zip(sorted(state.players, key=lambda x: x.seat), roles, strict=False):
            word = (
                wordpair.civilian
                if role == ROLE_CIVILIAN
                else (wordpair.undercover if role == ROLE_UNDERCOVER else None)
            )
            delta.update_player(p.seat, role=role, word=word, has_spoken_this_round=False,
                                has_voted_this_round=False, last_vote_target_seat=None)
            delta.add_event(
                "role_assigned",
                vis_seat(p.seat),
                seat=p.seat,
                role=role,
                word=word,
            )

        # Optional fellow-role hint when allow_whisper or fellow_roles_visible is on.
        if cfg.get("fellow_roles_visible") or cfg.get("allow_whisper"):
            under_seats = [
                p.seat
                for p, r in zip(sorted(state.players, key=lambda x: x.seat), roles, strict=False)
                if r == ROLE_UNDERCOVER
            ]
            delta.add_event(
                "fellow_role_revealed",
                vis_role(ROLE_UNDERCOVER),
                role=ROLE_UNDERCOVER,
                fellow_seats=under_seats,
                note="本局所有卧底彼此身份可见",
            )

        # Phase transition + first turn cue.
        delta.add_event(
            "phase_started",
            VIS_PUBLIC,
            phase=phase_str("speak", 1),
            round_index=1,
            deadline_ts=deadline,
        )
        delta.add_event(
            "your_turn_to_speak",
            vis_seat(delta.match_updates["current_speaker_seat"]),
            seat=delta.match_updates["current_speaker_seat"],
            deadline_ts=deadline,
        )
        return delta

    # ── Speak / Skip ─────────────────────────────────────────────

    @staticmethod
    def apply_speak(state: MatchState, seat: int, text: str, *, skipped: bool = False) -> Delta:
        kind, round_n = _expect_phase(state, "speak")
        if kind != "speak":
            return Delta.reject("wrong_phase", f"current phase is {state.phase}")
        if state.current_speaker_seat != seat:
            return Delta.reject("not_your_turn_to_speak",
                                f"current speaker is seat={state.current_speaker_seat}")
        speaker = state.by_seat(seat)
        if speaker is None or not speaker.alive:
            return Delta.reject("you_are_eliminated", "eliminated players cannot act")

        # Forbid leaking the secret word verbatim (D9). Caller can short-circuit
        # to 422 via delta.error.
        if not skipped and speaker.word and speaker.word in text:
            return Delta.reject("speech_contains_secret_word",
                                "speech must not contain your secret word verbatim")

        delta = Delta()
        delta.update_player(seat, has_spoken_this_round=True)

        if skipped:
            delta.add_event("speech_skipped", VIS_PUBLIC, seat=seat, round=round_n)
        else:
            delta.add_event("speech_posted", VIS_PUBLIC, seat=seat, round=round_n, text=text)

        next_seat = _next_speaker_seat(state, seat)
        cfg = state.config

        if next_seat is not None:
            speak_timeout = int(cfg.get("speak_timeout", 60))
            deadline = _now_ts() + speak_timeout
            delta.match_updates.update(current_speaker_seat=next_seat, deadline_ts=deadline)
            delta.summary.update(next_speaker_seat=next_seat,
                                 round_speak_progress=_speak_progress(state, after_seat=seat))
            delta.add_event("your_turn_to_speak", vis_seat(next_seat),
                            seat=next_seat, deadline_ts=deadline)
        else:
            # Round of speeches done → enter vote phase.
            vote_timeout = int(cfg.get("vote_timeout", 90))
            deadline = _now_ts() + vote_timeout
            delta.match_updates.update(
                phase=phase_str("vote", round_n),
                current_speaker_seat=None,
                deadline_ts=deadline,
            )
            # Reset round-local flags to prepare for vote.
            for p in state.alive():
                delta.update_player(p.seat, has_voted_this_round=False,
                                    last_vote_target_seat=None)
            delta.add_event("phase_started", VIS_PUBLIC,
                            phase=phase_str("vote", round_n),
                            round_index=round_n, deadline_ts=deadline)
            for p in state.alive():
                eligible = [q.seat for q in state.alive() if q.seat != p.seat]
                delta.add_event("your_turn_to_vote", vis_seat(p.seat),
                                seat=p.seat, deadline_ts=deadline,
                                eligible_targets=eligible)
            delta.summary.update(phase=phase_str("vote", round_n))

        return delta

    @staticmethod
    def apply_skip(state: MatchState, seat: int) -> Delta:
        return GameEngine.apply_speak(state, seat, text="", skipped=True)

    # ── Vote ─────────────────────────────────────────────────────

    @staticmethod
    def apply_vote(state: MatchState, seat: int, target_seat: int) -> Delta:
        kind, round_n = _expect_phase(state, "vote")
        if kind != "vote":
            return Delta.reject("wrong_phase", f"current phase is {state.phase}")
        voter = state.by_seat(seat)
        if voter is None or not voter.alive:
            return Delta.reject("you_are_eliminated", "eliminated players cannot vote")
        if voter.has_voted_this_round:
            return Delta.reject("already_voted", "one vote per round")
        if target_seat == seat:
            return Delta.reject("invalid_target", "cannot vote yourself")
        target = state.by_seat(target_seat)
        if target is None or not target.alive:
            return Delta.reject("invalid_target", "target seat is not an alive player")

        delta = Delta()
        delta.update_player(seat, has_voted_this_round=True,
                            last_vote_target_seat=target_seat)
        # D4 — public vote_cast carries target_seat in real time.
        delta.add_event("vote_cast", VIS_PUBLIC,
                        seat=seat, round=round_n, target_seat=target_seat)
        delta.add_event("your_vote_recorded", vis_seat(seat),
                        seat=seat, target_seat=target_seat)

        # Compute voted/total against alive count (using state + this delta).
        alive_seats = [p.seat for p in state.alive()]
        voted_seats = {
            p.seat for p in state.alive()
            if p.has_voted_this_round or p.seat == seat
        }
        delta.summary.update(votes_in=f"{len(voted_seats)}/{len(alive_seats)}",
                             your_vote=target_seat)

        if len(voted_seats) >= len(alive_seats):
            # All alive voted — resolve the round.
            return _merge(delta, _resolve_vote_round(state, round_n,
                                                    last_voter_seat=seat,
                                                    last_target_seat=target_seat))
        return delta

    # ── Concede ──────────────────────────────────────────────────

    @staticmethod
    def apply_concede(state: MatchState, seat: int) -> Delta:
        if state.status != "in_progress":
            return Delta.reject("match_not_in_progress",
                                f"status is {state.status}")
        p = state.by_seat(seat)
        if p is None:
            return Delta.reject("agent_not_in_match", "no such seat")
        if not p.alive:
            return Delta.reject("you_are_eliminated", "already out")
        delta = Delta()
        delta.update_player(seat, alive=False, eliminated_reason="concede",
                            eliminated_in_round=state.round_index,
                            revealed_role=p.role)
        delta.add_event("player_conceded", VIS_PUBLIC,
                        seat=seat, revealed_role=p.role,
                        round=state.round_index)
        # Mutate snapshot in place so the terminal check sees the new alive set.
        p.alive = False
        p.revealed_role = p.role
        terminal = _check_terminal(state)
        if terminal is not None:
            return _merge(delta, _finalise(state, terminal))
        delta.summary.update(remaining_civilians=len(state.alive_role(ROLE_CIVILIAN)),
                             remaining_undercovers=len(state.alive_role(ROLE_UNDERCOVER)))
        return delta

    # ── Whisper (default off) ────────────────────────────────────

    @staticmethod
    def apply_whisper(state: MatchState, from_seat: int, to_seat: int, text: str) -> Delta:
        if not state.config.get("allow_whisper"):
            return Delta.reject("whisper_not_enabled",
                                "config.allow_whisper=false")
        if state.status != "in_progress":
            return Delta.reject("match_not_in_progress",
                                f"status is {state.status}")
        sender = state.by_seat(from_seat)
        receiver = state.by_seat(to_seat)
        if sender is None or receiver is None:
            return Delta.reject("invalid_target", "no such seat")
        if not sender.alive or not receiver.alive:
            return Delta.reject("invalid_target", "whisper target must be alive")
        if from_seat == to_seat:
            return Delta.reject("invalid_target", "cannot whisper yourself")
        if sender.role != receiver.role:
            return Delta.reject("whisper_target_not_fellow",
                                "whisper only allowed within the same camp")
        delta = Delta()
        delta.add_event("whisper_received", vis_role(sender.role),
                        from_seat=from_seat, to_seat=to_seat, text=text)
        # No public broadcast at all (consistent with D6 v2).
        return delta

    # ── Timeout (called by janitor when deadline_ts elapses) ─────

    @staticmethod
    def apply_timeout(state: MatchState) -> Delta:
        """Force the current phase to advance after deadline_ts elapses.

        - speak_round_N: the current speaker is skipped (their `eliminated_*`
          stays untouched; D8 punishment is left to product decision).
        - vote_round_N: every alive player who hasn't voted is recorded as
          abstain (last_vote_target_seat=None, has_voted_this_round=True),
          then the round resolves normally.
        """
        if state.status != "in_progress":
            return Delta.reject("match_not_in_progress",
                                f"status is {state.status}")
        kind, round_n = _expect_phase(state, "speak")
        if kind == "speak":
            current = state.current_speaker_seat
            if current is None:
                return Delta.reject("no_current_speaker", "nothing to time out")
            delta = GameEngine.apply_skip(state, seat=current)
            if delta.accepted:
                delta.add_event("speech_timeout", VIS_PUBLIC,
                                seat=current, round=round_n)
            return delta

        if kind == "vote":
            delta = Delta()
            # Mark every un-voted alive seat as abstain and emit a public note.
            unvoted = [p.seat for p in state.alive() if not p.has_voted_this_round]
            for s in unvoted:
                delta.update_player(s, has_voted_this_round=True,
                                    last_vote_target_seat=None)
                p = state.by_seat(s)
                if p is not None:
                    p.has_voted_this_round = True
                    p.last_vote_target_seat = None
                delta.add_event("vote_timeout", VIS_PUBLIC,
                                seat=s, round=round_n)
            # Resolve the round now (note: there is no "last voter" — the
            # janitor closed the round, so we feed dummy values that won't
            # double-count anything because all has_voted flags are set).
            return _merge(delta, _resolve_vote_round(state, round_n,
                                                    last_voter_seat=-1,
                                                    last_target_seat=-1))

        return Delta.reject("nothing_to_timeout",
                            f"phase {state.phase} has no timeout transition")

    # ── Lifecycle abort (host cancels waiting room) ──────────────

    @staticmethod
    def apply_abort(state: MatchState, reason: str = "host_cancelled") -> Delta:
        if state.status != "waiting":
            return Delta.reject("match_not_waiting",
                                f"only waiting rooms can be aborted; status={state.status}")
        delta = Delta(
            match_updates={"status": "aborted",
                           "phase": "finished",
                           "result": {"winner_camp": None, "reason": reason,
                                      "summary": "对局未开始即取消"}},
        )
        delta.add_event("match_aborted", VIS_PUBLIC, reason=reason)
        return delta


# ──────────────────────────────────────────────────────────────────
# Internal helpers (pure)
# ──────────────────────────────────────────────────────────────────


def _expect_phase(state: MatchState, expected_kind: str) -> tuple[str, int]:
    if state.status != "in_progress":
        return ("", 0)
    phase = state.phase or ""
    if "_round_" in phase:
        kind, _, idx = phase.partition("_round_")
        return kind, int(idx)
    return phase, 0


def _first_alive_seat(players: Sequence[PlayerState]) -> int:
    return min(p.seat for p in players if p.alive)


def _next_speaker_seat(state: MatchState, after_seat: int) -> int | None:
    """Return next alive seat (in seat order) that hasn't spoken this round."""
    alive_seats = sorted(p.seat for p in state.alive())
    if not alive_seats:
        return None
    # We're the speaker, mark conceptually.
    spoken_set = {p.seat for p in state.players
                  if p.has_spoken_this_round} | {after_seat}
    remaining = [s for s in alive_seats if s not in spoken_set]
    return remaining[0] if remaining else None


def _speak_progress(state: MatchState, after_seat: int) -> str:
    alive = state.alive()
    spoken = sum(1 for p in alive if p.has_spoken_this_round) + (
        1 if state.by_seat(after_seat) and not state.by_seat(after_seat).has_spoken_this_round  # type: ignore[union-attr]
        else 0
    )
    return f"{spoken}/{len(alive)}"


def _resolve_vote_round(
    state: MatchState, round_n: int,
    *, last_voter_seat: int, last_target_seat: int,
) -> Delta:
    """Tally votes, eliminate (with tie_break), check terminal, advance phase."""
    cfg = state.config
    tie_break = cfg.get("tie_break", "random")

    # Build vote_breakdown counting THIS round's votes (use last_vote_target_seat
    # on every alive player, plus the voter we just recorded which the snapshot
    # doesn't know about yet).
    breakdown: dict[int, int] = {}
    for p in state.alive():
        target = (
            last_target_seat if p.seat == last_voter_seat
            else p.last_vote_target_seat
        )
        if target is None:
            continue
        breakdown[target] = breakdown.get(target, 0) + 1

    if not breakdown:
        # Everyone abstained — treat per tie_break (no candidates → noop).
        return _advance_after_round(state, round_n, eliminated_seat=None,
                                    breakdown=breakdown,
                                    tie_break_used=tie_break,
                                    summary_extra={"reason": "all_abstained"})

    max_votes = max(breakdown.values())
    candidates = [seat for seat, votes in breakdown.items() if votes == max_votes]
    eliminated_seat: int | None
    tie_used: str | None = None
    if len(candidates) == 1:
        eliminated_seat = candidates[0]
    else:
        tie_used = tie_break
        if tie_break == "random":
            eliminated_seat = secrets.SystemRandom().choice(candidates)
        elif tie_break == "noop_then_random":
            # First tie this round → noop; engine doesn't yet track consecutive
            # tied rounds across rounds, so pragmatic v1 behaviour: noop once,
            # next tie uses random. (Noop tracking persists in match.config.)
            consecutive = int(cfg.get("_consecutive_tie_rounds", 0))
            if consecutive >= 1:
                eliminated_seat = secrets.SystemRandom().choice(candidates)
            else:
                eliminated_seat = None
        else:
            # `revote` is not implemented in v0.1; falls back to random.
            eliminated_seat = secrets.SystemRandom().choice(candidates)

    return _advance_after_round(state, round_n,
                                eliminated_seat=eliminated_seat,
                                breakdown=breakdown,
                                tie_break_used=tie_used,
                                summary_extra={})


def _advance_after_round(
    state: MatchState, round_n: int, *,
    eliminated_seat: int | None,
    breakdown: dict[int, int],
    tie_break_used: str | None,
    summary_extra: dict,
) -> Delta:
    delta = Delta()
    cfg = state.config

    # Record reveal phase + round_resolved event.
    delta.match_updates.update(phase=f"reveal_round_{round_n}",
                               current_speaker_seat=None,
                               deadline_ts=None)
    revealed_role = None
    if eliminated_seat is not None:
        target = state.by_seat(eliminated_seat)
        revealed_role = target.role if target else None
        delta.update_player(eliminated_seat, alive=False,
                            eliminated_in_round=round_n,
                            eliminated_reason="vote",
                            revealed_role=revealed_role)
        # mutate snapshot so terminal check sees the elimination
        if target:
            target.alive = False
            target.eliminated_in_round = round_n
            target.eliminated_reason = "vote"
            target.revealed_role = revealed_role

    delta.add_event("round_resolved", VIS_PUBLIC,
                    round=round_n,
                    eliminated_seat=eliminated_seat,
                    revealed_role=revealed_role,
                    vote_breakdown=breakdown,
                    tie_break=tie_break_used)

    # Track consecutive ties for noop_then_random.
    if eliminated_seat is None and len(breakdown) > 0:
        prev = int(cfg.get("_consecutive_tie_rounds", 0))
        delta.match_updates["config"] = {**cfg, "_consecutive_tie_rounds": prev + 1}
    elif eliminated_seat is not None and cfg.get("_consecutive_tie_rounds"):
        delta.match_updates["config"] = {**cfg, "_consecutive_tie_rounds": 0}

    delta.summary.update(
        round_result={
            "eliminated_seat": eliminated_seat,
            "eliminated_role": revealed_role,
            "vote_breakdown": breakdown,
            "tie_break": tie_break_used,
            "remaining_civilians": len(state.alive_role(ROLE_CIVILIAN)),
            "remaining_undercovers": len(state.alive_role(ROLE_UNDERCOVER)),
            **summary_extra,
        }
    )

    # Terminal check
    terminal = _check_terminal(state, round_n=round_n)
    if terminal is not None:
        return _merge(delta, _finalise(state, terminal))

    # Otherwise — start next speak round.
    next_round = round_n + 1
    speak_timeout = int(cfg.get("speak_timeout", 60))
    deadline = _now_ts() + speak_timeout
    next_speaker = _first_alive_seat(state.players)

    delta.match_updates.update(
        phase=phase_str("speak", next_round),
        round_index=next_round,
        current_speaker_seat=next_speaker,
        deadline_ts=deadline,
    )
    for p in state.alive():
        delta.update_player(p.seat, has_spoken_this_round=False,
                            has_voted_this_round=False,
                            last_vote_target_seat=None)

    delta.add_event("phase_started", VIS_PUBLIC,
                    phase=phase_str("speak", next_round),
                    round_index=next_round, deadline_ts=deadline)
    delta.add_event("your_turn_to_speak", vis_seat(next_speaker),
                    seat=next_speaker, deadline_ts=deadline)
    delta.summary["next_phase"] = phase_str("speak", next_round)
    return delta


def _check_terminal(state: MatchState, *, round_n: int = 0) -> dict | None:
    """Returns dict {winner_camp, reason, ...} if terminal, else None.

    D12: walking past max_rounds counts as undercover win (their stealth paid off).
    """
    alive_under = state.alive_role(ROLE_UNDERCOVER)
    alive_civ = state.alive_role(ROLE_CIVILIAN)

    if not alive_under:
        return {"winner_camp": "civilian",
                "reason": "all_undercovers_eliminated",
                "summary": "平民胜：卧底全军覆没"}
    if len(alive_under) >= len(alive_civ):
        return {"winner_camp": "undercover",
                "reason": "undercover_majority",
                "summary": "卧底胜：卧底人数已 ≥ 平民人数"}
    max_rounds = int(state.config.get("max_rounds", state.config.get("n_players", 6)))
    if round_n >= max_rounds:
        return {"winner_camp": "undercover",
                "reason": "max_rounds_reached",
                "summary": "卧底胜：潜伏到底成功"}
    return None


def _finalise(state: MatchState, terminal: dict) -> Delta:
    delta = Delta()
    winner_camp = terminal["winner_camp"]
    winning_seats = [p.seat for p in state.players if p.role == winner_camp]
    losing_seats = [p.seat for p in state.players if p.role and p.role != winner_camp]
    result = {
        "winner_camp": winner_camp,
        "winning_seats": sorted(winning_seats),
        "losing_seats": sorted(losing_seats),
        "reason": terminal["reason"],
        "summary": terminal.get("summary"),
        "wordpair": {
            "civilian": next((p.word for p in state.players
                              if p.role == ROLE_CIVILIAN), None),
            "undercover": next((p.word for p in state.players
                                if p.role == ROLE_UNDERCOVER), None),
        },
    }
    delta.match_updates.update(status="finished", phase="finished",
                               result=result, deadline_ts=None,
                               current_speaker_seat=None)
    delta.add_event("match_finished", VIS_PUBLIC, **{
        k: v for k, v in result.items() if k != "wordpair"
    } | {"wordpair": result["wordpair"]})
    delta.summary["result"] = result
    return delta


def _merge(a: Delta, b: Delta) -> Delta:
    """Combine two deltas (a then b). b's match_updates override a's."""
    if not b.accepted:
        return b
    out = Delta(
        match_updates={**a.match_updates, **b.match_updates},
        player_updates={},
        new_events=[*a.new_events, *b.new_events],
        summary={**a.summary, **b.summary},
    )
    for seat, fields in a.player_updates.items():
        out.player_updates.setdefault(seat, {}).update(fields)
    for seat, fields in b.player_updates.items():
        out.player_updates.setdefault(seat, {}).update(fields)
    return out
