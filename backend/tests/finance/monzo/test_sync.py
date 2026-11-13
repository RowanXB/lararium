import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from lararium.core.actors import Actor
from lararium.core.commands import CommandContext
from lararium.core.connections import (
    ConnectionRecord,
    create_connection,
    get_connection,
    set_warnings,
    store_credential,
    update_config,
)
from lararium.core.connectors import SyncContext
from lararium.core.json_types import JSONValue
from lararium.core.notify import NullNotifier
from lararium.modules.finance.monzo.sync import MonzoConnector

from .fixtures import ACCOUNT_ID, make_account, make_balance, make_pot, make_transaction

KEY = bytes(range(32))
CONNECTOR = Actor("connector", "monzo")

type Handler = Callable[[httpx.Request], httpx.Response]


def _iso(value: datetime) -> str:
    return f"{value:%Y-%m-%dT%H:%M:%S}.{value.microsecond // 1000:03d}Z"


class FakeMonzo:
    """Serves one account's endpoints from fixed data and records every request."""

    def __init__(self, transactions: list[dict[str, Any]], account: dict[str, Any] | None = None):
        self.transactions = transactions
        self.account = account or make_account()
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/accounts":
            return httpx.Response(200, json={"accounts": [self.account]})
        if path == "/balance":
            return httpx.Response(200, json=make_balance())
        if path == "/pots":
            return httpx.Response(200, json={"pots": [make_pot()]})
        assert path == "/transactions", path
        since = request.url.params.get("since")
        ids = [t["id"] for t in self.transactions]
        if since is None or since not in ids:
            start = 0
        else:
            start = ids.index(since) + 1
        limit = int(request.url.params["limit"])
        return httpx.Response(200, json={"transactions": self.transactions[start : start + limit]})

    def transaction_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/transactions"]


async def _connection(
    run, *, config: dict[str, JSONValue] | None = None, cursor: JSONValue = None
) -> ConnectionRecord:
    async def setup(ctx: CommandContext) -> ConnectionRecord:
        connection = await create_connection(
            ctx,
            connector="monzo",
            display_name="Monzo",
            config={"client_id": "oauth2client_TEST1"} | (config or {}),
        )
        await store_credential(
            ctx,
            KEY,
            connection.id,
            "access_token",
            "at-TEST",
            datetime.now(UTC) + timedelta(hours=1),
        )
        if cursor is not None:
            await ctx.session.execute(
                text("update core.connections set cursor = cast(:c as jsonb) where id = :id"),
                {"c": json.dumps(cursor), "id": connection.id},
            )
        return connection

    return await run(setup)


async def _sync(session, run, connection: ConnectionRecord, fake: FakeMonzo) -> JSONValue:
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as http:
        ctx = CommandContext(session, CONNECTOR, uuid.uuid4())
        sync_ctx = SyncContext(
            command=ctx,
            connection=await get_connection(session, connection.id),
            sync_id=uuid.uuid4(),
            master_key=KEY,
            http=http,
            notifier=NullNotifier(),
            session_factory=async_sessionmaker(bind=session.bind),
        )
        return await MonzoConnector().sync(sync_ctx)


async def _raw_types(session) -> list[str]:
    rows = await session.execute(
        text("select record_type from core.raw_records order by record_type, id")
    )
    return [r[0] for r in rows]


def test_connector_identity() -> None:
    connector = MonzoConnector()
    assert connector.name == "monzo"
    assert connector.version == "monzo-sync-1"
    assert connector.capabilities == {"balances", "transactions", "webhook"}


async def test_first_sync_stores_raw_records_per_endpoint(session, run) -> None:
    created = datetime.now(UTC) - timedelta(days=3)
    fake = FakeMonzo([make_transaction(id="tx_TEST0001", created=_iso(created))])
    connection = await _connection(run, config={"backfill_requested": True})

    cursor = await _sync(session, run, connection, fake)

    assert await _raw_types(session) == ["accounts", "balance", "pots", "transactions_page"]
    rows = (
        await session.execute(
            text("select record_type, request, content from core.raw_records order by id")
        )
    ).all()
    by_type = {r.record_type: r for r in rows}
    assert by_type["balance"].request == {"account_id": ACCOUNT_ID}
    assert by_type["pots"].request == {"current_account_id": ACCOUNT_ID}
    assert by_type["transactions_page"].request == {"account_id": ACCOUNT_ID, "since": None}
    assert by_type["transactions_page"].content[0]["id"] == "tx_TEST0001"
    assert cursor == {"accounts": {ACCOUNT_ID: {"last_created": _iso(created), "backfilled": True}}}
    # A full backfill sends no `since`, and the request flag is cleared afterwards.
    assert "since" not in fake.transaction_requests()[0].url.params
    after = await get_connection(session, connection.id)
    assert "backfill_requested" not in after.config
    assert after.config["client_id"] == "oauth2client_TEST1"


async def test_incremental_sync_uses_14_day_overlap(session, run) -> None:
    last_created = datetime.now(UTC) - timedelta(days=5)
    cursor = {"accounts": {ACCOUNT_ID: {"last_created": _iso(last_created), "backfilled": True}}}
    fake = FakeMonzo([])
    connection = await _connection(run, cursor=cursor)

    result = await _sync(session, run, connection, fake)

    (request,) = fake.transaction_requests()
    assert request.url.params["since"] == _iso(last_created - timedelta(days=14))
    assert request.url.params["limit"] == "100"
    assert result == cursor
    assert (await get_connection(session, connection.id)).warnings == []


async def test_since_clamped_to_89_days_with_warning(session, run) -> None:
    last_created = datetime.now(UTC) - timedelta(days=200)
    cursor = {"accounts": {ACCOUNT_ID: {"last_created": _iso(last_created), "backfilled": True}}}
    fake = FakeMonzo([])
    connection = await _connection(run, cursor=cursor)

    before = datetime.now(UTC)
    await _sync(session, run, connection, fake)
    after = datetime.now(UTC)

    (request,) = fake.transaction_requests()
    since = datetime.fromisoformat(request.url.params["since"])
    assert before - timedelta(days=89, seconds=1) <= since <= after - timedelta(days=89)
    warnings = (await get_connection(session, connection.id)).warnings
    assert len(warnings) == 1
    assert "89 days" in warnings[0]


async def test_backfill_truncation_warning(session, run) -> None:
    now = datetime.now(UTC)
    account = make_account(created=_iso(now - timedelta(days=400)))
    first = make_transaction(id="tx_TEST0001", created=_iso(now - timedelta(days=89)))
    fake = FakeMonzo([first], account)
    connection = await _connection(run, config={"backfill_requested": True})

    await _sync(session, run, connection, fake)

    warnings = (await get_connection(session, connection.id)).warnings
    assert warnings == [f"Monzo account {ACCOUNT_ID}: history may be truncated"]


async def test_complete_backfill_has_no_truncation_warning(session, run) -> None:
    now = datetime.now(UTC)
    account = make_account(created=_iso(now - timedelta(days=40)))
    first = make_transaction(id="tx_TEST0001", created=_iso(now - timedelta(days=39)))
    connection = await _connection(run, config={"backfill_requested": True})

    await _sync(session, run, connection, FakeMonzo([first], account))

    assert (await get_connection(session, connection.id)).warnings == []


async def test_other_warnings_are_kept(session, run) -> None:
    connection = await _connection(run)
    await run(set_warnings, connection.id, ["reconciliation mismatch"])
    await run(update_config, connection.id, {"client_id": "oauth2client_TEST1"})
    now = datetime.now(UTC)
    fake = FakeMonzo([make_transaction(created=_iso(now - timedelta(days=1)))])

    await _sync(session, run, connection, fake)

    warnings = (await get_connection(session, connection.id)).warnings
    assert warnings[0] == "reconciliation mismatch"


async def test_unchanged_responses_not_stored_twice(session, run) -> None:
    now = datetime.now(UTC)
    fake = FakeMonzo([make_transaction(created=_iso(now - timedelta(days=1)))])
    connection = await _connection(run)

    await _sync(session, run, connection, fake)
    count = await session.scalar(text("select count(*) from core.raw_records"))
    await _sync(session, run, connection, fake)

    assert count == 4
    assert await session.scalar(text("select count(*) from core.raw_records")) == count


async def test_paginates_and_keeps_closed_accounts(session, run) -> None:
    now = datetime.now(UTC)
    history = [
        make_transaction(id=f"tx_TEST{i:04d}", created=_iso(now - timedelta(days=50, minutes=-i)))
        for i in range(150)
    ]
    fake = FakeMonzo(history, make_account(closed=True))
    connection = await _connection(run, config={"backfill_requested": True})

    cursor = await _sync(session, run, connection, fake)

    assert (await _raw_types(session)).count("transactions_page") == 2
    assert cursor["accounts"][ACCOUNT_ID]["last_created"] == history[-1]["created"]  # type: ignore[index]
