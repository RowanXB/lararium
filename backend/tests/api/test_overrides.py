import gc
import weakref
from typing import Any, cast

import httpx

from lararium.api.app import create_app
from lararium.config import Settings
from lararium.deps import SessionDep

from .conftest import UNUSED_DATABASE_URL
from .overrides import use_session


class _Session:
    pass


async def test_a_served_request_leaves_nothing_holding_the_test_session() -> None:
    # What is held here is held for the rest of the run: the session, and through it the
    # connection, the engine and its compiled statements. Over a whole run that came to
    # millions of objects and collector pauses of seconds.
    app = create_app(Settings(database_url=UNUSED_DATABASE_URL, auth_mode="dev"))
    seen: list[bool] = []

    @app.get("/probe")
    async def probe(db: SessionDep) -> None:  # pyright: ignore[reportUnusedFunction]
        seen.append(isinstance(db, _Session))

    session = _Session()
    use_session(app, cast(Any, session))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/probe")).is_success
    assert seen == [True]

    gone = weakref.ref(session)
    del session, app, transport, client, probe
    gc.collect()
    assert gone() is None
