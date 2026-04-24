"""Shared pytest fixtures.

Each test module gets a fresh SQLite file under tmp_path/, the FastAPI
app re-bound to that DB, and an httpx AsyncClient hitting the in-process
ASGI app — no real port, no real network.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest.fixture(scope="function", autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Point the app at a tmp sqlite file before any module loads its
    settings / engine. We rebuild the singleton-ish modules in the
    httpx_client fixture so each test starts from a virgin schema."""
    db_file = tmp_path / "clawvert_test.db"
    monkeypatch.setenv("CLAWVERT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    # Disable the janitor lifespan task to keep tests deterministic.
    monkeypatch.setenv("CLAWVERT_JANITOR_INTERVAL_SEC", "60")
    yield


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    # Important: import lazily so each test's monkeypatched env wins.
    from app.core import config as config_mod
    from app.core import db as db_mod
    config_mod.get_settings.cache_clear()  # type: ignore[attr-defined]

    settings = config_mod.get_settings()
    db_path = settings.database_url.split("///", 1)[-1]
    parent = os.path.dirname(db_path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)

    # Rebuild engine against the new DB URL.
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    new_engine = create_async_engine(settings.database_url, echo=False, future=True,
                                     connect_args={"check_same_thread": False})
    db_mod.engine = new_engine
    db_mod.async_session_maker = async_sessionmaker(
        new_engine, expire_on_commit=False)

    # Re-import app AFTER db rebind so its lifespan picks up new engine.
    import importlib

    import app.main as main_mod
    importlib.reload(main_mod)
    app = main_mod.app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        async with app.router.lifespan_context(app):  # type: ignore[arg-type]
            yield c

    await new_engine.dispose()
