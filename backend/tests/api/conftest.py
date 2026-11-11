from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from financial_gate.api.app import create_app
from financial_gate.config import Settings
from financial_gate.deps import get_session

# Never connected to: get_session is overridden to yield the test session.
UNUSED_DATABASE_URL = "postgresql+psycopg://unused@localhost/unused"


async def _client(settings: Settings, session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)

    async def _test_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(database_url=UNUSED_DATABASE_URL, auth_mode="dev")
    async for c in _client(settings, session):
        yield c


@pytest.fixture
async def tailscale_client(session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        database_url=UNUSED_DATABASE_URL, auth_mode="tailscale", owner_login="me@example.com"
    )
    async for c in _client(settings, session):
        yield c
