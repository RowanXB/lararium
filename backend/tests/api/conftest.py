import base64
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.api.app import create_app
from lararium.config import Settings

from .overrides import use_session

# Never connected to: get_session is overridden to yield the test session.
UNUSED_DATABASE_URL = "postgresql+psycopg://unused@localhost/unused"


async def _client(settings: Settings, session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)

    use_session(app, session)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def client(session: AsyncSession, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(database_url=UNUSED_DATABASE_URL, auth_mode="dev", document_dir=tmp_path)
    async for c in _client(settings, session):
        yield c


@pytest.fixture
async def tailscale_client(
    session: AsyncSession, tmp_path: Path
) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        database_url=UNUSED_DATABASE_URL,
        document_dir=tmp_path,
        auth_mode="tailscale",
        owner_login="me@example.com",
        master_key=base64.b64encode(bytes(range(32))).decode(),
    )
    async for c in _client(settings, session):
        yield c
