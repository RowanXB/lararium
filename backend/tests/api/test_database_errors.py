"""A database error the request causes is the client's error, never a 500.

Domain code checks the rules it knows about first, with clearer messages; this is the
backstop for a constraint or value limit that has no such check.
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

STATEMENTS = {
    "unique": ["INSERT INTO core.settings (key, value) VALUES ('dup', '1'), ('dup', '2')"],
    "check": [
        "INSERT INTO finance.instruments (kind, symbol, name, decimals, owner_entity_id)"
        " SELECT 'nope', 'X', 'X', 2, id FROM core.entities WHERE is_self"
    ],
    "foreign_key": [
        "INSERT INTO finance.instrument_identifiers (instrument_id, scheme, value)"
        " VALUES ('01900000-0000-7000-8000-000000000000', 'isin', 'X')"
    ],
    "data": ["SELECT 'not a number'::numeric"],
    "timeout": ["SET LOCAL statement_timeout = '1ms'", "SELECT pg_sleep(1)"],
}


@pytest.fixture
async def failing_client(session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(Settings(database_url=UNUSED_DATABASE_URL, auth_mode="dev"))

    @app.post("/fail/{kind}")
    async def fail(kind: str, db: SessionDep) -> None:  # pyright: ignore[reportUnusedFunction]
        async with db.begin_nested():
            for statement in STATEMENTS[kind]:
                await db.execute(text(statement))

    async def _test_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.parametrize(
    ("kind", "status", "error"),
    [
        ("unique", 409, "constraint_violation"),
        ("check", 422, "constraint_violation"),
        ("foreign_key", 422, "constraint_violation"),
        ("data", 422, "invalid_value"),
        ("timeout", 503, "query_timeout"),
    ],
)
async def test_database_error_is_not_a_500(failing_client, kind, status, error) -> None:
    response = await failing_client.post(f"/fail/{kind}")
    assert response.status_code == status
    detail = response.json()["detail"]
    assert detail["error"] == error
    assert detail["message"]


async def test_constraint_violation_names_the_constraint(failing_client) -> None:
    detail = (await failing_client.post("/fail/check")).json()["detail"]
    assert detail["constraint"] == "instruments_kind_check"
