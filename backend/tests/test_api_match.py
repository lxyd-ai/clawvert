"""End-to-end API tests against the FastAPI app via httpx ASGITransport.

These tests are the canonical 'protocol contract' coverage:
    - register / login / leaderboard happy path
    - 4-player match creation, join, auto-begin
    - viewer-scoped snapshot (anonymous vs spectator vs player_agent)
    - play_token enforcement (missing / wrong → 401/403)
    - same-owner double-seat → 409
    - speak + vote loop ending in civilian win
    - stats accumulate after match_finished

Each test is self-contained — fixtures wipe the DB between runs.
"""

from __future__ import annotations

import pytest

# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────


async def _register(client, name: str) -> tuple[str, str]:
    """Returns (agent_id, raw_api_key)."""
    r = await client.post("/api/agents", json={"name": name})
    assert r.status_code == 201, r.text
    body = r.json()
    return body["agent_id"], body["api_key"]


async def _create_match(client, key: str, n_players=4, n_undercover=1,
                        speak_timeout=120, vote_timeout=120) -> tuple[str, str]:
    """Returns (match_id, host_play_token)."""
    r = await client.post(
        "/api/matches",
        headers={"Authorization": f"Bearer {key}"},
        json={"config": {
            "n_players": n_players, "n_undercover": n_undercover,
            "speak_timeout": speak_timeout, "vote_timeout": vote_timeout,
        }},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    return body["match_id"], body["play_token"]


async def _join(client, mid: str, key: str) -> tuple[int, str]:
    r = await client.post(
        f"/api/matches/{mid}/join",
        headers={"Authorization": f"Bearer {key}"}, json={},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body["your_seat"], body["play_token"]


async def _act(client, mid: str, key: str, token: str, payload: dict, expect=200):
    r = await client.post(
        f"/api/matches/{mid}/action",
        headers={"Authorization": f"Bearer {key}", "X-Play-Token": token},
        json=payload,
    )
    assert r.status_code == expect, r.text
    return r


async def _snapshot(client, mid: str, *, key=None, as_=None) -> dict:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    params = {"as": as_} if as_ else {}
    r = await client.get(f"/api/matches/{mid}", headers=headers, params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ──────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_and_lobby(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["service"] == "clawvert"

    r = await client.get("/api/matches")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_register_and_auth_check(client):
    aid, key = await _register(client, "test_alice")
    r = await client.get("/api/auth/check",
                         headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    assert r.json()["agent_id"] == aid
    assert r.json()["name"] == "test_alice"

    # Public profile
    r = await client.get("/api/agents/test_alice")
    assert r.status_code == 200
    assert r.json()["agent_id"] == aid


@pytest.mark.asyncio
async def test_register_rejects_duplicate(client):
    await _register(client, "dup_name")
    r = await client.post("/api/agents", json={"name": "dup_name"})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "name_taken"


@pytest.mark.asyncio
async def test_register_rejects_reserved_prefix(client):
    r = await client.post("/api/agents", json={"name": "official-bot"})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "reserved_name_prefix"


@pytest.mark.asyncio
async def test_full_match_lifecycle_civilian_wins(client):
    # 4 real agents, 1 undercover
    keys = {}
    for n in ["alice", "bob", "carol", "dave"]:
        _, k = await _register(client, n)
        keys[n] = k

    mid, tok0 = await _create_match(client, keys["alice"], n_players=4, n_undercover=1)
    _, tok1 = await _join(client, mid, keys["bob"])
    _, tok2 = await _join(client, mid, keys["carol"])
    _, tok3 = await _join(client, mid, keys["dave"])
    tokens = [tok0, tok1, tok2, tok3]
    seat_keys = ["alice", "bob", "carol", "dave"]

    # Snapshot is now in_progress, speak_round_1.
    snap = await _snapshot(client, mid, as_="spectator")
    assert snap["status"] == "in_progress"
    assert snap["phase"] == "speak_round_1"
    # Spectator must NOT see role/word for any player.
    for p in snap["players"]:
        assert "role" not in p or p["role"] is None
        assert "word" not in p or p["word"] is None

    # Find the undercover via player_agent view.
    under = None
    for seat, who in enumerate(seat_keys):
        s = await _snapshot(client, mid, key=keys[who])
        assert s["viewer_role"] == "player_agent"
        assert s["your_seat"] == seat
        assert s["your_role"] in ("civilian", "undercover")
        if s["your_role"] == "undercover":
            under = seat

    assert under is not None, "exactly one undercover should be assigned"

    # Speak round 1 — go in seat order.
    for seat, who in enumerate(seat_keys):
        await _act(client, mid, keys[who], tokens[seat],
                   {"type": "speak", "text": f"r1 from {who}"})

    snap = await _snapshot(client, mid, as_="spectator")
    assert snap["phase"] == "vote_round_1"

    # Vote — everyone votes the undercover; undercover votes seat 0 (or 1).
    for seat, who in enumerate(seat_keys):
        target = under if seat != under else (under + 1) % 4
        await _act(client, mid, keys[who], tokens[seat],
                   {"type": "vote", "target_seat": target})

    snap = await _snapshot(client, mid)  # finished → replay view
    assert snap["status"] == "finished"
    assert snap["viewer_role"] == "replay"
    assert snap["result"]["winner_camp"] == "civilian"
    assert under in snap["result"]["losing_seats"]

    # Leaderboard reflects the result.
    r = await client.get("/api/agents")
    by_name = {a["name"]: a for a in r.json()}
    civ_wins = sum(by_name[seat_keys[s]]["wins"] for s in range(4) if s != under)
    assert civ_wins == 3
    assert by_name[seat_keys[under]]["losses"] == 1


@pytest.mark.asyncio
async def test_play_token_required_and_must_match(client):
    keys = {}
    for n in ["pk1", "pk2", "pk3", "pk4"]:
        _, k = await _register(client, n)
        keys[n] = k

    mid, tok0 = await _create_match(client, keys["pk1"])
    await _join(client, mid, keys["pk2"])
    await _join(client, mid, keys["pk3"])
    await _join(client, mid, keys["pk4"])

    # Missing X-Play-Token → 401
    r = await client.post(
        f"/api/matches/{mid}/action",
        headers={"Authorization": f"Bearer {keys['pk1']}"},
        json={"type": "speak", "text": "no token"},
    )
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "play_token_required"

    # Wrong token → 403 mismatch
    r = await client.post(
        f"/api/matches/{mid}/action",
        headers={"Authorization": f"Bearer {keys['pk1']}",
                 "X-Play-Token": "deadbeef" * 4},
        json={"type": "speak", "text": "wrong token"},
    )
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "play_token_mismatch"

    # No bearer at all → 401 auth_required
    r = await client.post(
        f"/api/matches/{mid}/action",
        headers={"X-Play-Token": tok0},
        json={"type": "speak", "text": "no auth"},
    )
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "auth_required"


@pytest.mark.asyncio
async def test_same_owner_blocked_from_two_seats(client):
    # Create the host with a dev-owner agent; second join with the same owner
    # but different agent name MUST be rejected per §7.1.
    r = await client.post(
        "/api/matches",
        headers={"Authorization": "Bearer dev-owner-u1-as-agentA"},
        json={"config": {"n_players": 4, "n_undercover": 1}},
    )
    assert r.status_code == 201
    mid = r.json()["match_id"]

    r = await client.post(
        f"/api/matches/{mid}/join",
        headers={"Authorization": "Bearer dev-owner-u1-as-agentB"}, json={},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "same_owner_already_in_match"


@pytest.mark.asyncio
async def test_spectator_event_filter_hides_private_events(client):
    keys = {}
    for n in ["sv1", "sv2", "sv3", "sv4"]:
        _, k = await _register(client, n)
        keys[n] = k

    mid, tok0 = await _create_match(client, keys["sv1"])
    await _join(client, mid, keys["sv2"])
    await _join(client, mid, keys["sv3"])
    await _join(client, mid, keys["sv4"])

    # Spectator events: should only contain public types.
    r = await client.get(f"/api/matches/{mid}/events?since=0&as=spectator")
    types = {e["type"] for e in r.json()["events"]}
    assert "role_assigned" not in types
    assert "your_turn_to_speak" not in types
    # Sanity: the public events that always exist post-deal:
    assert {"match_created", "match_started", "phase_started"} <= types

    # player_agent should additionally see role_assigned + your_turn_to_speak.
    r = await client.get(
        f"/api/matches/{mid}/events?since=0",
        headers={"Authorization": f"Bearer {keys['sv1']}"},
    )
    types_player = {e["type"] for e in r.json()["events"]}
    assert "role_assigned" in types_player


@pytest.mark.asyncio
async def test_anonymous_long_poll_blocks_then_returns(client):
    keys = {}
    for n in ["lpa", "lpb", "lpc", "lpd"]:
        _, k = await _register(client, n)
        keys[n] = k
    mid, _ = await _create_match(client, keys["lpa"])
    await _join(client, mid, keys["lpb"])
    await _join(client, mid, keys["lpc"])
    await _join(client, mid, keys["lpd"])

    # Ask for events strictly past current latest_seq, wait=1.
    r = await client.get(
        f"/api/matches/{mid}/events?since=999&wait=1",
        timeout=5.0,
    )
    assert r.status_code == 200
    assert r.json()["events"] == []
    # Roughly 1 second of waiting.
    assert r.json()["waited_ms"] >= 800
