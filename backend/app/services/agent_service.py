"""
Agent identity service.

Owns:
    register_agent      — creates a new Agent + raw api_key (returned ONCE)
    verify_api_key      — bearer-token → Agent (None on miss)
    get_by_name / by_id
    list_leaderboard
    rotate_key          — invalidate old api_key
    record_match_result — stat bump on match_finished

api_key format:
    `clv_<24 hex chars>` → prefix `clv_xxxxxxxx`, hash = sha256(raw)

Names:
    [A-Za-z0-9_-]{3..32}, normalised to lowercase, must not collide with
    dev-stub prefix `dev` or with reserved `official-` prefix when the
    caller isn't using the official-bot admin key.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent

# ── Domain exceptions ────────────────────────────────────────────


class AgentError(Exception):
    status_code: int = 400

    def __init__(self, code: str, message: str,
                 status_code: int | None = None,
                 data: dict[str, Any] | None = None):
        self.code = code
        self.message = message
        self.data = data or {}
        if status_code is not None:
            self.status_code = status_code
        super().__init__(message)


class NameTaken(AgentError):
    status_code = 409


class InvalidName(AgentError):
    status_code = 422


class NotFound(AgentError):
    status_code = 404


# ── Helpers ─────────────────────────────────────────────────────


_API_KEY_PREFIX = "clv_"


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_api_key() -> tuple[str, str, str]:
    """Returns (raw_key, prefix, sha256_hash)."""
    raw = _API_KEY_PREFIX + secrets.token_hex(12)
    return raw, raw[:8], hash_key(raw)


def new_claim_token() -> str:
    return secrets.token_hex(16)


_RESERVED_NAME_PREFIXES = ("dev-", "official-", "system-", "_")
_OFFICIAL_BOT_PREFIX = "official-"


def _normalise_name(name: str, *, allow_official_prefix: bool = False) -> str:
    n = name.strip().lower()
    if not n:
        raise InvalidName("invalid_name", "name must not be empty")
    blocked = tuple(
        p for p in _RESERVED_NAME_PREFIXES
        if not (allow_official_prefix and p == _OFFICIAL_BOT_PREFIX)
    )
    if any(n.startswith(p) for p in blocked):
        raise InvalidName(
            "reserved_name_prefix",
            f"names starting with {blocked} are reserved",
        )
    return n


# ── CRUD ────────────────────────────────────────────────────────


async def register_agent(
    db: AsyncSession,
    *,
    name: str,
    display_name: str | None = None,
    bio: str | None = None,
    homepage: str | None = None,
    contact: str | None = None,
    is_official_bot: bool = False,
) -> tuple[Agent, str]:
    norm = _normalise_name(name, allow_official_prefix=is_official_bot)
    raw_key, prefix, hashed = new_api_key()
    agent = Agent(
        name=norm,
        display_name=display_name or norm,
        bio=bio,
        homepage=homepage,
        contact=contact,
        api_key_hash=hashed,
        api_key_prefix=prefix,
        claim_token=None if is_official_bot else new_claim_token(),
        is_official_bot=is_official_bot,
    )
    db.add(agent)
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise NameTaken("name_taken", f"agent name {norm!r} is already in use") from e
    await db.refresh(agent)
    return agent, raw_key


async def verify_api_key(db: AsyncSession, raw_key: str) -> Agent | None:
    if not raw_key or not raw_key.startswith(_API_KEY_PREFIX):
        return None
    res = await db.execute(
        select(Agent).where(Agent.api_key_hash == hash_key(raw_key))
    )
    return res.scalar_one_or_none()


async def get_by_name(db: AsyncSession, name: str) -> Agent | None:
    n = name.strip().lower()
    res = await db.execute(select(Agent).where(Agent.name == n))
    return res.scalar_one_or_none()


async def get_by_id(db: AsyncSession, agent_id: str) -> Agent | None:
    return await db.get(Agent, agent_id)


async def rotate_key(db: AsyncSession, agent: Agent) -> str:
    raw_key, prefix, hashed = new_api_key()
    agent.api_key_hash = hashed
    agent.api_key_prefix = prefix
    await db.commit()
    return raw_key


async def list_leaderboard(db: AsyncSession, *, limit: int = 50) -> list[Agent]:
    res = await db.execute(
        select(Agent)
        .order_by(desc(Agent.wins), Agent.losses, Agent.created_at)
        .limit(limit)
    )
    return list(res.scalars().all())


# ── Stats bump (called by match_service on terminal) ───────────


async def record_match_result(
    db: AsyncSession, *, winning_agent_ids: list[str],
    losing_agent_ids: list[str], aborted: bool = False,
) -> None:
    """Bump wins/losses/aborts counters in a single round-trip per agent."""
    if aborted:
        for aid in {*winning_agent_ids, *losing_agent_ids}:
            if not aid:
                continue
            agent = await db.get(Agent, aid)
            if agent is None:
                continue
            agent.aborts = (agent.aborts or 0) + 1
            agent.games_played = (agent.games_played or 0) + 1
        return
    for aid in winning_agent_ids:
        if not aid:
            continue
        agent = await db.get(Agent, aid)
        if agent is None:
            continue
        agent.wins = (agent.wins or 0) + 1
        agent.games_played = (agent.games_played or 0) + 1
    for aid in losing_agent_ids:
        if not aid:
            continue
        agent = await db.get(Agent, aid)
        if agent is None:
            continue
        agent.losses = (agent.losses or 0) + 1
        agent.games_played = (agent.games_played or 0) + 1


# ── Misc ────────────────────────────────────────────────────────


async def count_agents(db: AsyncSession) -> int:
    res = await db.execute(select(func.count(Agent.id)))
    return int(res.scalar() or 0)
