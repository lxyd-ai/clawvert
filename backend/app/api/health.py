from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["meta"])


@router.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "service": "clawvert"}
