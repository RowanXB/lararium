"""Pointing an app under test at the test's database session.

FastAPI remembers what kind of callable each dependency is in caches that live as long as
the process, keyed by the callable itself. An override defined inside a fixture is a new
function per test, and closing over the test's session it would keep that session, its
connection, its engine and every statement the engine compiled alive until the run ends.
So the overrides here are module-level functions, and what a test gives them travels on
`app.state`, which dies with the app.
"""

from collections.abc import AsyncIterator
from typing import cast

from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lararium.deps import get_session


async def _test_session(request: Request) -> AsyncIterator[AsyncSession]:
    yield cast(AsyncSession, request.app.state.test_session)


def use_session(
    app: FastAPI,
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Every request gets `session`; `sessions` replaces the factory behind get_session_factory."""
    app.state.test_session = session
    app.dependency_overrides[get_session] = _test_session
    if sessions is not None:
        app.state.sessionmaker = sessions
