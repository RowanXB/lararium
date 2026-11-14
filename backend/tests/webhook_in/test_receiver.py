"""The webhook receiver (lararium.webhook_in): one write-only route into core.webhook_inbox."""

import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import psycopg
import pytest
from pydantic import SecretStr
from starlette.applications import Starlette

from lararium.webhook_in import app as receiver
from lararium.webhook_in.app import MAX_BODY_BYTES, MAX_JSON_DEPTH, create_app

SECRET = "whsec-TEST-0123456789abcdef0123456789"
DELIVERY = {
    "type": "transaction.created",
    "data": {"account_id": "acc_TEST1", "id": "tx_TEST0001", "amount": -1234},
}

type Rows = Callable[[], list[tuple[str, Any]]]


def _dsn(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture
def inbox(owner_url: str, migrated: None) -> Iterator[Rows]:
    """The inbox's committed rows; emptied afterwards, so no other test sees them."""

    def rows() -> list[tuple[str, Any]]:
        with psycopg.connect(_dsn(owner_url)) as conn:
            return conn.execute(
                "select source, body from core.webhook_inbox order by received_at, id"
            ).fetchall()

    assert rows() == []
    yield rows
    with psycopg.connect(_dsn(owner_url)) as conn:
        conn.execute("delete from core.webhook_inbox")


@asynccontextmanager
async def _serve(app: Starlette) -> AsyncGenerator[httpx.AsyncClient]:
    # ASGITransport doesn't run the lifespan (which opens the pool); run it here.
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        yield client


@pytest.fixture
async def client(webhook_in_url: str, migrated: None) -> AsyncIterator[httpx.AsyncClient]:
    async with _serve(create_app(webhook_in_url, SecretStr(SECRET))) as test_client:
        yield test_client


async def test_valid_delivery_inserted_200(client: httpx.AsyncClient, inbox: Rows) -> None:
    response = await client.post(f"/monzo/{SECRET}", json=DELIVERY)

    assert response.status_code == 200
    assert response.content == b""
    assert inbox() == [("monzo", DELIVERY)]


async def test_body_of_exactly_64_kib_accepted(client: httpx.AsyncClient, inbox: Rows) -> None:
    empty = json.dumps({"pad": ""}).encode()
    body = json.dumps({"pad": "x" * (MAX_BODY_BYTES - len(empty))}).encode()
    assert len(body) == MAX_BODY_BYTES == 64 * 1024

    response = await client.post(
        f"/monzo/{SECRET}", content=body, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 200
    assert len(inbox()) == 1


@pytest.mark.parametrize(
    "path",
    [
        "/monzo/wrong",
        f"/monzo/{SECRET[:-1]}",
        f"/monzo/{SECRET}x",
        "/monzo/%C3%A9",
        f"/other/{SECRET}",
        f"/monzo/{SECRET}/extra",
    ],
)
async def test_wrong_secret_404(client: httpx.AsyncClient, inbox: Rows, path: str) -> None:
    response = await client.post(path, json=DELIVERY)

    assert response.status_code == 404
    assert SECRET not in response.text
    assert inbox() == []


async def _chunks(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def test_body_too_large_413(client: httpx.AsyncClient, inbox: Rows) -> None:
    body = b'{"pad": "' + b"x" * MAX_BODY_BYTES + b'"}'

    declared = await client.post(f"/monzo/{SECRET}", content=body)
    # Chunked, with no Content-Length: the limit holds while the body is read.
    streamed = await client.post(f"/monzo/{SECRET}", content=_chunks(body[:1000], body[1000:]))

    assert declared.status_code == 413
    assert streamed.status_code == 413
    assert inbox() == []


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b'{"data": ',
        b"",
        b"NaN",
        b'{"amount": Infinity}',
        b"\xff\xfe",
        # Valid JSON that Postgres jsonb refuses.
        b'{"note": "\\u0000"}',
        # Nested deeper than the parser goes: the deepest that fits in 64 KiB.
        b"[" * (MAX_BODY_BYTES // 2) + b"]" * (MAX_BODY_BYTES // 2),
    ],
)
async def test_invalid_json_400(client: httpx.AsyncClient, inbox: Rows, body: bytes) -> None:
    response = await client.post(f"/monzo/{SECRET}", content=body)

    assert response.status_code == 400
    assert inbox() == []


async def test_nesting_past_limit_400(client: httpx.AsyncClient, inbox: Rows) -> None:
    depth = MAX_JSON_DEPTH + 1
    body = b"[" * depth + b"]" * depth

    response = await client.post(f"/monzo/{SECRET}", content=body)

    assert response.status_code == 400
    assert inbox() == []


async def test_nesting_at_limit_200(client: httpx.AsyncClient, inbox: Rows) -> None:
    body = b'{"a": ' * (MAX_JSON_DEPTH - 1) + b"[]" + b"}" * (MAX_JSON_DEPTH - 1)

    response = await client.post(f"/monzo/{SECRET}", content=body)

    assert response.status_code == 200
    assert len(inbox()) == 1


async def test_get_is_405(client: httpx.AsyncClient, inbox: Rows) -> None:
    assert (await client.get(f"/monzo/{SECRET}")).status_code == 405
    assert inbox() == []


def _as_receiver(webhook_in_url: str, statement: str) -> None:
    with psycopg.connect(_dsn(webhook_in_url)) as conn:
        conn.execute(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "select * from core.webhook_inbox",
        "select count(*) from core.webhook_inbox",
        "select * from finance.accounts",
        "select * from core.credentials",
        "select * from core.connections",
        "update core.webhook_inbox set outcome = 'queued'",
        "delete from core.webhook_inbox",
        "insert into core.webhook_inbox (source, body) values ('monzo', '{}') returning id",
    ],
)
def test_receiver_role_cannot_select(
    webhook_in_url: str, migrated: None, inbox: Rows, statement: str
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _as_receiver(webhook_in_url, statement)
    assert inbox() == []


def test_receiver_role_can_insert(webhook_in_url: str, migrated: None, inbox: Rows) -> None:
    _as_receiver(
        webhook_in_url, "insert into core.webhook_inbox (source, body) values ('monzo', '{}')"
    )
    assert inbox() == [("monzo", {})]


async def test_module_app_built_from_env_without_finance_settings(
    monkeypatch: pytest.MonkeyPatch, webhook_in_url: str, inbox: Rows
) -> None:
    for name in ("LARARIUM_MASTER_KEY", "LARARIUM_OWNER_LOGIN", "LARARIUM_AUTH_MODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LARARIUM_DATABASE_URL", webhook_in_url)
    monkeypatch.setenv("LARARIUM_WEBHOOK_SECRET", SECRET)

    async with _serve(receiver.app) as test_client:
        assert (await test_client.post(f"/monzo/{SECRET}", json=DELIVERY)).status_code == 200
    assert inbox() == [("monzo", DELIVERY)]
