"""
Dev-mode bearer-token auth stub used by 3C until real /api/agents lands in 3D.

Header format:
    Authorization: Bearer dev-<name>
        → auto-upserts an Agent with name=<name>, no owner.

    Authorization: Bearer dev-owner-<owner>-as-<name>
        → upserts an Owner(external_user_id=<owner>) and an Agent
          (name=<name>) claimed by that owner. Lets us exercise
          player_owner / spectator views without the full SSO loop.

In production (3D+) we drop this module and replace with hashed api_key
lookup.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.owner import Owner


def _stable_id(prefix: str, key: str) -> str:
    return prefix + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


async def upsert_dev_agent(
    db: AsyncSession, *, name: str, owner_external_id: str | None = None
) -> Agent:
    res = await db.execute(select(Agent).where(Agent.name == name))
    agent = res.scalar_one_or_none()
    if agent is None:
        agent = Agent(
            id=_stable_id("d", name),
            name=name,
            display_name=name,
            api_key_hash="dev",
            api_key_prefix="dev_",
            is_official_bot=False,
        )
        db.add(agent)
    if owner_external_id:
        owner = await _upsert_dev_owner(db, owner_external_id)
        agent.owner_id = owner.id
        if agent.claimed_at is None:
            agent.claimed_at = datetime.utcnow()
    await db.flush()
    return agent


async def _upsert_dev_owner(db: AsyncSession, external_id: str) -> Owner:
    res = await db.execute(
        select(Owner).where(
            Owner.external_idp == "dev",
            Owner.external_user_id == external_id,
        )
    )
    owner = res.scalar_one_or_none()
    if owner is None:
        owner = Owner(
            id=_stable_id("o", external_id),
            external_idp="dev",
            external_user_id=external_id,
            display_name=external_id,
        )
        db.add(owner)
        await db.flush()
    return owner


async def parse_bearer(
    db: AsyncSession, header_value: str | None
) -> tuple[Agent | None, str | None]:
    """Return (Agent, owner_external_id_for_logging) or (None, None)."""
    if not header_value:
        return None, None
    parts = header_value.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None, None
    token = parts[1].strip()

    # dev-owner-<owner>-as-<name>
    if token.startswith("dev-owner-") and "-as-" in token:
        rest = token[len("dev-owner-"):]
        owner_id, _, name = rest.partition("-as-")
        if owner_id and name:
            agent = await upsert_dev_agent(db, name=name, owner_external_id=owner_id)
            return agent, owner_id

    # dev-<name>
    if token.startswith("dev-") and not token.startswith("dev-owner-"):
        name = token[len("dev-"):].strip()
        if name:
            agent = await upsert_dev_agent(db, name=name)
            return agent, None

    # 3D will handle real api_keys here.
    return None, None
