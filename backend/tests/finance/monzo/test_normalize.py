import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from lararium.core.actors import Actor
from lararium.core.commands import command_context
from lararium.core.connections import (
    ConnectionRecord,
    create_connection,
    get_connection,
    set_warnings,
)
from lararium.core.connectors import SyncContext
from lararium.core.json_types import JSONValue
from lararium.core.models import Entity
from lararium.core.raw_records import store_raw_record
from lararium.modules.finance.accounts import get_account
from lararium.modules.finance.monzo.normalize import (
    NORMALIZER_VERSION,
    FetchedRecord,
    NormalizeReport,
    normalize,
    to_quantity,
)
from lararium.modules.finance.system_accounts import get_or_create_system_account

from .fixtures import ACCOUNT_ID, make_account, make_balance, make_pot, make_transaction

KEY = bytes(range(32))
CONNECTOR = Actor("connector", "monzo")
MERCHANT = {"id": "merch_TEST1", "name": "Test Grocer", "category": "groceries"}


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> None:
        self.messages.append(text)


def _no_http(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"normalize made an HTTP request: {request.url}")


async def _connection(run) -> ConnectionRecord:
    return await run(create_connection, connector="monzo", display_name="Monzo", config={})


async def _normalize(
    session,
    connection: ConnectionRecord,
    *,
    transactions: Sequence[dict[str, Any]] = (),
    balance: int = 25000,
    pots: Sequence[dict[str, Any]] = (),
    backfilled: Sequence[str] = (),
    first_backfills: Sequence[str] = (),
    notifier: RecordingNotifier | None = None,
) -> NormalizeReport:
    """Store one sync's raw records (as MonzoConnector.sync would), then normalize them."""
    sync_id = uuid.uuid4()
    async with command_context(session, CONNECTOR, "connection.sync", {}) as command:
        fetched: list[FetchedRecord] = []

        async def store(record_type: str, request: dict[str, JSONValue], content: Any) -> None:
            record_id = await store_raw_record(
                command,
                connection_id=connection.id,
                record_type=record_type,
                request=request,
                content=content,
                connector_version="monzo-sync-1",
                sync_id=sync_id,
            )
            fetched.append(FetchedRecord(record_id, request))

        await store("accounts", {}, [make_account()])
        await store("balance", {"account_id": ACCOUNT_ID}, make_balance(balance=balance))
        await store("pots", {"current_account_id": ACCOUNT_ID}, list(pots))
        if transactions:
            await store("transactions_page", {"account_id": ACCOUNT_ID}, list(transactions))
        async with httpx.AsyncClient(transport=httpx.MockTransport(_no_http)) as http:
            ctx = SyncContext(
                command=command,
                connection=await get_connection(session, connection.id),
                sync_id=sync_id,
                master_key=KEY,
                http=http,
                notifier=notifier or RecordingNotifier(),
                session_factory=async_sessionmaker(bind=session.bind),
            )
            return await normalize(
                ctx, fetched, backfilled=backfilled, first_backfills=first_backfills
            )


async def _account_map(session, connection: ConnectionRecord) -> dict[str, uuid.UUID]:
    account_map = (await get_connection(session, connection.id)).config["account_map"]
    assert isinstance(account_map, dict)
    return {k: uuid.UUID(str(v)) for k, v in account_map.items()}


async def _transactions(session) -> list[Any]:
    return list(
        await session.execute(
            text(
                "select id, external_id, status, description, booking_date, value_date,"
                " counterparty_entity_id from finance.transactions order by occurred_at, id"
            )
        )
    )


async def _postings(session, transaction_id: uuid.UUID) -> list[tuple[uuid.UUID, Decimal, str]]:
    rows = await session.execute(
        text(
            "select account_id, quantity, memo from finance.postings"
            " where transaction_id = :id order by position"
        ),
        {"id": transaction_id},
    )
    return [(r.account_id, r.quantity, r.memo) for r in rows]


async def _unassigned(run) -> uuid.UUID:
    return await run(get_or_create_system_account, "unassigned")


def test_to_quantity_is_exact() -> None:
    assert NORMALIZER_VERSION == "monzo-norm-1"
    assert to_quantity(-1234, 2) == Decimal("-12.34")
    assert to_quantity(5, 0) == Decimal(5)
    assert str(to_quantity(1, 2)) == "0.01"


async def test_creates_accounts_and_pot_children(session, run) -> None:
    connection = await _connection(run)

    await _normalize(session, connection, pots=[make_pot(balance=5000)])

    account_map = await _account_map(session, connection)
    main = await get_account(session, account_map[ACCOUNT_ID])
    pot = await get_account(session, account_map["pot_TEST1"])
    assert (main.name, main.ledger_type, main.subtype, main.balance_mode) == (
        "Monzo",
        "asset",
        "bank",
        "transactions",
    )
    assert (main.source_kind, main.source_connection_id) == ("connection", connection.id)
    assert (pot.name, pot.subtype, pot.balance_mode) == ("Savings", "savings_pot", "snapshots")
    assert pot.parent_account_id == main.id
    assert pot.source_connection_id == connection.id
    snapshots = list(
        await session.execute(
            text("select account_id, quantity, kind from finance.balance_snapshots")
        )
    )
    by_account = {r.account_id: (r.quantity, r.kind) for r in snapshots}
    assert by_account[pot.id] == (Decimal("50.00"), "reported")
    # The main account's reported balance is kept too, for reconciliation views.
    assert by_account[main.id] == (Decimal("250.00"), "reported")


async def test_deleted_pot_is_closed(session, run) -> None:
    connection = await _connection(run)
    await _normalize(session, connection, pots=[make_pot()])

    await _normalize(session, connection, pots=[make_pot(balance=0, deleted=True)])

    pot = await get_account(session, (await _account_map(session, connection))["pot_TEST1"])
    assert pot.status == "closed"


async def test_card_payment_posts_against_unassigned(session, run) -> None:
    connection = await _connection(run)
    payment = make_transaction(amount=-1234, merchant=MERCHANT, notes="weekly shop")

    report = await _normalize(session, connection, transactions=[payment])

    assert report.created == 1
    (tx,) = await _transactions(session)
    assert (tx.external_id, tx.status, tx.description) == ("tx_TEST0001", "booked", "Test Grocer")
    main = (await _account_map(session, connection))[ACCOUNT_ID]
    assert await _postings(session, tx.id) == [
        (main, Decimal("-12.34"), "weekly shop"),
        (await _unassigned(run), Decimal("12.34"), ""),
    ]
    merchant = await session.get_one(Entity, tx.counterparty_entity_id)
    assert (merchant.kind, merchant.name) == ("organization", "Test Grocer")
    assert merchant.external_ids == {"monzo_merchant": "merch_TEST1"}


async def test_description_falls_back_to_counterparty_then_description(session, run) -> None:
    connection = await _connection(run)
    transfer = make_transaction(id="tx_TEST0001", counterparty={"name": "A Friend"})
    plain = make_transaction(id="tx_TEST0002", created="2026-03-01T13:00:00.000Z")

    await _normalize(session, connection, transactions=[transfer, plain])

    rows = await _transactions(session)
    assert [(r.description, r.counterparty_entity_id) for r in rows] == [
        ("A Friend", None),
        ("TEST SHOP LONDON", None),
    ]


async def test_pending_settles_with_new_amount(session, run) -> None:
    connection = await _connection(run)
    pending = make_transaction(amount=-5000, settled="")
    await _normalize(session, connection, transactions=[pending])
    assert [r.status for r in await _transactions(session)] == ["pending"]

    settled = pending | {"amount": -4250, "settled": "2026-03-03T06:00:00.000Z"}
    report = await _normalize(session, connection, transactions=[settled])

    assert (report.created, report.updated) == (0, 1)
    (tx,) = await _transactions(session)
    assert (tx.status, tx.value_date) == ("booked", date(2026, 3, 3))
    main = (await _account_map(session, connection))[ACCOUNT_ID]
    assert (await _postings(session, tx.id))[0][:2] == (main, Decimal("-42.50"))


@pytest.mark.parametrize(
    "reversal", [{"amount": 0}, {"decline_reason": "INSUFFICIENT_FUNDS"}], ids=["zero", "declined"]
)
async def test_reversed_pending_is_voided(session, run, reversal: dict[str, Any]) -> None:
    connection = await _connection(run)
    pending = make_transaction(amount=-5000, settled="")
    await _normalize(session, connection, transactions=[pending])

    report = await _normalize(session, connection, transactions=[pending | reversal])

    assert report.voided == 1
    (tx,) = await _transactions(session)
    assert tx.status == "void"
    # Already void: nothing more to do.
    again = await _normalize(session, connection, transactions=[pending | reversal])
    assert (again.voided, again.unchanged) == (0, 1)


async def test_declined_not_posted(session, run) -> None:
    connection = await _connection(run)
    declined = make_transaction(decline_reason="INSUFFICIENT_FUNDS", settled="")

    report = await _normalize(session, connection, transactions=[declined])

    assert (report.created, report.skipped_declined) == (0, 1)
    assert await _transactions(session) == []
    kept = await session.scalar(
        text("select count(*) from core.raw_records where record_type = 'transactions_page'")
    )
    assert kept == 1


async def test_pot_move_pairs_directly(session, run) -> None:
    connection = await _connection(run)
    move = make_transaction(amount=-5000, description="pot_TEST1", metadata={"pot_id": "pot_TEST1"})

    await _normalize(session, connection, transactions=[move], pots=[make_pot()])

    (tx,) = await _transactions(session)
    account_map = await _account_map(session, connection)
    assert await _postings(session, tx.id) == [
        (account_map[ACCOUNT_ID], Decimal("-50.00"), ""),
        (account_map["pot_TEST1"], Decimal("50.00"), ""),
    ]
    unassigned_postings = await session.scalar(
        text(
            "select count(*) from finance.postings p join finance.accounts a"
            " on a.id = p.account_id where a.name = 'Unassigned'"
        )
    )
    assert unassigned_postings == 0


async def _warnings(session, connection: ConnectionRecord) -> list[str]:
    return (await get_connection(session, connection.id)).warnings


async def test_opening_adjustment_once(session, run) -> None:
    connection = await _connection(run)
    history = [
        make_transaction(id="tx_TEST0001", amount=15000, created="2026-03-01T12:00:00.000Z"),
        make_transaction(id="tx_TEST0002", amount=-5000, created="2026-03-05T12:00:00.000Z"),
    ]
    notifier = RecordingNotifier()
    backfill = {"backfilled": [ACCOUNT_ID], "first_backfills": [ACCOUNT_ID]}

    first = await _normalize(
        session, connection, transactions=history, balance=25000, notifier=notifier, **backfill
    )

    assert first.mismatches == []
    opening = [r for r in await _transactions(session) if r.external_id == f"opening:{ACCOUNT_ID}"]
    (opening_tx,) = opening
    assert (opening_tx.status, opening_tx.description, opening_tx.booking_date) == (
        "booked",
        "Opening balance adjustment",
        date(2026, 2, 28),
    )
    main = (await _account_map(session, connection))[ACCOUNT_ID]
    opening_balances = await run(get_or_create_system_account, "opening_balances")
    assert await _postings(session, opening_tx.id) == [
        (main, Decimal("150.00"), ""),
        (opening_balances, Decimal("-150.00"), ""),
    ]

    second = await _normalize(
        session,
        connection,
        transactions=history,
        balance=26000,
        notifier=notifier,
        backfilled=[ACCOUNT_ID],
    )

    assert len(await _transactions(session)) == 3  # nothing new posted
    assert len(second.mismatches) == 1
    (warning,) = await _warnings(session, connection)
    assert "reconciliation mismatch" in warning
    assert "10.00" in warning
    assert len(notifier.messages) == 1
    assert "10.00" in notifier.messages[0]


async def test_mismatch_notifies_once_per_value_and_clears(session, run) -> None:
    connection = await _connection(run)
    notifier = RecordingNotifier()
    history = [make_transaction(amount=25000)]
    await _normalize(
        session,
        connection,
        transactions=history,
        notifier=notifier,
        backfilled=[ACCOUNT_ID],
        first_backfills=[ACCOUNT_ID],
    )

    async def reconcile(balance: int) -> NormalizeReport:
        return await _normalize(
            session,
            connection,
            transactions=history,
            balance=balance,
            notifier=notifier,
            backfilled=[ACCOUNT_ID],
        )

    await reconcile(26000)
    await reconcile(26000)
    assert len(notifier.messages) == 1
    await reconcile(27000)
    assert len(notifier.messages) == 2
    (warning,) = await _warnings(session, connection)
    assert "20.00" in warning

    report = await reconcile(25000)

    assert report.mismatches == []
    assert await _warnings(session, connection) == []
    assert len(notifier.messages) == 2
    # Matching again resets the memory: the same difference later is news again.
    await reconcile(26000)
    assert len(notifier.messages) == 3


async def test_reconciliation_keeps_other_warnings(session, run) -> None:
    connection = await _connection(run)
    await _normalize(session, connection, backfilled=[ACCOUNT_ID], first_backfills=[ACCOUNT_ID])
    async with command_context(session, CONNECTOR, "test", {}) as command:
        await set_warnings(command, connection.id, ["history may be truncated"])

    await _normalize(session, connection, balance=26000, backfilled=[ACCOUNT_ID])
    await _normalize(session, connection, balance=25000, backfilled=[ACCOUNT_ID])

    assert await _warnings(session, connection) == ["history may be truncated"]


async def test_not_backfilled_account_is_not_reconciled(session, run) -> None:
    connection = await _connection(run)
    notifier = RecordingNotifier()

    report = await _normalize(session, connection, balance=99900, notifier=notifier)

    assert report.mismatches == []
    assert notifier.messages == []
    assert await _transactions(session) == []


async def test_normalize_is_idempotent(session, run) -> None:
    connection = await _connection(run)
    history = [
        make_transaction(id="tx_TEST0001", merchant=MERCHANT),
        make_transaction(id="tx_TEST0002", amount=-700, settled=""),
        make_transaction(id="tx_TEST0003", amount=-5000, metadata={"pot_id": "pot_TEST1"}),
    ]
    run_args: dict[str, Any] = {
        "transactions": history,
        "pots": [make_pot()],
        "backfilled": [ACCOUNT_ID],
        "first_backfills": [ACCOUNT_ID],
    }
    first = await _normalize(session, connection, **run_args)
    assert first.created == 4  # three transactions and the opening adjustment
    snapshots = await session.scalar(text("select count(*) from finance.balance_snapshots"))
    events = await session.scalar(text("select count(*) from core.events"))

    second = await _normalize(session, connection, **run_args)

    assert second == NormalizeReport(
        created=0, updated=0, unchanged=3, voided=0, skipped_declined=0, mismatches=[]
    )
    assert await session.scalar(text("select count(*) from finance.balance_snapshots")) == snapshots
    assert await session.scalar(text("select count(*) from core.events")) == events


async def test_booking_date_uses_local_date(session, run) -> None:
    connection = await _connection(run)
    late = make_transaction(created="2026-03-29T23:30:00.000Z", settled="")

    await _normalize(session, connection, transactions=[late])

    (tx,) = await _transactions(session)
    assert (tx.booking_date, tx.value_date) == (date(2026, 3, 30), None)
