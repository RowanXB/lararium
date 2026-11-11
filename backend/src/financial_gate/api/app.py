from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.routing import APIRoute

from financial_gate import __version__
from financial_gate.api import core_routes
from financial_gate.config import Settings, get_settings
from financial_gate.db import create_engine, make_sessionmaker
from financial_gate.modules.registry import MODULES


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
        title="financial-gate",
        version=__version__,
        lifespan=lifespan,
        generate_unique_id_function=_operation_id,
    )
    # Read by financial_gate.deps.
    app.state.settings = settings
    app.state.sessionmaker = make_sessionmaker(engine)
    app.include_router(core_routes.public_router)
    app.include_router(core_routes.router)
    for module in MODULES:
        module.install(app)
    return app
