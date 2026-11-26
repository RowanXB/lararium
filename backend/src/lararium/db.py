from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Bounds how long one request can hold a connection, whatever query it runs; a cancelled
# statement reaches the client as a 503 (see lararium.api.app).
STATEMENT_TIMEOUT = "30s"
# JIT compiles a plan whose estimated cost passes jit_above_cost. The ledger's queries are
# short, and on a large ledger with no statistics (a fresh database) their estimates
# soar: heal's scan of the own records ran in 0.3 s and spent 2.4 s compiling.
JIT = "off"


def psycopg_dsn(database_url: str) -> str:
    """The SQLAlchemy URL as a plain libpq DSN, for code that uses psycopg directly."""
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def create_engine(url: str) -> AsyncEngine:
    return create_async_engine(
        url, connect_args={"options": f"-c statement_timeout={STATEMENT_TIMEOUT} -c jit={JIT}"}
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
