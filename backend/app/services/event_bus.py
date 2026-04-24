"""
Per-match asyncio.Event registry powering long-poll wake-ups.

One in-memory dict is sufficient for single-process deployments.
If we ever scale out to multiple API workers, swap the backend with Redis
pub/sub — the public API (`wait_for_new`, `notify`) stays the same.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

_events: dict[str, asyncio.Event] = defaultdict(asyncio.Event)


def _event_for(match_id: str) -> asyncio.Event:
    return _events[match_id]


def notify(match_id: str) -> None:
    """Wake every coroutine awaiting new events for this match."""
    ev = _event_for(match_id)
    ev.set()
    ev.clear()


async def wait_for_new(match_id: str, timeout: float) -> bool:
    """Block up to `timeout` seconds or until `notify(match_id)` is called."""
    if timeout <= 0:
        return False
    ev = _event_for(match_id)
    try:
        await asyncio.wait_for(ev.wait(), timeout=timeout)
        return True
    except TimeoutError:
        return False
