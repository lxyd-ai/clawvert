from __future__ import annotations

import os
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()

if _settings.database_url.startswith("sqlite"):
    db_path = _settings.database_url.split("///", 1)[-1]
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

engine = create_async_engine(
    _settings.database_url,
    echo=False,
    future=True,
    connect_args=(
        {"check_same_thread": False}
        if _settings.database_url.startswith("sqlite")
        else {}
    ),
)

async_session_maker: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with async_session_maker() as session:
        yield session


async def init_db() -> None:
    """Create all tables. We don't use Alembic; idempotent ALTERs in
    a `migrations` list below carry us through schema evolution until
    the project warrants real migrations."""
    # Import models so Base.metadata is populated. (Filled in 3B.)
    try:
        from app.models import agent, match, owner  # noqa: F401
    except ImportError:
        # 3A skeleton: models not yet present, still allow startup.
        pass

    from sqlalchemy import text

    async with engine.begin() as conn:
        if _settings.database_url.startswith("sqlite"):
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA synchronous=NORMAL"))
        await conn.run_sync(Base.metadata.create_all)

        migrations: list[str] = [
            # populated as schema evolves; each statement is idempotent
        ]
        for stmt in migrations:
            try:
                await conn.execute(text(stmt))
            except Exception:  # noqa: BLE001
                pass
