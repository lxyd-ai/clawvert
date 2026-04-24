"""FastAPI request dependencies — auth + viewer resolution."""

from __future__ import annotations

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.agent import Agent
from app.services.dev_auth import parse_bearer


async def optional_agent(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> Agent | None:
    agent, _ = await parse_bearer(db, authorization)
    return agent
