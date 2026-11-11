from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.routing import APIRoute

from lararium import __version__
from lararium.api import core_routes
from lararium.config import Settings, get_settings
from lararium.db import create_engine, make_sessionmaker
from lararium.modules.registry import MODULES


def _operation_id(route: APIRoute) -> str:
    # The route name is the operationId, so generated client function names stay stable.
    return route.name


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
    app.include_router(core_routes.public_router)
    app.include_router(core_routes.router)
    for module in MODULES:
        module.install(app)
    return app
