"""/api/v1/connections: the Trading 212 connection routes."""

import base64
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from procrastinate.testing import InMemoryConnector
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lararium.api.app import create_app
from lararium.config import Settings
from lararium.core.connections import get_connection, load_credential, set_status
from lararium.deps import get_session, get_session_factory
from lararium.modules.finance import connections_api
from lararium.worker import jobs
from lararium.worker.app import procrastinate_app

KEY = bytes(range(32))
OWNER = {"Tailscale-User-Login": "me@example.com"}
API_KEY = "t212-KEY-TEST-do-not-log"
API_SECRET = "t212-SECRET-TEST-do-not-log"
NEW_KEY = "t212-KEY-TEST-second"
NEW_SECRET = "t212-SECRET-TEST-second"
BODY = {
    "display_name": "Trading 212 Invest",
    "account_kind": "invest",
    "api_key": API_KEY,
    "api_secret": API_SECRET,
}
SUMMARY = '{"id": 12345678, "currency": "GBP", "totalValue": 10.5}'
URL = "/api/v1/connections/trading212"


class FakeTrading212:
    """The account/summary answer; every request is kept."""

    def __init__(self) -> None:
        self.status = 200
        self.body = SUMMARY
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, text=self.body)


@pytest.fixture
def trading212() -> FakeTrading212:
    return FakeTrading212()


@pytest.fixture
def sessions(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=session.bind, join_transaction_mode="create_savepoint")


@pytest.fixture
def queue() -> Any:
    with procrastinate_app.replace_connector(InMemoryConnector()) as app:
        yield app.connector


@pytest.fixture
async def api(session, sessions, trading212, queue) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        database_url="postgresql+psycopg://unused@localhost/unused",
        auth_mode="tailscale",
        owner_login="me@example.com",
        master_key=SecretStr(base64.b64encode(KEY).decode()),
    )
    app = create_app(settings)

    async def _test_session() -> AsyncIterator[AsyncSession]:
        yield session

    async def _test_http() -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(trading212)) as http:
            yield http

    app.dependency_overrides[get_session] = _test_session
    app.dependency_overrides[get_session_factory] = lambda: sessions
    app.dependency_overrides[connections_api.get_http] = _test_http
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _create(api: httpx.AsyncClient, **overrides: Any) -> dict[str, Any]:
    response = await api.post(URL, json=BODY | overrides, headers=OWNER)
    assert response.status_code == 201, response.text
    return response.json()


async def _logged(session: AsyncSession) -> str:
    logged = await session.scalar(
        text(
            "select coalesce((select string_agg(args::text || changes::text, ' ')"
            " from core.audit_log), '')"
            " || coalesce((select string_agg(data::text, ' ') from core.events), '')"
        )
    )
    return logged or ""


def _queued(queue) -> list[str]:
    return [job["task_name"] for job in queue.jobs.values()]


async def test_create_hides_keys(api, session, queue, trading212) -> None:
    created = await _create(api)

    assert created["connector"] == "trading212"
    assert created["status"] == "healthy"
    for secret in (API_KEY, API_SECRET):
        assert secret not in str(created)
        assert secret not in await _logged(session)
    args = await session.scalar(
        text("select args from core.audit_log where command = 'create_trading212_connection'")
    )
    assert args == {"display_name": "Trading 212 Invest", "account_kind": "invest"}
    key = await load_credential(session, KEY, created["id"], "api_key")
    secret = await load_credential(session, KEY, created["id"], "api_secret")
    assert key is not None and key.reveal() == API_KEY
    assert secret is not None and secret.reveal() == API_SECRET
    record = await get_connection(session, created["id"])
    assert record.config == {"account_kind": "invest"}
    # Where the sync's account check looks.
    assert record.cursor == {"account_id": "12345678"}
    assert _queued(queue) == ["sync_connection"]
    assert [request.method for request in trading212.requests] == ["GET"]


async def test_same_account_twice_409(api, queue) -> None:
    await _create(api)
    queue.jobs.clear()

    response = await api.post(URL, json=BODY | {"account_kind": "isa"}, headers=OWNER)

    assert response.status_code == 409
    assert "already connected" in response.json()["detail"]["message"]
    assert API_KEY not in response.text
    assert _queued(queue) == []


async def test_bad_key_422(api, session, trading212, queue) -> None:
    trading212.status = 401

    response = await api.post(URL, json=BODY, headers=OWNER)

    assert response.status_code == 422
    assert response.json()["detail"]["message"] == "Trading 212 rejected this key"
    assert API_KEY not in response.text and API_SECRET not in response.text
    assert await session.scalar(text("select count(*) from core.connections")) == 0
    assert _queued(queue) == []


@pytest.mark.parametrize("status", [403, 500])
async def test_other_errors_422_with_a_fixed_message(api, trading212, status) -> None:
    trading212.status = status
    trading212.body = f"boom {API_KEY}"

    response = await api.post(URL, json=BODY, headers=OWNER)

    assert response.status_code == 422
    message = response.json()["detail"]["message"]
    assert message.startswith("Trading 212 could not check this key")
    assert API_KEY not in response.text


async def test_summary_without_an_id_422(api, trading212) -> None:
    trading212.body = '{"currency": "GBP"}'

    response = await api.post(URL, json=BODY, headers=OWNER)

    assert response.status_code == 422
    assert "account id" in response.json()["detail"]["message"]


async def test_account_kind_must_be_known(api) -> None:
    response = await api.post(URL, json=BODY | {"account_kind": "cfd"}, headers=OWNER)
    assert response.status_code == 422


async def test_update_from_needs_reauth(api, session, run, queue) -> None:
    created = await _create(api)
    await run(set_status, created["id"], "needs_reauth", error="Trading 212 rejected the key")
    queue.jobs.clear()

    response = await api.put(
        f"/api/v1/connections/{created['id']}/trading212",
        json={"api_key": NEW_KEY, "api_secret": NEW_SECRET},
        headers=OWNER,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["last_error"]) == ("healthy", None)
    assert NEW_KEY not in response.text and NEW_SECRET not in response.text
    key = await load_credential(session, KEY, created["id"], "api_key")
    secret = await load_credential(session, KEY, created["id"], "api_secret")
    assert key is not None and key.reveal() == NEW_KEY
    assert secret is not None and secret.reveal() == NEW_SECRET
    logged = await _logged(session)
    assert NEW_KEY not in logged and NEW_SECRET not in logged
    record = await get_connection(session, created["id"])
    assert record.config == {"account_kind": "invest"}
    assert _queued(queue) == ["sync_connection"]


async def test_update_takes_a_disabled_connection_back_once_its_keys_pass(
    api, session, run, trading212, queue
) -> None:
    """Update keys is how a disabled connection comes back: the new keys pass the checks of
    a create (Trading 212 accepts them, for the same account), and it is healthy again."""
    created = await _create(api)
    await run(set_status, created["id"], "disabled")
    keys = {"api_key": NEW_KEY, "api_secret": NEW_SECRET}
    url = f"/api/v1/connections/{created['id']}/trading212"

    trading212.status = 401
    refused = await api.put(url, json=keys, headers=OWNER)
    assert refused.status_code == 422
    assert refused.json()["detail"]["message"] == "Trading 212 rejected this key"
    assert (await get_connection(session, created["id"])).status == "disabled"

    trading212.status = 200
    trading212.body = SUMMARY.replace("12345678", "12345679")
    other = await api.put(url, json=keys, headers=OWNER)
    assert other.status_code == 422
    assert other.json()["detail"]["message"] == "these keys are another Trading 212 account's"
    key = await load_credential(session, KEY, created["id"], "api_key")
    assert key is not None and key.reveal() != NEW_KEY

    trading212.body = SUMMARY
    queue.jobs.clear()
    response = await api.put(url, json=keys, headers=OWNER)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "healthy"
    key = await load_credential(session, KEY, created["id"], "api_key")
    assert key is not None and key.reveal() == NEW_KEY
    assert _queued(queue) == ["sync_connection"]


async def test_update_refuses_other_connectors(api, run) -> None:

    other = await api.post(
        "/api/v1/connections/ibkr",
        json={
            "display_name": "I",
            "token": "tok-TEST",
            "token_expires_on": "2099-01-01",
            "query_id": "1",
        },
        headers=OWNER,
    )
    assert other.status_code == 201, other.text
    response = await api.put(
        f"/api/v1/connections/{other.json()['id']}/trading212",
        json={"api_key": NEW_KEY, "api_secret": NEW_SECRET},
        headers=OWNER,
    )
    assert response.status_code == 422


async def test_failed_create_leaves_nothing_behind(api, session, queue, monkeypatch) -> None:
    stored = 0
    real = connections_api.store_credential

    async def _fail_on_the_second(*args: Any, **kwargs: Any) -> Any:
        nonlocal stored
        stored += 1
        if stored == 2:
            raise RuntimeError("boom")
        return await real(*args, **kwargs)

    monkeypatch.setattr(connections_api, "store_credential", _fail_on_the_second)

    with pytest.raises(RuntimeError):
        await api.post(URL, json=BODY, headers=OWNER)

    assert stored == 2
    assert await session.scalar(text("select count(*) from core.connections")) == 0
    assert await session.scalar(text("select count(*) from core.credentials")) == 0
    assert _queued(queue) == []


class _Notes:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        self.messages.append(message)


async def test_update_with_another_accounts_keys_fails_the_next_sync(
    api, session, sessions, trading212
) -> None:
    created = await _create(api)
    response = await api.put(
        f"/api/v1/connections/{created['id']}/trading212",
        json={"api_key": NEW_KEY, "api_secret": NEW_SECRET},
        headers=OWNER,
    )
    assert response.status_code == 200, response.text
    # The new keys belong to another account.
    trading212.body = '{"id": 99999999, "currency": "GBP"}'

    settings = Settings(
        database_url="postgresql+psycopg://unused@localhost/unused",
        auth_mode="dev",
        master_key=SecretStr(base64.b64encode(KEY).decode()),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(trading212)) as http:
        await jobs.run_sync(
            session,
            UUID(created["id"]),
            settings=settings,
            http=http,
            notifier=_Notes(),
            session_factory=sessions,
        )

    record = await get_connection(session, created["id"])
    assert record.status == "needs_reauth"
    assert record.cursor == {"account_id": "12345678"}


async def test_too_long_keys_are_not_echoed(api) -> None:
    key, secret = "k" * 501, "s" * 501
    response = await api.post(
        URL, json=BODY | {"api_key": key, "api_secret": secret}, headers=OWNER
    )
    assert response.status_code == 422
    assert key not in response.text and secret not in response.text

    updated = await api.put(
        f"/api/v1/connections/{uuid4()}/trading212",
        json={"api_key": key, "api_secret": secret},
        headers=OWNER,
    )
    assert updated.status_code == 422
    assert key not in updated.text and secret not in updated.text
