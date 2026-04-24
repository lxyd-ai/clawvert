from __future__ import annotations

import secrets
from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def _ulid() -> str:
    """Compact 12-char base32 id; collision-resistant enough for our scale."""
    return secrets.token_hex(8)


class Owner(Base):
    __tablename__ = "owners"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_ulid)
    # External identity (ClawdChat user id by default; left wide for any IdP)
    external_idp: Mapped[str] = mapped_column(String(32), default="clawdchat")
    external_user_id: Mapped[str] = mapped_column(String(64), index=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
