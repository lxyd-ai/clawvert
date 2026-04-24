"""
Background housekeeping for clawvert.

Three sweeps every `janitor_interval_sec`:

  1. HARD CAP  — `waiting` matches older than `waiting_max_minutes`
                  are aborted no matter what.
  2. HOST IDLE — `waiting` matches whose seat-0 host hasn't heart-beat
                  for `waiting_host_idle_minutes` are aborted.
  3. PHASE TIMEOUT — `in_progress` matches whose `deadline_ts` has
                  elapsed are force-advanced via engine.apply_timeout
                  (skipping a stuck speaker, abstaining un-voted seats).

Each sweep is best-effort: errors are logged but never crash the loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select

from app.core.config import get_settings
from app.core.db import async_session_maker
from app.models.match import Match, MatchPlayer
from app.services import match_service
from app.services.match_service import MatchError

log = logging.getLogger("clawvert.janitor")


async def _sweep_waiting(now: datetime, hard_minutes: int, idle_minutes: int) -> int:
    candidates: dict[str, str] = {}
    async with async_session_maker() as db:
        if hard_minutes > 0:
            cutoff = now - timedelta(minutes=hard_minutes)
            stmt = (
                select(Match.id)
                .where(Match.status == "waiting", Match.created_at < cutoff)
                .limit(200)
            )
            for mid in (await db.execute(stmt)).scalars().all():
                candidates[mid] = "hard_cap"

        if idle_minutes > 0:
            idle_cutoff = now - timedelta(minutes=idle_minutes)
            stmt = (
                select(Match.id)
                .join(MatchPlayer, MatchPlayer.match_id == Match.id)
                .where(
                    Match.status == "waiting",
                    MatchPlayer.seat == 0,
                    or_(
                        MatchPlayer.last_seen_at < idle_cutoff,
                        (MatchPlayer.last_seen_at.is_(None))
                        & (MatchPlayer.joined_at < idle_cutoff),
                    ),
                )
                .limit(200)
            )
            for mid in (await db.execute(stmt)).scalars().all():
                candidates.setdefault(mid, "host_idle")

        swept = 0
        for mid, tag in candidates.items():
            try:
                await match_service.abort_match(db, mid, reason=f"janitor_{tag}")
                swept += 1
                log.info("janitor reaped waiting match %s (%s)", mid, tag)
            except MatchError as e:
                log.warning("janitor failed to abort %s: %s", mid, e.message)
            except Exception as e:  # noqa: BLE001
                log.warning("janitor unexpected on %s: %r", mid, e)
    return swept


async def _sweep_phase_timeouts(now_ts: int) -> int:
    """Force-advance any in_progress match whose deadline has elapsed."""
    advanced = 0
    async with async_session_maker() as db:
        stmt = (
            select(Match.id)
            .where(
                Match.status == "in_progress",
                Match.deadline_ts.is_not(None),
                Match.deadline_ts < now_ts,
            )
            .limit(200)
        )
        for mid in (await db.execute(stmt)).scalars().all():
            try:
                await match_service.force_timeout(db, mid)
                advanced += 1
                log.info("janitor force-advanced timeout for match %s", mid)
            except MatchError as e:
                log.debug("janitor timeout no-op on %s: %s", mid, e.message)
            except Exception as e:  # noqa: BLE001
                log.warning("janitor timeout failed on %s: %r", mid, e)
    return advanced


async def _sweep_once() -> int:
    settings = get_settings()
    now = datetime.now(UTC)
    now_ts = int(time.time())
    waited = await _sweep_waiting(now, settings.waiting_max_minutes,
                                  settings.waiting_host_idle_minutes)
    advanced = await _sweep_phase_timeouts(now_ts)
    return waited + advanced


async def run() -> None:
    settings = get_settings()
    interval = max(2, int(settings.janitor_interval_sec))
    log.info(
        "janitor started (interval=%ds, hard_cap=%dmin, idle=%dmin)",
        interval, settings.waiting_max_minutes,
        settings.waiting_host_idle_minutes,
    )
    while True:
        try:
            await _sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("janitor sweep crashed: %r", e)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            log.info("janitor cancelled, exiting")
            raise
