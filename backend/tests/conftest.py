from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.docker_client import get_docker_host

from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.db import create_engine

from .containers import SSH, Endpoint, running

BACKEND = Path(__file__).parents[1]
INFRA_POSTGRES = BACKEND.parent / "infra" / "postgres"


def pytest_report_header() -> str:
    # The only sign of which daemon the containers run on: on a Mac whose docker context is
    # Docker Desktop, a plain `make check` runs there, as CI's does on the runner's Docker.
    mode = "ssh forward" if SSH else "local"
    return f"docker: {get_docker_host() or 'default local socket'} ({mode})"


@pytest.fixture(scope="session")
def postgres() -> Iterator[Endpoint]:
    def build() -> PostgresContainer:
        return PostgresContainer("postgres:18").with_copy_into_container(
            INFRA_POSTGRES / "roles.sql", "/lararium/roles.sql"
        )

    with running(build) as (container, endpoint):
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
        yield endpoint


def _url(endpoint: Endpoint, user: str, password: str) -> str:
    return f"postgresql+psycopg://{user}:{password}@{endpoint.host}:{endpoint.port}/lararium"


@pytest.fixture(scope="session")
def owner_url(postgres: Endpoint) -> str:
    return _url(postgres, "lararium_owner", "owner")


@pytest.fixture(scope="session")
def backend_url(postgres: Endpoint) -> str:
    return _url(postgres, "lararium_backend", "backend")


@pytest.fixture(scope="session")
def webhook_in_url(postgres: Endpoint) -> str:
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
