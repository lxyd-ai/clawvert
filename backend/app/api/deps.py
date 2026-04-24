"""FastAPI request dependencies — auth + viewer resolution."""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException
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


async def require_agent(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> Agent:
    agent, _ = await parse_bearer(db, authorization)
    if agent is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "auth_required",
                "message": "Authorization: Bearer <api_key> required",
            },
        )
    return agent
