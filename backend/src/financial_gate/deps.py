"""Request dependencies shared by the API layer and the modules' routes.

They live outside `financial_gate.api` because modules may not import the API package
(import-linter contract); `create_app` puts what they need on `app.state`.
"""

from collections.abc import AsyncIterator
from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financial_gate.config import Settings
from financial_gate.core.actors import Actor

OWNER_HEADER = "Tailscale-User-Login"


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    sessionmaker = cast(async_sessionmaker[AsyncSession], request.app.state.sessionmaker)
    async with sessionmaker() as session:
        yield session


def get_actor(request: Request) -> Actor:
    settings = cast(Settings, request.app.state.settings)
    if settings.auth_mode == "dev":
        return Actor("user", "dev")
    login = request.headers.get(OWNER_HEADER)
    if login is None or login != settings.owner_login:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown or missing user")
    return Actor("user", login)


SessionDep = Annotated[AsyncSession, Depends(get_session)]
ActorDep = Annotated[Actor, Depends(get_actor)]
