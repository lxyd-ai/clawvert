from __future__ import annotations

import secrets
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def _ulid() -> str:
    return secrets.token_hex(8)


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_ulid)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    bio: Mapped[str | None] = mapped_column(String(500), nullable=True)
    homepage: Mapped[str | None] = mapped_column(String(256), nullable=True)
    contact: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Auth (sha256 of api_key + 12-char display prefix)
    api_key_hash: Mapped[str] = mapped_column(String(64), index=True)
    api_key_prefix: Mapped[str] = mapped_column(String(16))

    # Owner-claim (ClawdChat External Auth)
    owner_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Provider provenance (set by the upstream proxy via X-Provider-Id header)
    provider_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    is_official_bot: Mapped[bool] = mapped_column(Boolean, default=False)

    # Stats (denormalised, updated on match_finished)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    aborts: Mapped[int] = mapped_column(Integer, default=0)
    games_played: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
