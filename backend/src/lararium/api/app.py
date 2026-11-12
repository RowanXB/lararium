import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from psycopg.errors import QueryCanceled, UniqueViolation
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError, OperationalError

from lararium import __version__
from lararium.api import core_routes
from lararium.config import Settings, get_settings
from lararium.db import create_engine, make_sessionmaker
from lararium.modules.registry import MODULES

logger = logging.getLogger(__name__)


def _operation_id(route: APIRoute) -> str:
    # The route name is the operationId, so generated client function names stay stable.
    return route.name


def _database_error_response(exc: DBAPIError) -> tuple[int, str, str, str | None]:
    """(status, error code, message, constraint) for a database error a request caused."""
    if isinstance(exc, IntegrityError):
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        status_code = (
            status.HTTP_409_CONFLICT
            if isinstance(exc.orig, UniqueViolation)
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        rule = f" {constraint}" if constraint else ""
        message = f"the change breaks the database rule{rule}"
        return status_code, "constraint_violation", message, constraint
    if isinstance(exc, DataError):
        message = "a value is malformed or out of range for the database"
        return status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_value", message, None
    if isinstance(exc.orig, QueryCanceled):
        message = "the request took too long and was stopped"
        return status.HTTP_503_SERVICE_UNAVAILABLE, "query_timeout", message, None
    message = "the database is unavailable"
    return status.HTTP_503_SERVICE_UNAVAILABLE, "database_unavailable", message, None


async def _database_error(request: Request, exc: Exception) -> JSONResponse:
    """A database error the request caused is never a 500.

    Domain code checks the rules and limits it knows about first, with clearer errors; this
    covers one without such a check (a constraint, a value limit, the statement timeout).
    Only the error's class and constraint are logged: its text can quote stored values.
    """
    assert isinstance(exc, DBAPIError)
    status_code, error, message, constraint = _database_error_response(exc)
    logger.warning(
        "database error %s (%s) on %s", type(exc.orig).__name__, constraint, request.url.path
    )
    detail: dict[str, str | None] = {"error": error, "message": message}
    if isinstance(exc, IntegrityError):
        detail["constraint"] = constraint
    return JSONResponse({"detail": detail}, status_code)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = create_engine(settings.database_url)  # connects lazily, on first use

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        yield
        await engine.dispose()

    app = FastAPI(
        title="Lararium",
        version=__version__,
        lifespan=lifespan,
        generate_unique_id_function=_operation_id,
    )
    # Read by lararium.deps.
    app.state.settings = settings
    app.state.sessionmaker = make_sessionmaker(engine)
    for error_class in (IntegrityError, DataError, OperationalError):
        app.add_exception_handler(error_class, _database_error)
    app.include_router(core_routes.public_router)
    app.include_router(core_routes.router)
    for module in MODULES:
        module.install(app)
    return app
