"""Request-scoped dependencies, wired to resources owned by the app lifespan."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.cache import Cache
from app.core.config import Settings
from app.ingestion.poller import Poller


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_poller(request: Request) -> Poller:
    poller: Poller = request.app.state.poller
    return poller


def get_cache(request: Request) -> Cache:
    cache: Cache = request.app.state.cache
    return cache


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        yield session
