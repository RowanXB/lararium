"""Request dependencies shared by the API layer and the modules' routes.

They live outside `financial_gate.api` because modules may not import the API package
(import-linter contract); `create_app` puts what they need on `app.state`.
"""

from collections.abc import AsyncIterator
from typing import Annotated, cast
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from financial_gate.config import Settings
from financial_gate.core.actors import Actor

OWNER_HEADER = "Tailscale-User-Login"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    sessionmaker = cast(async_sessionmaker[AsyncSession], request.app.state.sessionmaker)
    async with sessionmaker() as session:
        yield session


def _is_cross_site(request: Request) -> bool:
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site not in ("same-origin", "none"):
        return True
    origin = request.headers.get("origin")
    if origin is None:
        return False
    host = request.headers.get("host", "")
    return urlsplit(origin).netloc.lower() != host.lower()


def get_actor(request: Request) -> Actor:
    # The owner's browser attaches the identity header to every request it sends, even one
    # a page on another site triggers, so a write must come from our own origin. Requests
    # without these headers (curl, scripts) don't come from a browser page and pass.
    if request.method not in SAFE_METHODS and _is_cross_site(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "cross-site request refused")
    settings = cast(Settings, request.app.state.settings)
    if settings.auth_mode == "dev":
        return Actor("user", "dev")
    login = request.headers.get(OWNER_HEADER)
    if login is None or login != settings.owner_login:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown or missing user")
    return Actor("user", login)


SessionDep = Annotated[AsyncSession, Depends(get_session)]
ActorDep = Annotated[Actor, Depends(get_actor)]
