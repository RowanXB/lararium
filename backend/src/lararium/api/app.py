import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from psycopg.errors import UniqueViolation
from sqlalchemy.exc import IntegrityError

from lararium import __version__
from lararium.api import core_routes
from lararium.config import Settings, get_settings
from lararium.db import create_engine, make_sessionmaker
from lararium.modules.registry import MODULES

logger = logging.getLogger(__name__)


def _operation_id(route: APIRoute) -> str:
    # The route name is the operationId, so generated client function names stay stable.
    return route.name


async def _constraint_violation(request: Request, exc: Exception) -> JSONResponse:
    """A write that breaks a database constraint is the request's fault, never a 500.

    Domain code checks the rules it knows about first, with clearer errors; this covers a
    constraint without such a check, so that gap shows as a 4xx naming the constraint.
    """
    assert isinstance(exc, IntegrityError)
    constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
    logger.warning("request broke database constraint %s: %s", constraint, exc.orig)
    if isinstance(exc.orig, UniqueViolation):
        status_code = status.HTTP_409_CONFLICT
    else:
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    return JSONResponse(
        {"detail": {"error": "constraint_violation", "constraint": constraint}}, status_code
    )


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
    app.add_exception_handler(IntegrityError, _constraint_violation)
    app.include_router(core_routes.public_router)
    app.include_router(core_routes.router)
    for module in MODULES:
        module.install(app)
    return app
