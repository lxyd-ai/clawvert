"""Wordpair library — loads ./data/wordpairs.json with mtime-based hot reload.

Format on disk:
    [{"id": "phone_call", "civilian": "手机", "undercover": "电话",
      "tags": ["生活"]}, ...]

A pair is the source of two distinct words shown to the two camps.
Selection at deal time uses `secrets.choice` so agents cannot predict
or coordinate on a pair via timing.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import dataclass

from app.core.config import get_settings


@dataclass(frozen=True)
class WordPair:
    id: str
    civilian: str
    undercover: str
    tags: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "civilian": self.civilian,
            "undercover": self.undercover,
            "tags": list(self.tags),
        }


class WordpairLibrary:
    """Thread-safe wordpair store with mtime-based hot reload."""

    def __init__(self, path: str | None = None) -> None:
        s = get_settings()
        self.path = os.path.abspath(path or s.wordpairs_path)
        self._reload_interval = s.wordpairs_reload_interval_sec
        self._lock = threading.Lock()
        self._mtime: float = 0.0
        self._pairs: list[WordPair] = []
        self._by_tag: dict[str, list[WordPair]] = {}
        self._load_locked()

    # ── public API ───────────────────────────────────────────────
    def all(self) -> list[WordPair]:
        self._maybe_reload()
        return list(self._pairs)

    def by_tag(self, tag: str | None) -> list[WordPair]:
        self._maybe_reload()
        if tag is None:
            return list(self._pairs)
        return list(self._by_tag.get(tag, []))

    def random_pair(self, tag: str | None = None) -> WordPair:
        pool = self.by_tag(tag) or self._pairs
        if not pool:
            raise RuntimeError(
                f"wordpair library empty (path={self.path}, tag={tag!r}); "
                "make sure wordpairs.json exists and is non-empty"
            )
        return secrets.choice(pool)

    def get(self, pair_id: str) -> WordPair | None:
        self._maybe_reload()
        for p in self._pairs:
            if p.id == pair_id:
                return p
        return None

    # ── reload machinery ─────────────────────────────────────────
    def _maybe_reload(self) -> None:
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return
        if mtime <= self._mtime:
            return
        with self._lock:
            if mtime <= self._mtime:
                return
            self._load_locked()

    def _load_locked(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            self._mtime = os.path.getmtime(self.path)
        except FileNotFoundError:
            self._pairs = []
            self._by_tag = {}
            return
        pairs: list[WordPair] = []
        by_tag: dict[str, list[WordPair]] = {}
        for item in raw:
            p = WordPair(
                id=item["id"],
                civilian=item["civilian"],
                undercover=item["undercover"],
                tags=tuple(item.get("tags", []) or []),
            )
            pairs.append(p)
            for t in p.tags:
                by_tag.setdefault(t, []).append(p)
        self._pairs = pairs
        self._by_tag = by_tag


# Process-wide singleton.
_LIBRARY: WordpairLibrary | None = None


def get_wordpairs() -> WordpairLibrary:
    global _LIBRARY
    if _LIBRARY is None:
        _LIBRARY = WordpairLibrary()
    return _LIBRARY
