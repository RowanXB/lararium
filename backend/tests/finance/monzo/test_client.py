import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lararium.core.actors import Actor
from lararium.core.commands import CommandContext, command_context
from lararium.core.connections import (
    ConnectionRecord,
    create_connection,
    load_credential,
    store_credential,
)
from lararium.core.connectors import SyncContext
from lararium.core.errors import CredentialRejectedError
from lararium.core.notify import NullNotifier
from lararium.core.secrets import Secret
from lararium.db import create_engine, make_sessionmaker
from lararium.modules.finance.monzo.auth import (
    TokenPair,
    access_token_for,
    authorize_url,
    refresh,
)
from lararium.modules.finance.monzo.client import (
    AccessTokenExpiredError,
    MonzoApiError,
    MonzoClient,
    MonzoRateLimitedError,
    NotYetApprovedError,
)

from .fixtures import ACCOUNT_ID, make_transaction

KEY = bytes(range(32))
ACCESS = "at-TEST-access-token-value"
CONNECTOR = Actor("connector", "monzo")

type Handler = Callable[[httpx.Request], httpx.Response]


def _http(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_transactions_paginates_with_since_last_id() -> None:
    history = [make_transaction(id=f"tx_TEST{i:04d}") for i in range(250)]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        since = request.url.params.get("since")
        start = 0 if since is None else [t["id"] for t in history].index(since) + 1
        limit = int(request.url.params["limit"])
        return httpx.Response(200, json={"transactions": history[start : start + limit]})

    async with _http(handler) as http:
        client = MonzoClient(http, Secret(ACCESS))
        pages = [page async for page in client.transactions(ACCOUNT_ID, since=None)]

    assert [len(page) for page in pages] == [100, 100, 50]
    assert [t["id"] for page in pages for t in page] == [t["id"] for t in history]
    assert len(requests) == 3
    first, second, third = (r.url.params for r in requests)
    assert "since" not in first
    assert second["since"] == history[99]["id"]
    assert third["since"] == history[199]["id"]
    for request in requests:
        assert request.url.path == "/transactions"
        assert request.url.params["account_id"] == ACCOUNT_ID
        assert request.url.params.get_list("expand[]") == ["merchant"]
        assert request.url.params["limit"] == "100"
        assert request.headers["authorization"] == f"Bearer {ACCESS}"


async def test_transactions_passes_since_and_before() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"transactions": []})

    async with _http(handler) as http:
        client = MonzoClient(http, Secret(ACCESS))
        pages = [
            page
            async for page in client.transactions(
                ACCOUNT_ID, since="2026-01-01T00:00:00Z", before="2026-02-01T00:00:00Z"
            )
        ]

    assert pages == []
    assert seen[0].url.params["since"] == "2026-01-01T00:00:00Z"
    assert seen[0].url.params["before"] == "2026-02-01T00:00:00Z"


def test_authorize_url_params() -> None:
    redirect = "https://lararium.example/api/v1/connections/oauth/monzo/callback?x=1"
    url = authorize_url("oauth2client_TEST1", redirect, "st ate&1")

    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://auth.monzo.com/"
    assert parse_qs(parts.query) == {
        "client_id": ["oauth2client_TEST1"],
        "redirect_uri": [redirect],
        "response_type": ["code"],
        "state": ["st ate&1"],
    }
    # Encoded: the redirect's own query and the state's "&" don't leak into ours.
    assert "redirect_uri=https%3A%2F%2Flararium.example" in parts.query
    assert "x=1" not in parts.query
    assert "&1" not in parts.query


@pytest.mark.parametrize(
    ("status", "body", "error", "code"),
    [
        (401, {"code": "unauthorized.bad_access_token.expired"}, AccessTokenExpiredError, None),
        (401, {"error": "invalid_token"}, AccessTokenExpiredError, "invalid_token"),
        (401, {"error": "unauthorized"}, AccessTokenExpiredError, "unauthorized"),
        (401, {"code": "bad_request.other"}, MonzoApiError, "bad_request.other"),
        (403, {"code": "forbidden.insufficient_permissions"}, NotYetApprovedError, None),
        (429, {"code": "too_many_requests"}, MonzoRateLimitedError, "too_many_requests"),
        (500, {"code": "internal_service"}, MonzoApiError, "internal_service"),
        (502, "<html>bad gateway</html>", MonzoApiError, None),
    ],
)
async def test_error_mapping(status, body, error, code) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    async with _http(handler) as http:
        with pytest.raises(MonzoApiError) as raised:
            await MonzoClient(http, Secret(ACCESS)).accounts()

    assert type(raised.value) is error
    assert raised.value.status == status
    if code is not None:
        assert raised.value.code == code


async def test_client_endpoints() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        match (request.method, request.url.path):
            case ("GET", "/ping/whoami"):
                return httpx.Response(200, json={"authenticated": True})
            case ("GET", "/accounts"):
                return httpx.Response(200, json={"accounts": [{"id": ACCOUNT_ID}]})
            case ("GET", "/balance"):
                return httpx.Response(200, json={"balance": 100})
            case ("GET", "/pots"):
                return httpx.Response(200, json={"pots": [{"id": "pot_TEST1"}]})
            case ("GET", "/webhooks"):
                return httpx.Response(200, json={"webhooks": [{"id": "webhook_TEST1"}]})
            case ("POST", "/webhooks"):
                return httpx.Response(200, json={"webhook": {"id": "webhook_TEST2"}})
            case ("DELETE", "/webhooks/webhook_TEST1"):
                return httpx.Response(200, json={})
            case _:
                return httpx.Response(404, json={"code": "not_found"})

    async with _http(handler) as http:
        client = MonzoClient(http, Secret(ACCESS))
        assert await client.whoami() == {"authenticated": True}
        assert await client.accounts() == [{"id": ACCOUNT_ID}]
        assert await client.balance(ACCOUNT_ID) == {"balance": 100}
        assert await client.pots(ACCOUNT_ID) == [{"id": "pot_TEST1"}]
        assert await client.webhooks(ACCOUNT_ID) == [{"id": "webhook_TEST1"}]
        hook = "https://hooks.example/monzo/s3cret"
        assert await client.register_webhook(ACCOUNT_ID, hook) == {"id": "webhook_TEST2"}
        await client.delete_webhook("webhook_TEST1")

    assert seen[2].url.params["account_id"] == ACCOUNT_ID
    assert seen[3].url.params["current_account_id"] == ACCOUNT_ID
    assert seen[4].url.params["account_id"] == ACCOUNT_ID
    assert parse_qs(seen[5].content.decode()) == {"account_id": [ACCOUNT_ID], "url": [hook]}


async def test_errors_never_contain_tokens() -> None:
    client_secret = "cs-TEST-client-secret"
    refresh_token = "rt-TEST-refresh-token"
    secrets = (ACCESS, client_secret, refresh_token)

    def echo(request: httpx.Request) -> dict[str, str]:
        # A hostile or buggy upstream that echoes everything it was sent.
        sent = request.headers.get("authorization", "") + " " + request.content.decode()
        return {"code": sent, "error": sent, "message": sent}

    raised: list[BaseException] = []
    for status in (400, 401, 403, 429, 500):

        def handler(request: httpx.Request, status: int = status) -> httpx.Response:
            return httpx.Response(status, json=echo(request))

        async with _http(handler) as http:
            with pytest.raises(MonzoApiError) as api_error:
                await MonzoClient(http, Secret(ACCESS)).accounts()
            raised.append(api_error.value)
            with pytest.raises((MonzoApiError, CredentialRejectedError)) as token_error:
                await refresh(
                    http, "oauth2client_TEST1", Secret(client_secret), Secret(refresh_token)
                )
            raised.append(token_error.value)

    pair = TokenPair(Secret(ACCESS), Secret(refresh_token), datetime.now(UTC))
    shown = [str(e) for e in raised] + [repr(e) for e in raised] + [repr(pair), str(pair)]
    shown += [str(arg) for e in raised for arg in e.args]
    for secret in secrets:
        assert not any(secret in text for text in shown), secret


def _token_response(access: str, refresh_token: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": access,
            "refresh_token": refresh_token,
            "expires_in": 21600,
            "token_type": "Bearer",
            "client_id": "oauth2client_TEST1",
            "user_id": "user_TEST1",
        },
    )


async def _store_tokens(ctx: CommandContext, connection_id: uuid.UUID, expires_in: timedelta):
    await store_credential(ctx, KEY, connection_id, "client_secret", "cs-TEST", None)
    await store_credential(
        ctx, KEY, connection_id, "access_token", "at-old", datetime.now(UTC) + expires_in
    )
    await store_credential(ctx, KEY, connection_id, "refresh_token", "rt-old", None)


def _sync_ctx(
    ctx: CommandContext,
    connection: ConnectionRecord,
    http: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> SyncContext:
    return SyncContext(
        command=ctx,
        connection=connection,
        sync_id=uuid.uuid4(),
        master_key=KEY,
        http=http,
        notifier=NullNotifier(),
        session_factory=session_factory,
    )


async def _connection(run, expires_in: timedelta) -> ConnectionRecord:
    connection = await run(
        create_connection,
        connector="monzo",
        display_name="Monzo",
        config={"client_id": "oauth2client_TEST1"},
    )
    await run(_store_tokens, connection.id, expires_in)
    return connection


async def test_valid_access_token_is_used_without_refresh(session, run) -> None:
    connection = await _connection(run, timedelta(hours=1))

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request to {request.url}")

    async with _http(handler) as http:
        ctx = CommandContext(session, CONNECTOR, uuid.uuid4())
        sessions = async_sessionmaker(bind=session.bind)
        token = await access_token_for(_sync_ctx(ctx, connection, http, sessions))

    assert token.reveal() == "at-old"


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (400, {"error": "invalid_grant", "error_description": "refresh token is invalid"}),
        (401, {"code": "unauthorized.bad_refresh_token.evicted"}),
    ],
)
async def test_invalid_grant_is_credential_rejected(session, run, status, body) -> None:
    connection = await _connection(run, timedelta(minutes=4))
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json=body)

    async with _http(handler) as http:
        ctx = CommandContext(session, CONNECTOR, uuid.uuid4())
        sessions = async_sessionmaker(bind=session.bind)
        with pytest.raises(CredentialRejectedError):
            await access_token_for(_sync_ctx(ctx, connection, http, sessions))

    assert [str(r.url) for r in requests] == ["https://api.monzo.com/oauth2/token"]
    assert parse_qs(requests[0].content.decode()) == {
        "grant_type": ["refresh_token"],
        "client_id": ["oauth2client_TEST1"],
        "client_secret": ["cs-TEST"],
        "refresh_token": ["rt-old"],
    }
    kept = await load_credential(session, KEY, connection.id, "refresh_token")
    assert kept is not None and kept.reveal() == "rt-old"


async def test_refreshed_tokens_committed_before_use(
    backend_url: str, owner_url: str, migrated: None
) -> None:
    # Ruling R3: a real commit through its own engine, not the savepoint-wrapped fixture.
    engine = create_engine(backend_url)
    sessions = make_sessionmaker(engine)
    connection_id: uuid.UUID | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return _token_response("at-new", "rt-new")
        return httpx.Response(500, json={"code": "internal_service"})

    try:
        async with sessions() as setup:
            async with command_context(setup, Actor("user", "test"), "test.setup", {}) as ctx:
                connection = await create_connection(
                    ctx,
                    connector="monzo",
                    display_name=f"Monzo {uuid.uuid4()}",
                    config={"client_id": "oauth2client_TEST1"},
                )
                connection_id = connection.id
                await _store_tokens(ctx, connection_id, timedelta(minutes=1))

        async with _http(handler) as http, sessions() as sync_session:
            args = {"connection_id": str(connection_id)}
            with pytest.raises(MonzoApiError):
                async with command_context(sync_session, CONNECTOR, "connection.sync", args) as ctx:
                    token = await access_token_for(_sync_ctx(ctx, connection, http, sessions))
                    assert token.reveal() == "at-new"
                    await MonzoClient(http, token).accounts()

        async with sessions() as fresh:
            refreshed = await load_credential(fresh, KEY, connection_id, "refresh_token")
            access = await load_credential(fresh, KEY, connection_id, "access_token")
            expires_at = await fresh.scalar(
                text(
                    "select expires_at from core.credentials"
                    " where connection_id = :id and purpose = 'access_token'"
                ),
                {"id": connection_id},
            )
            audit = (
                await fresh.execute(
                    text(
                        "select actor_type, actor_id, args::text, changes::text"
                        " from core.audit_log where command = 'connection.save_tokens'"
                        " and args->>'connection_id' = :id"
                    ),
                    {"id": str(connection_id)},
                )
            ).all()
        assert refreshed is not None and refreshed.reveal() == "rt-new"
        assert access is not None and access.reveal() == "at-new"
        assert expires_at > datetime.now(UTC) + timedelta(hours=5)
        assert len(audit) == 1
        actor_type, actor_id, logged_args, logged_changes = audit[0]
        assert (actor_type, actor_id) == ("connector", "monzo")
        for secret in ("at-new", "rt-new", "at-old", "rt-old"):
            assert secret not in logged_args + logged_changes
    finally:
        if connection_id is not None:
            # Credentials cascade; the audit log is append-only for the backend role.
            async with engine.begin() as conn:
                params = {"id": connection_id}
                await conn.execute(text("delete from core.events where subject_id = :id"), params)
                await conn.execute(text("delete from core.connections where id = :id"), params)
            owner = create_engine(owner_url)
            try:
                async with owner.begin() as conn:
                    await conn.execute(
                        text(
                            "delete from core.audit_log where args->>'connection_id' = :id"
                            " or changes ? ('connection:' || :id)"
                        ),
                        {"id": str(connection_id)},
                    )
            finally:
                await owner.dispose()
        await engine.dispose()
