from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession
from testcontainers.community.postgres import PostgresContainer

from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.db import create_engine

BACKEND = Path(__file__).parents[1]
INFRA_POSTGRES = BACKEND.parent / "infra" / "postgres"


@pytest.fixture(scope="session")
def postgres() -> Iterator[PostgresContainer]:
    container = PostgresContainer("postgres:18").with_volume_mapping(
        str(INFRA_POSTGRES), "/lararium", mode="ro"
    )
    with container:
        result = container.exec(
            [
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-v",
                "owner_pw=owner",
                "-v",
                "backend_pw=backend",
                "-v",
                "webhook_in_pw=webhook_in",
                "-U",
                container.username,
                "-f",
                "/lararium/roles.sql",
            ]
        )
        assert result.exit_code == 0, result.output.decode()
        yield container


def _url(container: PostgresContainer, user: str, password: str) -> str:
    host = container.get_container_host_ip()
    port = container.get_exposed_port(5432)
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/lararium"


@pytest.fixture(scope="session")
def owner_url(postgres: PostgresContainer) -> str:
    return _url(postgres, "lararium_owner", "owner")


@pytest.fixture(scope="session")
def backend_url(postgres: PostgresContainer) -> str:
    return _url(postgres, "lararium_backend", "backend")


@pytest.fixture(scope="session")
def webhook_in_url(postgres: PostgresContainer) -> str:
    return _url(postgres, "lararium_webhook_in", "webhook_in")


@pytest.fixture(scope="session")
def migrated(owner_url: str) -> None:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", owner_url)
    command.upgrade(config, "head")


@pytest.fixture
async def session(backend_url: str, migrated: None) -> AsyncIterator[AsyncSession]:
    engine = create_engine(backend_url)
    try:
        async with engine.connect() as conn:
            outer = await conn.begin()
            db_session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
            try:
                yield db_session
            finally:
                await db_session.close()
                await outer.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
def actor() -> Actor:
    return Actor("user", "test")


type Run = Callable[..., Awaitable[Any]]


@pytest.fixture
def run(session: AsyncSession, actor: Actor) -> Run:
    async def _run(fn: Callable[..., Awaitable[Any]], /, *args: Any, **kwargs: Any) -> Any:
        async with command_context(session, actor, "test", {}) as ctx:
            return await fn(ctx, *args, **kwargs)

    return _run
