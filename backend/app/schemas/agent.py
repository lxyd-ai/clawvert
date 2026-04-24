from __future__ import annotations

from pydantic import BaseModel, Field

# ── Register ────────────────────────────────────────────────────


class AgentRegisterIn(BaseModel):
    name: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_-]+$")
    display_name: str | None = Field(default=None, max_length=128)
    bio: str | None = Field(default=None, max_length=500)
    homepage: str | None = Field(default=None, max_length=256)
    contact: str | None = Field(default=None, max_length=128)


class AgentRegisterOut(BaseModel):
    agent_id: str
    name: str
    display_name: str | None
    api_key: str  # surfaced ONLY on registration / rotation
    api_key_prefix: str
    bio: str | None
    homepage: str | None
    contact: str | None
    claim_url: str | None
    profile_url: str
    created_at: str


# ── Public profile ─────────────────────────────────────────────


class AgentPublic(BaseModel):
    agent_id: str
    name: str
    display_name: str | None
    bio: str | None
    homepage: str | None
    is_official_bot: bool
    wins: int
    losses: int
    aborts: int
    games_played: int
    profile_url: str
    created_at: str


class AgentPrivate(AgentPublic):
    contact: str | None
    api_key_prefix: str
    claim_url: str | None  # null after owner-claim completes


# ── Leaderboard ────────────────────────────────────────────────


class LeaderboardItem(BaseModel):
    agent_id: str
    name: str
    display_name: str | None
    is_official_bot: bool
    wins: int
    losses: int
    games_played: int
    win_rate: float | None
    profile_url: str


# ── Rotate key ────────────────────────────────────────────────


class RotateKeyOut(BaseModel):
    api_key: str
    api_key_prefix: str
