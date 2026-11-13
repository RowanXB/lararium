"""/api/v1/connections, the Monzo OAuth callback and the approval window (Decision 7)."""

import base64
import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from procrastinate.testing import InMemoryConnector
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lararium.api.app import create_app
from lararium.config import Settings
from lararium.core.connections import (
    create_connection,
    get_connection,
    load_credential,
    set_status,
    update_config,
)
from lararium.deps import get_session, get_session_factory
from lararium.modules.finance import connections_api
from lararium.worker import jobs
from lararium.worker.app import procrastinate_app

from ..finance.monzo.fixtures import make_account, make_balance, make_pot, make_transaction

KEY = bytes(range(32))
OWNER = {"Tailscale-User-Login": "me@example.com"}
BASE_URL = "https://lararium.example"
REDIRECT_URI = f"{BASE_URL}/api/v1/connections/oauth/monzo/callback"
CALLBACK = "/api/v1/connections/oauth/monzo/callback"
CLIENT_SECRET = "cs-TEST-do-not-log"
NAVIGATION = {
    "Sec-Fetch-Site": "cross-site",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
}
EXPIRED = "Approve the Monzo request in the app within 4 minutes, then authorise again"


def _settings(**overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "database_url": "postgresql+psycopg://unused@localhost/unused",
        "auth_mode": "tailscale",
        "owner_login": "me@example.com",
        "master_key": SecretStr(base64.b64encode(KEY).decode()),
        "public_base_url": BASE_URL,
    }
    return Settings(**(fields | overrides))


class FakeMonzo:
    """Monzo's token endpoint and API; /accounts answers 403 until `approve_after` calls."""

    def __init__(self) -> None:
        self.approve_after: int | None = 0
        self.transactions_fail = False
        self.token_forms: list[dict[str, list[str]]] = []
        self.requests: list[httpx.Request] = []
        now = datetime.now(UTC)
        self.account = make_account(created=_iso(now - timedelta(days=30)))
        self.transactions = [
            make_transaction(id="tx_TEST0001", created=_iso(now - timedelta(days=29))),
            make_transaction(id="tx_TEST0002", created=_iso(now - timedelta(days=2))),
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/oauth2/token":
            self.token_forms.append(parse_qs(request.content.decode()))
            return httpx.Response(
                200,
                json={"access_token": "at-TEST", "refresh_token": "rt-TEST", "expires_in": 21600},
            )
        if path == "/accounts":
            polls = len(self.account_requests())
            if self.approve_after is None or polls <= self.approve_after:
                return httpx.Response(403, json={"code": "forbidden.insufficient_permissions"})
            return httpx.Response(200, json={"accounts": [self.account]})
        if path == "/balance":
            return httpx.Response(200, json=make_balance(balance=-2468))
        if path == "/pots":
            return httpx.Response(200, json={"pots": [make_pot()]})
        assert path == "/transactions", path
        if self.transactions_fail:
            return httpx.Response(500, json={"code": "internal_service"})
        return httpx.Response(200, json={"transactions": self.transactions})

    def account_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/accounts"]


def _iso(value: datetime) -> str:
    return f"{value:%Y-%m-%dT%H:%M:%S}.{value.microsecond // 1000:03d}Z"


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> None:
        self.messages.append(text)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000
        self.slept: list[int] = []

    def __call__(self) -> int:
        return self.now

    async def sleep(self, seconds: int) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def monzo() -> FakeMonzo:
    return FakeMonzo()


@pytest.fixture
def sessions(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    # Sessions of their own (save_tokens) still run inside the test's rolled-back transaction.
    return async_sessionmaker(bind=session.bind, join_transaction_mode="create_savepoint")


@pytest.fixture
def queue() -> Any:
    with procrastinate_app.replace_connector(InMemoryConnector()) as app:
        yield app.connector


async def _client(
    settings: Settings,
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    monzo: FakeMonzo,
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)

    async def _test_session() -> AsyncIterator[AsyncSession]:
        yield session

    async def _test_http() -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(monzo)) as http:
            yield http

    app.dependency_overrides[get_session] = _test_session
    app.dependency_overrides[get_session_factory] = lambda: sessions
    app.dependency_overrides[connections_api.get_http] = _test_http
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def api(session, sessions, monzo) -> AsyncIterator[httpx.AsyncClient]:
    async for client in _client(_settings(), session, sessions, monzo):
        yield client


async def _create(api: httpx.AsyncClient) -> dict[str, Any]:
    response = await api.post(
        "/api/v1/connections",
        json={
            "display_name": "My Monzo",
            "client_id": "oauth2client_TEST1",
            "client_secret": CLIENT_SECRET,
        },
        headers=OWNER,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _authorize(api: httpx.AsyncClient, connection_id: str) -> str:
    """Start authorising; return the state in the Monzo URL."""
    response = await api.post(f"/api/v1/connections/{connection_id}/authorize", headers=OWNER)
    assert response.status_code == 200, response.text
    return parse_qs(urlsplit(response.json()["url"]).query)["state"][0]


async def _callback(api: httpx.AsyncClient, state: str, **headers: str) -> httpx.Response:
    return await api.get(
        CALLBACK,
        params={"code": "code-TEST", "state": state},
        headers=OWNER | NAVIGATION | headers,
    )


async def _logged(session: AsyncSession) -> str:
    """Everything the audit log and the events hold, as text."""
    logged = await session.scalar(
        text(
            "select coalesce((select string_agg(args::text || changes::text, ' ')"
            " from core.audit_log), '')"
            " || coalesce((select string_agg(data::text, ' ') from core.events), '')"
        )
    )
    return logged or ""


async def test_create_connection_hides_secret(api, session) -> None:
    created = await _create(api)

    assert CLIENT_SECRET not in str(created)
    assert created["status"] == "created"
    assert created["connector"] == "monzo"
    assert created["redirect_uri"] == REDIRECT_URI
    assert created["webhook_enabled"] is False
    args = await session.scalar(
        text("select args from core.audit_log where command = 'create_monzo_connection'")
    )
    assert args == {"display_name": "My Monzo", "client_id": "oauth2client_TEST1"}
    assert CLIENT_SECRET not in await _logged(session)
    stored = await load_credential(session, KEY, created["id"], "client_secret")
    assert stored is not None and stored.reveal() == CLIENT_SECRET

    listed = await api.get("/api/v1/connections", headers=OWNER)
    assert listed.status_code == 200
    assert CLIENT_SECRET not in listed.text
    assert [c["id"] for c in listed.json()] == [created["id"]]
    assert set(listed.json()[0]) == {
        "id",
        "connector",
        "display_name",
        "status",
        "last_success_at",
        "last_error",
        "consecutive_failures",
        "credential_expires_at",
        "warnings",
        "redirect_uri",
        "webhook_enabled",
    }


async def test_webhook_enabled_when_base_url_and_secret_set(session, sessions, monzo) -> None:
    secret = "whsec-TEST-0123456789abcdef"
    settings = _settings(
        webhook_base_url="https://hooks.lararium.example", webhook_secret=SecretStr(secret)
    )
    async for api in _client(settings, session, sessions, monzo):
        created = await _create(api)
        listed = await api.get("/api/v1/connections", headers=OWNER)

    assert created["webhook_enabled"] is True
    assert listed.json()[0]["webhook_enabled"] is True
    assert secret not in str(created) + listed.text


async def test_authorize_returns_monzo_url_and_stores_state_hash(api, session) -> None:
    created = await _create(api)
    before = datetime.now(UTC)

    response = await api.post(f"/api/v1/connections/{created['id']}/authorize", headers=OWNER)

    assert response.status_code == 200
    url = urlsplit(response.json()["url"])
    assert (url.scheme, url.netloc, url.path) == ("https", "auth.monzo.com", "/")
    query = parse_qs(url.query)
    state = query.pop("state")[0]
    assert len(state) >= 32
    assert query == {
        "client_id": ["oauth2client_TEST1"],
        "redirect_uri": [REDIRECT_URI],
        "response_type": ["code"],
    }
    config = (await get_connection(session, created["id"])).config
    stored: Any = config["oauth_state"]
    assert stored["hash"] == hashlib.sha256(state.encode()).hexdigest()
    expires_at = datetime.fromisoformat(stored["expires_at"])
    assert before + timedelta(minutes=9) < expires_at <= datetime.now(UTC) + timedelta(minutes=10)
    assert state not in str(config)
    assert state not in await _logged(session)


async def test_config_writes_keep_the_account_map(api, session, run, queue) -> None:
    created = await _create(api)
    account_map = {"acc_TEST1": "01900000-0000-7000-8000-000000000001"}
    # As a sync that ran meanwhile would have written it.
    await run(update_config, created["id"], {"account_map": account_map})

    state = await _authorize(api, created["id"])
    assert (await get_connection(session, created["id"])).config["account_map"] == account_map
    assert (await _callback(api, state)).status_code == 303

    config = (await get_connection(session, created["id"])).config
    assert config == {"client_id": "oauth2client_TEST1", "account_map": account_map}


async def test_public_base_url_required(session, sessions, monzo, run) -> None:
    async for api in _client(_settings(public_base_url=None), session, sessions, monzo):
        response = await api.post(
            "/api/v1/connections",
            json={"display_name": "M", "client_id": "oauth2client_TEST1", "client_secret": "s"},
            headers=OWNER,
        )
        assert response.status_code == 422
        assert "LARARIUM_PUBLIC_BASE_URL" in response.json()["detail"]["message"]

        connection = await run(
            create_connection,
            connector="monzo",
            display_name="M",
            config={"client_id": "oauth2client_TEST1"},
        )
        response = await api.post(f"/api/v1/connections/{connection.id}/authorize", headers=OWNER)
        assert response.status_code == 422
        assert response.json()["detail"]["error"] == "not_configured"
        listed = await api.get("/api/v1/connections", headers=OWNER)
        assert listed.json()[0]["redirect_uri"] is None


async def test_callback_navigation_allowed_but_fetch_refused(api, queue) -> None:
    created = await _create(api)
    state = await _authorize(api, created["id"])

    # Only a top-level navigation may come from another site (auth.monzo.com).
    refused = [
        OWNER | NAVIGATION | {"Sec-Fetch-Mode": "cors"},
        OWNER | NAVIGATION | {"Sec-Fetch-Dest": "iframe"},
        OWNER | {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"},
    ]
    for headers in refused:
        response = await api.get(
            CALLBACK, params={"code": "code-TEST", "state": state}, headers=headers
        )
        assert response.status_code == 403, headers
    # The owner header is still required.
    response = await api.get(
        CALLBACK, params={"code": "code-TEST", "state": state}, headers=NAVIGATION
    )
    assert response.status_code == 401

    response = await _callback(api, state)
    assert response.status_code == 303
    assert response.headers["location"] == "/connections"


async def test_other_routes_still_refuse_cross_site_navigation(api) -> None:
    response = await api.get("/api/v1/connections", headers=OWNER | NAVIGATION)
    assert response.status_code == 403


async def test_callback_state_single_use_and_expiring(api, session, monzo, queue) -> None:
    created = await _create(api)
    state = await _authorize(api, created["id"])

    first = await _callback(api, state)
    again = await _callback(api, state)

    assert (first.status_code, first.headers["location"]) == (303, "/connections")
    assert (again.status_code, again.headers["location"]) == (303, "/connections?error=state")
    assert len(monzo.token_forms) == 1

    unknown = await _callback(api, "not-a-state")
    assert unknown.headers["location"] == "/connections?error=state"

    expiring = await _authorize(api, created["id"])
    await session.execute(
        text(
            "update core.connections set config = jsonb_set(config, '{oauth_state,expires_at}',"
            " to_jsonb(cast(:past as text))) where id = :id"
        ),
        {"past": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), "id": created["id"]},
    )
    expired = await _callback(api, expiring)
    assert expired.headers["location"] == "/connections?error=state"
    assert "oauth_state" not in (await get_connection(session, created["id"])).config
    assert len(monzo.token_forms) == 1


async def test_callback_exchanges_code_and_defers_approval(api, session, monzo, queue) -> None:
    created = await _create(api)
    state = await _authorize(api, created["id"])

    response = await _callback(api, state)

    assert response.status_code == 303
    assert monzo.token_forms == [
        {
            "grant_type": ["authorization_code"],
            "client_id": ["oauth2client_TEST1"],
            "client_secret": [CLIENT_SECRET],
            "redirect_uri": [REDIRECT_URI],
            "code": ["code-TEST"],
        }
    ]
    access = await load_credential(session, KEY, created["id"], "access_token")
    refresh = await load_credential(session, KEY, created["id"], "refresh_token")
    assert access is not None and access.reveal() == "at-TEST"
    assert refresh is not None and refresh.reveal() == "rt-TEST"
    assert (await get_connection(session, created["id"])).status == "authorising"
    saved_by = await session.scalar(
        text("select actor_type || ':' || actor_id from core.audit_log where command = :c"),
        {"c": "connection.save_tokens"},
    )
    assert saved_by == "user:me@example.com"

    queued = list(queue.jobs.values())
    assert [(j["task_name"], j["args"], j["lock"]) for j in queued] == [
        ("await_approval", {"connection_id": created["id"]}, f"sync:{created['id']}")
    ]
    logged = await _logged(session)
    for value in ("code-TEST", state, "at-TEST", "rt-TEST", CLIENT_SECRET):
        assert value not in logged


async def test_sync_now_and_disable(api, session, queue) -> None:
    created = await _create(api)

    synced = await api.post(f"/api/v1/connections/{created['id']}/sync", headers=OWNER)
    again = await api.post(f"/api/v1/connections/{created['id']}/sync", headers=OWNER)
    disabled = await api.post(f"/api/v1/connections/{created['id']}/disable", headers=OWNER)
    missing = await api.post(
        "/api/v1/connections/01900000-0000-7000-8000-000000000001/sync", headers=OWNER
    )

    assert (synced.status_code, synced.json()) == (202, {"queued": True})
    assert (again.status_code, again.json()) == (202, {"queued": False})
    queued = list(queue.jobs.values())
    assert [(j["task_name"], j["queueing_lock"]) for j in queued] == [
        ("sync_connection", f"sync:{created['id']}")
    ]
    assert (disabled.status_code, disabled.json()["status"]) == (200, "disabled")
    assert missing.status_code == 404
    assert missing.json()["detail"]["error"] == "connection_not_found"


async def _authorising(api: httpx.AsyncClient) -> str:
    created = await _create(api)
    state = await _authorize(api, created["id"])
    assert (await _callback(api, state)).status_code == 303
    return created["id"]


async def _await_approval(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    connection_id: str,
    monzo: FakeMonzo,
    notifier: RecordingNotifier,
    clock: FakeClock,
) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(monzo)) as http:
        await jobs.run_await_approval(
            session,
            uuid.UUID(connection_id),
            settings=_settings(),
            http=http,
            notifier=notifier,
            session_factory=sessions,
            clock=clock,
            sleep=clock.sleep,
        )


async def test_await_approval_backfills_on_first_success(
    api, session, sessions, monzo, queue
) -> None:
    connection_id = await _authorising(api)
    monzo.approve_after = 2  # approved during the third poll
    notifier, clock = RecordingNotifier(), FakeClock()

    await _await_approval(session, sessions, connection_id, monzo, notifier, clock)

    assert clock.slept == [10, 10]
    connection = await get_connection(session, connection_id)
    assert connection.status == "healthy"
    assert connection.last_success_at is not None
    assert "backfill_requested" not in connection.config
    assert "account_map" in connection.config
    # The full backfill: no `since`, so all history comes while the window is open.
    tx_requests = [r for r in monzo.requests if r.url.path == "/transactions"]
    assert tx_requests and all("since" not in r.url.params for r in tx_requests)
    assert await session.scalar(text("select count(*) from finance.transactions")) == 2
    assert connection.warnings == []
    assert notifier.messages == []


async def test_approval_window_expired(api, session, sessions, monzo, queue) -> None:
    connection_id = await _authorising(api)
    monzo.approve_after = None  # never approved
    notifier, clock = RecordingNotifier(), FakeClock()

    await _await_approval(session, sessions, connection_id, monzo, notifier, clock)

    # Every 10 s for 4 minutes: polls at 0, 10, ..., 240 s.
    assert len(monzo.account_requests()) == 25
    assert sum(clock.slept) == 240
    connection = await get_connection(session, connection_id)
    assert (connection.status, connection.last_error) == ("needs_reauth", EXPIRED)
    assert len(notifier.messages) == 1
    assert "My Monzo" in notifier.messages[0] and EXPIRED in notifier.messages[0]
    assert await session.scalar(text("select count(*) from finance.transactions")) == 0
    assert await session.scalar(text("select count(*) from finance.accounts")) == 0


async def test_await_approval_stops_when_no_longer_authorising(
    api, session, sessions, monzo, queue
) -> None:
    connection_id = await _authorising(api)
    response = await api.post(f"/api/v1/connections/{connection_id}/disable", headers=OWNER)
    assert response.status_code == 200
    notifier, clock = RecordingNotifier(), FakeClock()

    await _await_approval(session, sessions, connection_id, monzo, notifier, clock)

    assert monzo.account_requests() == []
    assert (await get_connection(session, connection_id)).status == "disabled"
    assert notifier.messages == []


@pytest.mark.parametrize("syncing", ["healthy", "failing"])
async def test_authorising_refused_while_the_connection_syncs(
    api, session, run, monzo, queue, syncing: str
) -> None:
    created = await _create(api)
    state = await _authorize(api, created["id"])
    await run(set_status, created["id"], syncing)

    refused = await api.post(f"/api/v1/connections/{created['id']}/authorize", headers=OWNER)
    callback = await _callback(api, state)

    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == "not_authorisable"
    assert (callback.status_code, callback.headers["location"]) == (
        303,
        "/connections?error=status",
    )
    assert monzo.token_forms == []
    assert await load_credential(session, KEY, created["id"], "access_token") is None
    assert (await get_connection(session, created["id"])).status == syncing
    assert list(queue.jobs.values()) == []


@pytest.mark.parametrize("status", ["needs_reauth", "authorising", "disabled"])
async def test_authorising_allowed_where_no_sync_runs(api, run, queue, status: str) -> None:
    created = await _create(api)
    await run(set_status, created["id"], status)
    state = await _authorize(api, created["id"])
    response = await _callback(api, state)
    assert response.headers["location"] == "/connections"


async def test_failed_backfill_clears_the_request(api, session, sessions, monzo, queue) -> None:
    connection_id = await _authorising(api)
    monzo.transactions_fail = True
    notifier, clock = RecordingNotifier(), FakeClock()

    await _await_approval(session, sessions, connection_id, monzo, notifier, clock)

    connection = await get_connection(session, connection_id)
    assert connection.status == "failing"
    assert "backfill_requested" not in connection.config
    assert connection.warnings == [
        "Full history could not be fetched during approval; authorise again to retry"
    ]

    # The next sync is incremental, clamped to Monzo's 90 days.
    monzo.transactions_fail = False
    monzo.requests.clear()
    async with httpx.AsyncClient(transport=httpx.MockTransport(monzo)) as http:
        await jobs.run_sync(
            session,
            uuid.UUID(connection_id),
            settings=_settings(),
            http=http,
            notifier=notifier,
            session_factory=sessions,
        )
    since = [r.url.params["since"] for r in monzo.requests if r.url.path == "/transactions"]
    assert since
    floor = datetime.now(UTC) - timedelta(days=89)
    assert abs(datetime.fromisoformat(since[0]) - floor) < timedelta(minutes=1)
    after = await get_connection(session, connection_id)
    assert after.status == "healthy"
    assert after.warnings[0].startswith("Full history could not be fetched")


def test_connection_operation_ids() -> None:
    spec = create_app(_settings()).openapi()
    operations = {
        (method, path): operation["operationId"]
        for path, item in spec["paths"].items()
        if path.startswith("/api/v1/connections")
        for method, operation in item.items()
    }
    assert operations == {
        ("get", "/api/v1/connections"): "list_connections",
        ("post", "/api/v1/connections"): "create_monzo_connection",
        ("post", "/api/v1/connections/{connection_id}/authorize"): "authorize_connection",
        ("get", "/api/v1/connections/oauth/monzo/callback"): "monzo_oauth_callback",
        ("post", "/api/v1/connections/{connection_id}/sync"): "sync_connection_now",
        ("post", "/api/v1/connections/{connection_id}/disable"): "disable_connection",
    }
    schema = spec["components"]["schemas"]["MonzoConnectionCreate"]
    assert schema["properties"]["client_secret"]["writeOnly"] is True
