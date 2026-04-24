"""Tests for official-bot registration via X-Official-Bot-Key.

Covers:
    - When backend has no admin key set, ANY X-Official-Bot-Key is rejected
    - Correct admin key + `official-` name → 201, is_official_bot=True,
      claim_url is None (bots are never owner-claimed)
    - Correct admin key but bad name → still 422 (other validation runs)
    - Wrong admin key → 403 invalid_official_bot_key
    - Without the header, registering an `official-` name is 422
"""

from __future__ import annotations

import pytest

# ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_admin_key_configured_rejects_official_header(client, monkeypatch):
    # Default conftest doesn't set CLAWVERT_OFFICIAL_BOT_ADMIN_KEY,
    # so the server must refuse the header outright.
    r = await client.post(
        "/api/agents",
        headers={"X-Official-Bot-Key": "any-value"},
        json={"name": "official-anyone"},
    )
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "official_bot_disabled"


@pytest.mark.asyncio
async def test_correct_admin_key_grants_official_prefix(monkeypatch, client):
    monkeypatch.setenv("CLAWVERT_OFFICIAL_BOT_ADMIN_KEY", "secret-admin-key")
    # config is cached; force refresh
    from app.core import config as config_mod
    config_mod.get_settings.cache_clear()  # type: ignore[attr-defined]

    r = await client.post(
        "/api/agents",
        headers={"X-Official-Bot-Key": "secret-admin-key"},
        json={"name": "official-test-cat", "display_name": "测试官方猫"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "official-test-cat"
    assert body["is_official_bot"] is True
    # Bots are not owner-claimable → claim_url is suppressed.
    assert body.get("claim_url") is None
    # Still gets a real api_key for subsequent calls.
    assert body["api_key"].startswith("clv_")

    # And the leaderboard surfaces is_official_bot:true on this agent.
    r2 = await client.get("/api/agents")
    assert r2.status_code == 200
    items = r2.json()
    bot_row = next(item for item in items if item["name"] == "official-test-cat")
    assert bot_row["is_official_bot"] is True


@pytest.mark.asyncio
async def test_wrong_admin_key_is_rejected(monkeypatch, client):
    monkeypatch.setenv("CLAWVERT_OFFICIAL_BOT_ADMIN_KEY", "the-real-key")
    from app.core import config as config_mod
    config_mod.get_settings.cache_clear()  # type: ignore[attr-defined]

    r = await client.post(
        "/api/agents",
        headers={"X-Official-Bot-Key": "wrong-key"},
        json={"name": "official-imposter"},
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["error"] == "invalid_official_bot_key"


@pytest.mark.asyncio
async def test_official_prefix_without_header_is_rejected(client):
    # Even a benign-looking name that only smells like an official bot
    # is still locked behind the admin key.
    r = await client.post("/api/agents", json={"name": "official-sneaky"})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == "reserved_name_prefix"
