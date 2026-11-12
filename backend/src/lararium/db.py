from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Bounds how long one request can hold a connection, whatever query it runs; a cancelled
# statement reaches the client as a 503 (see lararium.api.app).
STATEMENT_TIMEOUT = "30s"


def create_engine(url: str) -> AsyncEngine:
    return create_async_engine(
        url, connect_args={"options": f"-c statement_timeout={STATEMENT_TIMEOUT}"}
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
