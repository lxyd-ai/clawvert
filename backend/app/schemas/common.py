from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ErrorResp(BaseModel):
    error: str
    message: str
    data: dict[str, Any] | None = None


def iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.replace(microsecond=0).isoformat() + "Z"
