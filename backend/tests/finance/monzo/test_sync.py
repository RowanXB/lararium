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
from lararium.core.connectors import BACKFILL_FAILED, SyncContext
from lararium.core.json_types import JSONValue
from lararium.core.notify import NullNotifier
from lararium.modules.finance.monzo.sync import MonzoConnector
from lararium.modules.finance.valuation import balances_asof

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
        self.balance = make_balance()
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/accounts":
            return httpx.Response(200, json={"accounts": [self.account]})
        if path == "/balance":
            return httpx.Response(200, json=self.balance)
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
    # Everything needed to replay the page request exactly.
    assert by_type["transactions_page"].request == {
        "account_id": ACCOUNT_ID,
        "since": None,
        "limit": "100",
        "expand[]": "merchant",
    }
    assert by_type["transactions_page"].content[0]["id"] == "tx_TEST0001"
    assert cursor == {
        "accounts": {
            ACCOUNT_ID: {"last_created": _iso(created), "backfilled": True, "reconciled": True}
        }
    }
    # A full backfill sends no `since`, and the request flag is cleared afterwards.
    assert "since" not in fake.transaction_requests()[0].url.params
    after = await get_connection(session, connection.id)
    assert "backfill_requested" not in after.config
    assert after.config["client_id"] == "oauth2client_TEST1"


async def test_incremental_sync_uses_14_day_overlap(session, run) -> None:
    last_created = datetime.now(UTC) - timedelta(days=5)
    # Without an opening yet, so the empty ledger isn't reconciled against the balance.
    state = {"last_created": _iso(last_created), "backfilled": False, "reconciled": False}
    cursor = {"accounts": {ACCOUNT_ID: state}}
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
    cursor = {"accounts": {ACCOUNT_ID: {"last_created": _iso(last_created), "backfilled": False}}}
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


async def test_accounts_missing_from_response_keep_their_cursor(session, run) -> None:
    now = datetime.now(UTC)
    other = {"last_created": _iso(now - timedelta(days=2)), "backfilled": True}
    cursor = {
        "accounts": {
            ACCOUNT_ID: {"last_created": _iso(now - timedelta(days=5)), "backfilled": True},
            "acc_TEST_GONE": other,
        }
    }
    connection = await _connection(run, cursor=cursor)

    result = await _sync(session, run, connection, FakeMonzo([]))

    assert result["accounts"]["acc_TEST_GONE"] == other  # type: ignore[index]


async def test_sync_normalizes_into_the_ledger(session, run) -> None:
    now = datetime.now(UTC)
    payment = make_transaction(
        id="tx_TEST0001", amount=-1234, created=_iso(now - timedelta(days=3))
    )
    account = make_account(created=_iso(now - timedelta(days=4)))
    connection = await _connection(run, config={"backfill_requested": True})

    await _sync(session, run, connection, FakeMonzo([payment], account))

    after = await get_connection(session, connection.id)
    account_map = after.config["account_map"]
    assert isinstance(account_map, dict)
    assert set(account_map) == {ACCOUNT_ID, "pot_TEST1"}
    rows = (
        await session.execute(
            text("select external_id from finance.transactions order by external_id")
        )
    ).all()
    # The payment, and the opening balance that brings the ledger to Monzo's 250.00.
    assert [r.external_id for r in rows] == [f"opening:{ACCOUNT_ID}", "tx_TEST0001"]
    balance = await session.scalar(
        text(
            "select sum(p.quantity) from finance.postings p"
            " join finance.transactions t on t.id = p.transaction_id"
            " where p.account_id = cast(:account as uuid) and t.status <> 'void'"
        ),
        {"account": account_map[ACCOUNT_ID]},
    )
    assert str(balance) == "250.00"
    assert after.warnings == []


def _noon(days_ago: int) -> datetime:
    """Noon UTC some days ago: the same date in the ledger's local time zone."""
    day = datetime.now(UTC) - timedelta(days=days_ago)
    return day.replace(hour=12, minute=0, second=0, microsecond=0)


async def _ledger(session, connection: ConnectionRecord) -> dict[str, Any]:
    """The main account's balance, the opening adjustment(s) and the net worth of the
    account and its pot as of tomorrow."""
    account_map: Any = (await get_connection(session, connection.id)).config["account_map"]
    main, pot = uuid.UUID(account_map[ACCOUNT_ID]), uuid.UUID(account_map["pot_TEST1"])
    balance = await session.scalar(
        text(
            "select sum(p.quantity) from finance.postings p"
            " join finance.transactions t on t.id = p.transaction_id"
            " where p.account_id = :account and t.status <> 'void'"
        ),
        {"account": main},
    )
    openings = (
        await session.execute(
            text(
                "select t.status, t.booking_date, p.quantity from finance.transactions t"
                " join finance.postings p on p.transaction_id = t.id and p.position = 0"
                " where t.external_id = :external_id"
            ),
            {"external_id": f"opening:{ACCOUNT_ID}"},
        )
    ).all()
    tomorrow = datetime.now(UTC).date() + timedelta(days=1)
    rows = await balances_asof(session, tomorrow)
    net_worth = sum(r.quantity for r in rows if r.account_id in (main, pot))
    return {
        "balance": str(balance),
        "openings": [(o.status, o.booking_date, str(o.quantity)) for o in openings],
        "net_worth": str(net_worth),
    }


async def test_account_first_seen_on_an_incremental_sync_gets_an_opening(session, run) -> None:
    # A joint account opened after the connection's backfill appears on a later sync.
    other = {"last_created": _iso(_noon(2)), "backfilled": True}
    connection = await _connection(run, cursor={"accounts": {"acc_TEST_OTHER": other}})
    payment = make_transaction(id="tx_TEST0001", created=_iso(_noon(10)))
    fake = FakeMonzo([payment], make_account(created=_iso(_noon(300))))

    cursor = await _sync(session, run, connection, fake)

    state: Any = cursor["accounts"][ACCOUNT_ID]  # type: ignore[index]
    assert (state["backfilled"], state["reconciled"]) == (False, True)
    ledger = await _ledger(session, connection)
    assert ledger["balance"] == "250.00"
    day_before = _noon(11).date()
    assert ledger["openings"] == [("booked", day_before, "262.34")]
    assert ledger["net_worth"] == "300.00"  # Monzo's total balance, the pot included
    assert (await get_connection(session, connection.id)).warnings == []

    # From now on it is reconciled, and the opening is left alone.
    fake.balance = make_balance(balance=26000)
    await run(lambda ctx: _record_cursor(ctx, connection, cursor))
    await _sync(session, run, connection, fake)

    (warning,) = (await get_connection(session, connection.id)).warnings
    assert warning.startswith(f"Monzo account {ACCOUNT_ID}: reconciliation mismatch")
    assert (await _ledger(session, connection))["openings"] == [("booked", day_before, "262.34")]


async def test_full_backfill_with_older_history_corrects_the_opening(session, run) -> None:
    account = make_account(created=_iso(_noon(201)))
    recent = make_transaction(id="tx_TEST0002", created=_iso(_noon(10)))
    connection = await _connection(run)
    cursor = await _sync(session, run, connection, FakeMonzo([recent], account))
    assert (await _ledger(session, connection))["openings"] == [
        ("booked", _noon(11).date(), "262.34")
    ]

    # Authorised again: the backfill now fetches older history too.
    older = make_transaction(id="tx_TEST0001", amount=10000, created=_iso(_noon(200)))
    await run(lambda ctx: _record_cursor(ctx, connection, cursor))
    await run(update_config, connection.id, {"backfill_requested": True})
    await _sync(session, run, connection, FakeMonzo([older, recent], account))

    ledger = await _ledger(session, connection)
    # Corrected in place, not added to: 162.34 + 100.00 - 12.34 is Monzo's 250.00.
    assert ledger["openings"] == [("booked", _noon(201).date(), "162.34")]
    assert ledger["balance"] == "250.00"
    assert ledger["net_worth"] == "300.00"
    assert (await get_connection(session, connection.id)).warnings == []


async def test_full_backfill_drops_the_warnings_it_resolves(session, run) -> None:
    now = datetime.now(UTC)
    truncated = f"Monzo account {ACCOUNT_ID}: history may be truncated"
    connection = await _connection(run, config={"backfill_requested": True})
    await run(set_warnings, connection.id, [BACKFILL_FAILED, truncated, "something else"])
    account = make_account(created=_iso(now - timedelta(days=40)))
    first = make_transaction(id="tx_TEST0001", created=_iso(now - timedelta(days=39)))

    await _sync(session, run, connection, FakeMonzo([first], account))

    assert (await get_connection(session, connection.id)).warnings == ["something else"]


async def test_full_backfill_keeps_a_truncation_that_still_applies(session, run) -> None:
    now = datetime.now(UTC)
    truncated = f"Monzo account {ACCOUNT_ID}: history may be truncated"
    connection = await _connection(run, config={"backfill_requested": True})
    await run(set_warnings, connection.id, [truncated])
    account = make_account(created=_iso(now - timedelta(days=400)))
    first = make_transaction(id="tx_TEST0001", created=_iso(now - timedelta(days=89)))

    await _sync(session, run, connection, FakeMonzo([first], account))

    assert (await get_connection(session, connection.id)).warnings == [truncated]


async def _record_cursor(ctx: CommandContext, connection: ConnectionRecord, cursor: Any) -> None:
    # As run_sync records it after a successful sync.
    await ctx.session.execute(
        text("update core.connections set cursor = cast(:c as jsonb) where id = :id"),
        {"c": json.dumps(cursor), "id": connection.id},
    )
