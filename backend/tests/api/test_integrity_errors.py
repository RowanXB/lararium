"""Any database constraint the request breaks is the client's error, never a 500.

Domain code checks the rules it knows about first, with clearer messages; this is the
backstop for a constraint that has no such check.
"""

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.api.app import create_app
from lararium.config import Settings
from lararium.deps import SessionDep, get_session

UNUSED_DATABASE_URL = "postgresql+psycopg://unused@localhost/unused"  # never connected to

VIOLATIONS = {
    "unique": "INSERT INTO core.settings (key, value) VALUES ('dup', '1'), ('dup', '2')",
    "check": "INSERT INTO finance.instruments (kind, symbol, name, decimals, owner_entity_id)"
    " SELECT 'nope', 'X', 'X', 2, id FROM core.entities WHERE is_self",
    "foreign_key": "INSERT INTO finance.instrument_identifiers (instrument_id, scheme, value)"
    " VALUES ('01900000-0000-7000-8000-000000000000', 'isin', 'X')",
}


@pytest.fixture
async def violating_client(session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(Settings(database_url=UNUSED_DATABASE_URL, auth_mode="dev"))

    @app.post("/violate/{kind}")
    async def violate(kind: str, db: SessionDep) -> None:  # pyright: ignore[reportUnusedFunction]
        async with db.begin_nested():
            await db.execute(text(VIOLATIONS[kind]))

    async def _test_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.parametrize(
    ("kind", "status"), [("unique", 409), ("check", 422), ("foreign_key", 422)]
)
async def test_constraint_violation_is_a_client_error(violating_client, kind, status) -> None:
    response = await violating_client.post(f"/violate/{kind}")
    assert response.status_code == status
    detail = response.json()["detail"]
    assert detail["error"] == "constraint_violation"
    assert detail["constraint"]
