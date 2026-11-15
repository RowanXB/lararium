import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import MAX_PREC, Decimal, localcontext
from typing import Any

import httpx
import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from lararium.core.actors import Actor
from lararium.core.commands import CommandContext, command_context
from lararium.core.connections import (
    ConnectionRecord,
    create_connection,
    get_connection,
    set_warnings,
    update_config,
)
from lararium.core.connectors import SyncContext
from lararium.core.json_types import JSONValue
from lararium.core.models import Entity, RawRecord
from lararium.core.raw_records import _content_hash, store_raw_record
from lararium.modules.finance.accounts import get_account
from lararium.modules.finance.instruments import get_currency
from lararium.modules.finance.ledger import (
    PostingInput,
    TransactionInput,
    post_transaction,
    void_transaction,
)
from lararium.modules.finance.monzo.normalize import (
    _ISO_TIME,  # pyright: ignore[reportPrivateUsage]
    NORMALIZER_VERSION,
    NORMALIZER_VERSION_KEY,
    FetchedRecord,
    MonzoFieldError,
    NormalizeReport,
    _readable_time,  # pyright: ignore[reportPrivateUsage]
    normalize,
    to_quantity,
)
from lararium.modules.finance.system_accounts import get_or_create_system_account
from lararium.modules.finance.valuation import balances_asof

from .fixtures import (
    ACCOUNT_ID,
    make_account,
    make_balance,
    make_merchant,
    make_pot,
    make_transaction,
)

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
    reconciled: Sequence[str] = (),
    openings: Sequence[str] = (),
    notifier: RecordingNotifier | None = None,
    account: dict[str, Any] | None = None,
    balance_record: dict[str, Any] | None = None,
) -> NormalizeReport:
    """Store one sync's raw records (as MonzoConnector.sync would), then normalize them.
    `balance_record`, when given, is the balance response in place of one of `balance`."""
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
            fetched.append(FetchedRecord(record_id, request, datetime.now(UTC)))

        await store("accounts", {}, [account or make_account()])
        await store(
            "balance",
            {"account_id": ACCOUNT_ID},
            make_balance(balance=balance) if balance_record is None else balance_record,
        )
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
            return await normalize(ctx, fetched, reconciled=reconciled, openings=openings)


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
    assert NORMALIZER_VERSION == "monzo-norm-2"
    assert to_quantity(-1234, 2) == Decimal("-12.34")
    assert to_quantity(5, 0) == Decimal(5)
    assert str(to_quantity(1, 2)) == "0.01"
    # Beyond the 28 digits Decimal keeps by default: still exact.
    assert to_quantity(10**30 + 1, 2) == Decimal("10000000000000000000000000000.01")


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
    assert (pot.name, pot.subtype, pot.balance_mode) == ("Savings", "savings_pot", "hybrid")
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


def _iso(value: datetime) -> str:
    return f"{value:%Y-%m-%dT%H:%M:%S}.{value.microsecond // 1000:03d}Z"


async def test_pot_history_before_first_snapshot(session, run) -> None:
    connection = await _connection(run)
    now = datetime.now(UTC)
    history = [
        make_transaction(id="tx_TEST0001", amount=30000, created=_iso(now - timedelta(days=20))),
        make_transaction(
            id="tx_TEST0002",
            amount=-5000,
            created=_iso(now - timedelta(days=10)),
            metadata={"pot_id": "pot_TEST1"},
        ),
    ]

    await _normalize(
        session,
        connection,
        transactions=history,
        balance=25000,
        pots=[make_pot(balance=5000)],
        reconciled=[ACCOUNT_ID],
        openings=[ACCOUNT_ID],
    )

    account_map = await _account_map(session, connection)
    main, pot = account_map[ACCOUNT_ID], account_map["pot_TEST1"]

    async def on(day: date) -> dict[uuid.UUID, Decimal]:
        rows = await balances_asof(session, day)
        return {r.account_id: r.quantity for r in rows if r.account_id in (main, pot)}

    today = now.date()
    before_move = await on(today - timedelta(days=15))
    after_move = await on(today - timedelta(days=5))
    assert sum(before_move.values()) == Decimal("300.00")
    # The pot move only moves money: before the pot's first snapshot, its moves value it.
    assert sum(after_move.values()) == Decimal("300.00")
    assert after_move[pot] == Decimal("50.00")
    current = await on(today + timedelta(days=1))
    assert current[pot] == Decimal("50.00")  # the snapshot, the move not counted twice


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
    backfill = {"reconciled": [ACCOUNT_ID], "openings": [ACCOUNT_ID]}

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
        reconciled=[ACCOUNT_ID],
    )

    assert len(await _transactions(session)) == 3  # nothing new posted
    assert len(second.mismatches) == 1
    (warning,) = await _warnings(session, connection)
    assert "reconciliation mismatch" in warning
    assert "10.00" in warning
    assert len(notifier.messages) == 1
    assert "10.00" in notifier.messages[0]


async def test_opening_recomputed_in_place(session, run) -> None:
    connection = await _connection(run)
    recent = [make_transaction(id="tx_TEST0002", amount=-5000, created="2026-03-05T12:00:00.000Z")]
    await _normalize(session, connection, transactions=recent, openings=[ACCOUNT_ID])
    older = make_transaction(id="tx_TEST0001", amount=15000, created="2026-01-10T12:00:00.000Z")

    # A later backfill brings older history: the opening shrinks and moves before it.
    report = await _normalize(
        session, connection, transactions=[older, *recent], openings=[ACCOUNT_ID]
    )

    assert report.mismatches == []
    (opening,) = [
        r for r in await _transactions(session) if r.external_id == f"opening:{ACCOUNT_ID}"
    ]
    assert (opening.status, opening.booking_date) == ("booked", date(2026, 1, 9))
    main = (await _account_map(session, connection))[ACCOUNT_ID]
    assert (await _postings(session, opening.id))[0] == (main, Decimal("150.00"), "")

    # A backfill that fetches the rest of the older history: it adds up on its own, and the
    # opening is no longer needed.
    oldest = make_transaction(id="tx_TEST0000", amount=15000, created="2026-01-01T12:00:00.000Z")
    report = await _normalize(
        session, connection, transactions=[oldest, older, *recent], openings=[ACCOUNT_ID]
    )
    assert report.voided == 1
    assert report.mismatches == []
    statuses = {r.external_id: r.status for r in await _transactions(session)}
    assert statuses[f"opening:{ACCOUNT_ID}"] == "void"


async def test_a_backfill_does_not_absorb_what_the_owner_voided(session, run) -> None:
    connection = await _connection(run)
    deposit = make_transaction(id="tx_TEST0001", amount=10000, created="2026-02-01T12:00:00.000Z")
    payment = make_transaction(id="tx_TEST0002", amount=-5000, created="2026-02-20T12:00:00.000Z")
    history = [deposit, payment]
    await _normalize(session, connection, transactions=history, openings=[ACCOUNT_ID])
    (posted,) = [r for r in await _transactions(session) if r.external_id == "tx_TEST0002"]
    async with command_context(session, Actor("user", "owner"), "void_transaction", {}) as ctx:
        await void_transaction(ctx, posted.id)

    # Authorised again: the backfill fetches the same history. The void is a difference
    # Monzo doesn't know about, so it stays a mismatch rather than moving the opening.
    report = await _normalize(session, connection, transactions=history, openings=[ACCOUNT_ID])

    (opening,) = [
        r for r in await _transactions(session) if r.external_id == f"opening:{ACCOUNT_ID}"
    ]
    main = (await _account_map(session, connection))[ACCOUNT_ID]
    assert (await _postings(session, opening.id))[0] == (main, Decimal("200.00"), "")
    (mismatch,) = report.mismatches
    assert "difference -50.00 GBP" in mismatch


async def test_opening_voided_by_the_owner_stays_void(session, run) -> None:
    connection = await _connection(run)
    history = [make_transaction(amount=15000)]
    await _normalize(session, connection, transactions=history, openings=[ACCOUNT_ID])
    (opening,) = [
        r for r in await _transactions(session) if r.external_id == f"opening:{ACCOUNT_ID}"
    ]
    async with command_context(session, Actor("user", "owner"), "void_transaction", {}) as ctx:
        await void_transaction(ctx, opening.id)

    report = await _normalize(
        session, connection, transactions=history, balance=30000, openings=[ACCOUNT_ID]
    )

    statuses = {r.external_id: r.status for r in await _transactions(session)}
    assert statuses[f"opening:{ACCOUNT_ID}"] == "void"
    # Still reconciled: the owner's choice shows as a mismatch, never silently undone.
    assert len(report.mismatches) == 1


async def test_mismatch_notifies_once_per_value_and_clears(session, run) -> None:
    connection = await _connection(run)
    notifier = RecordingNotifier()
    history = [make_transaction(amount=25000)]
    await _normalize(
        session,
        connection,
        transactions=history,
        notifier=notifier,
        reconciled=[ACCOUNT_ID],
        openings=[ACCOUNT_ID],
    )

    async def reconcile(balance: int) -> NormalizeReport:
        return await _normalize(
            session,
            connection,
            transactions=history,
            balance=balance,
            notifier=notifier,
            reconciled=[ACCOUNT_ID],
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


async def test_reconciliation_counts_manual_postings(session, run) -> None:
    connection = await _connection(run)
    history = [make_transaction(id="tx_TEST0001", amount=15000, created="2026-03-01T12:00:00.000Z")]
    await _normalize(session, connection)  # creates the accounts, with no history yet
    main = (await _account_map(session, connection))[ACCOUNT_ID]
    unassigned = await _unassigned(run)
    gbp = await get_currency(session, "GBP")

    async def post_manual(amount: str) -> None:
        await run(
            post_transaction,
            TransactionInput(
                occurred_at=datetime(2026, 3, 2, 12, tzinfo=UTC),
                description="Manual",
                postings=[
                    PostingInput(main, gbp.id, Decimal(amount)),
                    PostingInput(unassigned, gbp.id, -Decimal(amount)),
                ],
            ),
        )

    await post_manual("10.00")
    # The opening absorbs the manual posting (balance 250 = 150 + 10 + opening 90)...
    report = await _normalize(
        session, connection, transactions=history, reconciled=[ACCOUNT_ID], openings=[ACCOUNT_ID]
    )
    assert report.mismatches == []
    # ...and reconciling again, opening recomputed or not, must still count it.
    report = await _normalize(
        session, connection, transactions=history, reconciled=[ACCOUNT_ID], openings=[ACCOUNT_ID]
    )
    assert report.mismatches == []
    assert await _warnings(session, connection) == []

    await post_manual("5.00")  # a later manual posting is reported exactly
    report = await _normalize(session, connection, transactions=history, reconciled=[ACCOUNT_ID])

    assert len(report.mismatches) == 1
    (warning,) = await _warnings(session, connection)
    assert "(difference -5.00 GBP)" in warning


async def _opening(session) -> tuple[Decimal, date]:
    row = (
        await session.execute(
            text(
                "select p.quantity, t.booking_date from finance.transactions t"
                " join finance.postings p on p.transaction_id = t.id and p.position = 0"
                f" where t.external_id = 'opening:{ACCOUNT_ID}' and t.status <> 'void'"
            )
        )
    ).one()
    return row.quantity, row.booking_date


@pytest.mark.parametrize(
    ("occurred_at", "booking_date"),
    [
        # Dated before the opening: still not history a backfill fetched.
        (datetime(2025, 6, 1, 12, tzinfo=UTC), None),
        # Booked years before it occurred: the opening is placed by the time things occur.
        (datetime(2026, 4, 1, 12, tzinfo=UTC), date(2020, 1, 1)),
    ],
    ids=["dated-earlier", "booked-earlier"],
)
async def test_backfills_never_absorb_a_manual_posting(
    session, run, occurred_at: datetime, booking_date: date | None
) -> None:
    connection = await _connection(run)
    history = [
        make_transaction(id="tx_TEST0001", amount=15000, created="2026-03-01T12:00:00.000Z"),
        make_transaction(id="tx_TEST0002", amount=-5000, created="2026-03-05T12:00:00.000Z"),
    ]
    await _normalize(session, connection, transactions=history, openings=[ACCOUNT_ID])
    main = (await _account_map(session, connection))[ACCOUNT_ID]
    unassigned = await _unassigned(run)
    gbp = await get_currency(session, "GBP")
    await run(
        post_transaction,
        TransactionInput(
            occurred_at=occurred_at,
            booking_date=booking_date,
            description="Manual",
            postings=[
                PostingInput(main, gbp.id, Decimal("30.00")),
                PostingInput(unassigned, gbp.id, Decimal("-30.00")),
            ],
        ),
    )

    # Authorised again, three times: the same history each time.
    for _ in range(3):
        report = await _normalize(session, connection, transactions=history, openings=[ACCOUNT_ID])
        assert (await _opening(session))[0] == Decimal("150.00")
        (mismatch,) = report.mismatches
        assert "difference -30.00 GBP" in mismatch


async def test_a_backfill_after_no_opening_was_needed_absorbs_nothing(session, run) -> None:
    # The first backfill adds up exactly (as on the real account): no opening is posted.
    connection = await _connection(run)
    history = [
        make_transaction(id="tx_TEST0001", amount=30000, created="2026-03-01T12:00:00.000Z"),
        make_transaction(id="tx_TEST0002", amount=-5000, created="2026-03-05T12:00:00.000Z"),
    ]
    report = await _normalize(session, connection, transactions=history, openings=[ACCOUNT_ID])
    assert report.mismatches == []
    (payment,) = [r for r in await _transactions(session) if r.external_id == "tx_TEST0002"]
    async with command_context(session, Actor("user", "owner"), "void_transaction", {}) as ctx:
        await void_transaction(ctx, payment.id)

    # Authorised again: the same history. The void is the owner's, not older history.
    report = await _normalize(session, connection, transactions=history, openings=[ACCOUNT_ID])

    assert not [r for r in await _transactions(session) if r.external_id.startswith("opening:")]
    (mismatch,) = report.mismatches
    assert "difference -50.00 GBP" in mismatch


@pytest.mark.parametrize("posted", [True, False], ids=["with-opening", "no-opening-needed"])
async def test_an_account_from_before_boundaries_absorbs_nothing(
    session, run, posted: bool
) -> None:
    # Reconciled before boundaries were recorded; its first sync after the upgrade is a
    # re-authorisation's backfill.
    connection = await _connection(run)
    history = [
        make_transaction(
            id="tx_TEST0001", amount=10000 if posted else 30000, created="2026-03-01T12:00:00.000Z"
        ),
        make_transaction(id="tx_TEST0002", amount=-5000, created="2026-03-05T12:00:00.000Z"),
    ]
    await _normalize(session, connection, transactions=history, openings=[ACCOUNT_ID])
    await run(update_config, connection.id, remove=["opening_boundaries"])
    (payment,) = [r for r in await _transactions(session) if r.external_id == "tx_TEST0002"]
    async with command_context(session, Actor("user", "owner"), "void_transaction", {}) as ctx:
        await void_transaction(ctx, payment.id)

    report = await _normalize(session, connection, transactions=history, openings=[ACCOUNT_ID])

    openings = [r for r in await _transactions(session) if r.external_id.startswith("opening:")]
    if posted:
        assert (await _opening(session))[0] == Decimal("200.00")
    else:
        assert openings == []
    (mismatch,) = report.mismatches
    assert "difference -50.00 GBP" in mismatch


async def test_older_history_fetched_later_corrects_the_opening_past_a_manual_posting(
    session, run
) -> None:
    connection = await _connection(run)
    first = make_transaction(id="tx_TEST0002", amount=15000, created="2026-03-01T12:00:00.000Z")
    await _normalize(session, connection)  # creates the accounts, with no history yet
    main = (await _account_map(session, connection))[ACCOUNT_ID]
    unassigned = await _unassigned(run)
    gbp = await get_currency(session, "GBP")
    # A manual posting dated before all of Monzo's history, then the first opening.
    await run(
        post_transaction,
        TransactionInput(
            occurred_at=datetime(2026, 1, 1, 12, tzinfo=UTC),
            description="Manual",
            postings=[
                PostingInput(main, gbp.id, Decimal("30.00")),
                PostingInput(unassigned, gbp.id, Decimal("-30.00")),
            ],
        ),
    )
    await _normalize(session, connection, transactions=[first], openings=[ACCOUNT_ID])
    assert (await _opening(session))[0] == Decimal("70.00")

    # A later backfill fetches older Monzo history, dated between the two.
    older = make_transaction(id="tx_TEST0001", amount=4000, created="2026-02-01T12:00:00.000Z")
    report = await _normalize(
        session, connection, transactions=[older, first], openings=[ACCOUNT_ID]
    )

    assert (await _opening(session))[0] == Decimal("30.00")
    assert report.mismatches == []


async def test_a_reversed_pending_payment_presented_again_is_posted(session, run) -> None:
    connection = await _connection(run)
    pending = make_transaction(amount=-5000, settled="")
    await _normalize(session, connection, transactions=[pending])
    await _normalize(session, connection, transactions=[pending | {"amount": 0}])

    # The capture arrives under the same id: the connector's own void gives way.
    settled = pending | {"amount": -4500, "settled": "2026-03-02T06:00:00.000Z"}
    await _normalize(session, connection, transactions=[settled])

    (tx,) = await _transactions(session)
    assert tx.status == "booked"
    main = (await _account_map(session, connection))[ACCOUNT_ID]
    assert (await _postings(session, tx.id))[0][:2] == (main, Decimal("-45.00"))


async def test_an_opening_the_connector_voided_is_posted_again_when_needed(session, run) -> None:
    connection = await _connection(run)
    recent = make_transaction(id="tx_TEST0002", amount=10000, created="2026-03-05T12:00:00.000Z")
    # 250.00 against 100.00 of history: an opening of 150.00.
    await _normalize(session, connection, transactions=[recent], openings=[ACCOUNT_ID])
    # A backfill finds older history that explains it all: the connector voids it.
    older = make_transaction(id="tx_TEST0001", amount=15000, created="2026-03-01T12:00:00.000Z")
    report = await _normalize(
        session, connection, transactions=[older, recent], openings=[ACCOUNT_ID]
    )
    assert report.voided == 1
    # Even older history in the other direction: an opening is needed again.
    oldest = make_transaction(id="tx_TEST0000", amount=-2000, created="2026-02-01T12:00:00.000Z")
    report = await _normalize(
        session, connection, transactions=[oldest, older, recent], openings=[ACCOUNT_ID]
    )

    assert (await _opening(session))[0] == Decimal("20.00")
    assert report.mismatches == []


async def test_a_reversed_then_settled_earliest_transaction_leaves_the_opening_alone(
    session, run
) -> None:
    connection = await _connection(run)
    first = make_transaction(
        id="tx_TEST0001", amount=10000, settled="", created="2026-03-01T12:00:00.000Z"
    )
    later = make_transaction(id="tx_TEST0002", amount=5000, created="2026-03-05T12:00:00.000Z")
    # 250.00 against 150.00 of history: an opening of 100.00.
    await _normalize(session, connection, transactions=[first, later], openings=[ACCOUNT_ID])

    # The earliest transaction comes back reversed (the connector voids it)...
    report = await _normalize(
        session,
        connection,
        transactions=[first | {"amount": 0}, later],
        balance=15000,
        openings=[ACCOUNT_ID],
    )
    assert report.mismatches == []
    # ...and then settled after all, under the same id.
    settled = first | {"settled": "2026-03-02T06:00:00.000Z"}
    report = await _normalize(
        session, connection, transactions=[settled, later], openings=[ACCOUNT_ID]
    )

    assert (await _opening(session))[0] == Decimal("100.00")
    assert report.mismatches == []


async def test_reconciliation_keeps_other_warnings(session, run) -> None:
    connection = await _connection(run)
    await _normalize(session, connection, reconciled=[ACCOUNT_ID], openings=[ACCOUNT_ID])
    async with command_context(session, CONNECTOR, "test", {}) as command:
        await set_warnings(command, connection.id, ["history may be truncated"])

    await _normalize(session, connection, balance=26000, reconciled=[ACCOUNT_ID])
    await _normalize(session, connection, balance=25000, reconciled=[ACCOUNT_ID])

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
        "reconciled": [ACCOUNT_ID],
        "openings": [ACCOUNT_ID],
    }
    first = await _normalize(session, connection, **run_args)
    assert first.created == 4  # three transactions and the opening adjustment
    snapshots = await session.scalar(text("select count(*) from finance.balance_snapshots"))
    events = await session.scalar(text("select count(*) from core.events"))

    second = await _normalize(session, connection, **run_args)

    # The three transactions, and the opening recomputed to the same amount.
    assert second == NormalizeReport(
        created=0, updated=0, unchanged=4, voided=0, skipped_declined=0, mismatches=[]
    )
    assert await session.scalar(text("select count(*) from finance.balance_snapshots")) == snapshots
    assert await session.scalar(text("select count(*) from core.events")) == events


async def test_booking_date_uses_local_date(session, run) -> None:
    connection = await _connection(run)
    late = make_transaction(created="2026-03-29T23:30:00.000Z", settled="")

    await _normalize(session, connection, transactions=[late])

    (tx,) = await _transactions(session)
    assert (tx.booking_date, tx.value_date) == (date(2026, 3, 30), None)


# Typed source fields (plan Decisions 5-11).

UNMAPPED = "Monzo field {} is not stored yet; it stays in the raw records"


async def _details(session, transaction_id: uuid.UUID) -> Any:
    return (
        await session.execute(
            text(
                "select original_quantity, original_instrument_id, category, labels,"
                " include_in_spending, payment_scheme, place_entity_id"
                " from finance.transactions where id = :id"
            ),
            {"id": transaction_id},
        )
    ).one()


async def _splits(session, transaction_id: uuid.UUID) -> list[tuple[str, Decimal]]:
    rows = await session.execute(
        text(
            "select category, quantity from finance.transaction_category_splits"
            " where transaction_id = :id order by position"
        ),
        {"id": transaction_id},
    )
    return [(r.category, r.quantity) for r in rows]


async def _declined(session) -> list[Any]:
    return list(
        await session.execute(
            text("select * from finance.declined_transactions order by occurred_at, id")
        )
    )


async def _rows(session, table: str) -> list[str]:
    """Every row of the table as JSON text, in a stable order."""
    return list(
        await session.scalars(text(f"select row_to_json(t)::text from {table} t order by 1"))
    )


async def test_transaction_details_are_stored(session, run) -> None:
    connection = await _connection(run)
    payment = make_transaction(
        amount=-850,
        local_amount=-1000,
        local_currency="USD",
        category="groceries",
        labels=["test-label"],
        scheme="mastercard",
        include_in_spending=True,
        merchant=make_merchant(),
        metadata={"mcc": "5411", "card_acceptor_website": "https://pay.test-grocer.example"},
    )

    await _normalize(session, connection, transactions=[payment])

    (tx,) = await _transactions(session)
    details = await _details(session, tx.id)
    usd = await get_currency(session, "USD")
    assert (details.original_quantity, details.original_instrument_id) == (
        Decimal("-10.00"),
        usd.id,
    )
    assert (
        details.category,
        details.labels,
        details.include_in_spending,
        details.payment_scheme,
    ) == ("groceries", ["test-label"], True, "mastercard")
    organization = await session.get_one(Entity, tx.counterparty_entity_id)
    assert (organization.kind, organization.name) == ("organization", "Test Grocer")
    place = await session.get_one(Entity, details.place_entity_id)
    assert (place.kind, place.name, place.external_ids) == (
        "place",
        "Test Grocer",
        {"monzo_merchant": "merch_TEST1"},
    )
    assert (
        place.address_street,
        place.address_city,
        place.address_region,
        place.address_postcode,
        place.address_country,
        place.address_formatted,
    ) == (
        "1 Test Street",
        "Testford",
        "Testshire",
        "TE1 1ST",
        "GBR",
        "1 Test Street, Testford TE1 1ST",
    )
    assert place.latitude == Decimal("51.5")
    # Exact: built from the number's shortest form, not from its binary fraction.
    assert str(place.longitude) == "-0.1278"
    assert (place.location_approximate, place.online, place.mcc) == (False, False, "5411")
    assert (place.website, place.card_acceptor_website, place.logo_url) == (
        "https://test-grocer.example",
        "https://pay.test-grocer.example",
        "https://logos.example/test-grocer.png",
    )
    assert await _warnings(session, connection) == []


async def test_place_gets_details_a_later_transaction_states(session, run) -> None:
    connection = await _connection(run)
    payments = [
        make_transaction(id="tx_TEST0001", merchant=make_merchant()),
        make_transaction(
            id="tx_TEST0002",
            merchant=make_merchant(),
            metadata={"mcc": "5411"},
            created="2026-03-01T13:00:00.000Z",
        ),
    ]

    await _normalize(session, connection, transactions=payments)
    first, second = await _transactions(session)
    place_id = (await _details(session, first.id)).place_entity_id
    assert (await _details(session, second.id)).place_entity_id == place_id
    place = await session.get_one(Entity, place_id, populate_existing=True)
    assert place.mcc == "5411"

    changes_before = await session.scalar(
        text("select count(*) from core.audit_log where changes ? :key"),
        {"key": f"entity:{place_id}"},
    )
    again = await _normalize(session, connection, transactions=payments)

    assert again == NormalizeReport(unchanged=2)
    assert changes_before == await session.scalar(
        text("select count(*) from core.audit_log where changes ? :key"),
        {"key": f"entity:{place_id}"},
    )


async def test_two_merchant_ids_one_name_two_places(session, run) -> None:
    connection = await _connection(run)
    town = make_merchant(id="merch_TEST1", name="Test Cafe")
    station = make_merchant(
        id="merch_TEST2",
        name="Test Cafe",
        online=True,
        address=make_merchant()["address"] | {"address": "2 Station Road"},
    )
    payments = [
        make_transaction(id="tx_TEST0001", merchant=town),
        make_transaction(id="tx_TEST0002", merchant=station, created="2026-03-01T13:00:00.000Z"),
    ]

    await _normalize(session, connection, transactions=payments)

    first, second = await _transactions(session)
    assert first.counterparty_entity_id == second.counterparty_entity_id
    places = [
        await session.get_one(Entity, (await _details(session, r.id)).place_entity_id)
        for r in (first, second)
    ]
    assert [
        (p.kind, p.name, p.external_ids["monzo_merchant"], p.address_street, p.online)
        for p in places
    ] == [
        ("place", "Test Cafe", "merch_TEST1", "1 Test Street", False),
        ("place", "Test Cafe", "merch_TEST2", "2 Station Road", True),
    ]
    organizations = await session.scalar(
        text(
            "select count(*) from core.entities where kind = 'organization' and name = 'Test Cafe'"
        )
    )
    assert organizations == 1


async def test_cash_deposit_fee_is_a_separate_posting(session, run) -> None:
    connection = await _connection(run)
    deposit = make_transaction(
        amount=12300, notes="cash in", fees={"deposit.cash": -100, "unused.fee": 0}
    )

    report = await _normalize(
        session,
        connection,
        transactions=[deposit],
        balance=12300,
        reconciled=[ACCOUNT_ID],
        openings=[ACCOUNT_ID],
    )

    assert report.mismatches == []
    (tx,) = await _transactions(session)  # and no opening: the ledger agrees with Monzo
    main = (await _account_map(session, connection))[ACCOUNT_ID]
    rows = await session.execute(
        text(
            "select account_id, quantity, is_fee, memo from finance.postings"
            " where transaction_id = :id order by position"
        ),
        {"id": tx.id},
    )
    assert [tuple(r) for r in rows] == [
        (main, Decimal("124.00"), False, "cash in"),
        (await _unassigned(run), Decimal("-123.00"), False, ""),
        (main, Decimal("-1.00"), True, "deposit.cash"),
    ]
    balance = await session.scalar(
        text("select sum(quantity) from finance.postings where account_id = :id"), {"id": main}
    )
    assert balance == Decimal("123.00")


async def test_non_string_category_does_not_fail_the_sync(session, run) -> None:
    connection = await _connection(run)
    payment = make_transaction(category={"id": "x"}, categories={"groceries": -1234})

    await _normalize(session, connection, transactions=[payment])

    (posted,) = await _transactions(session)
    assert (await _details(session, posted.id)).category is None
    # Not derivable from a category it cannot read, so the split is kept.
    assert await _splits(session, posted.id) == [("groceries", Decimal("-12.34"))]
    assert await _warnings(session, connection) == [UNMAPPED.format("category")]


async def test_split_categories_stored_only_when_split(session, run) -> None:
    connection = await _connection(run)

    def payment(n: int, amount: int, categories: Any) -> dict[str, Any]:
        return make_transaction(
            id=f"tx_TEST000{n}",
            created=f"2026-03-01T12:0{n}:00.000Z",
            amount=amount,
            category="groceries",
            categories=categories,
        )

    payments = [
        payment(1, -1234, {"groceries": -1234}),  # the category itself: derivable
        payment(2, -3000, {"household": -1000, "groceries": -2000}),
        payment(3, -1500, {"shopping": -1500}),
        payment(4, -1500, {"groceries": -1000}),
        payment(5, -700, None),
    ]

    await _normalize(session, connection, transactions=payments)

    assert [await _splits(session, r.id) for r in await _transactions(session)] == [
        [],
        [("groceries", Decimal("-20.00")), ("household", Decimal("-10.00"))],
        [("shopping", Decimal("-15.00"))],
        [("groceries", Decimal("-10.00"))],
        [],
    ]


async def test_declined_recorded_not_posted(session, run) -> None:
    connection = await _connection(run)
    declined = make_transaction(
        amount=-2500, settled="", decline_reason="INSUFFICIENT_FUNDS", merchant=make_merchant()
    )

    report = await _normalize(session, connection, transactions=[declined])

    assert (report.created, report.skipped_declined) == (0, 1)
    assert await _transactions(session) == []
    (row,) = await _declined(session)
    main = (await _account_map(session, connection))[ACCOUNT_ID]
    gbp = await get_currency(session, "GBP")
    assert (
        row.connection_id,
        row.external_id,
        row.account_id,
        row.occurred_at,
        row.quantity,
        row.instrument_id,
        row.description,
        row.decline_reason,
    ) == (
        connection.id,
        "tx_TEST0001",
        main,
        datetime(2026, 3, 1, 12, tzinfo=UTC),
        Decimal("-25.00"),
        gbp.id,
        "Test Grocer",
        "INSUFFICIENT_FUNDS",
    )
    organization = await session.get_one(Entity, row.counterparty_entity_id)
    place = await session.get_one(Entity, row.place_entity_id)
    assert (organization.kind, organization.name) == ("organization", "Test Grocer")
    assert (place.kind, place.external_ids) == ("place", {"monzo_merchant": "merch_TEST1"})


async def test_pending_then_declined_is_voided_and_recorded(session, run) -> None:
    connection = await _connection(run)
    pending = make_transaction(amount=-5000, settled="")
    await _normalize(session, connection, transactions=[pending])

    report = await _normalize(
        session, connection, transactions=[pending | {"decline_reason": "CARD_INACTIVE"}]
    )

    assert (report.voided, report.skipped_declined) == (1, 0)
    (tx,) = await _transactions(session)
    assert tx.status == "void"
    (row,) = await _declined(session)
    assert (row.external_id, row.quantity, row.decline_reason) == (
        "tx_TEST0001",
        Decimal("-50.00"),
        "CARD_INACTIVE",
    )


async def test_account_and_pot_details(session, run) -> None:
    connection = await _connection(run)
    pot = make_pot(
        type="flexible_savings",
        isa_wrapper="ISA",
        goal_amount=100000,
        locked=True,
        round_up=True,
        round_up_multiplier=2,
    )

    await _normalize(session, connection, pots=[pot])

    account_map = await _account_map(session, connection)
    main = await get_account(session, account_map[ACCOUNT_ID])
    saved = await get_account(session, account_map["pot_TEST1"])
    assert (main.product_type, main.owner_type, main.tax_wrapper) == (
        "standard",
        "personal",
        "none",
    )
    assert (
        saved.product_type,
        saved.goal_amount,
        saved.locked,
        saved.round_up,
        saved.round_up_multiplier,
        saved.tax_wrapper,
    ) == ("flexible_savings", Decimal("1000.00"), True, True, Decimal(2), "isa")

    # Already mapped: what Monzo says now is written all the same, absence included.
    await _normalize(
        session,
        connection,
        account=make_account(product_type="", owner_type="business"),
        pots=[make_pot(isa_wrapper="", locked=False, round_up=False)],
    )

    main = await get_account(session, account_map[ACCOUNT_ID])
    saved = await get_account(session, account_map["pot_TEST1"])
    assert (main.product_type, main.owner_type) == (None, "business")
    assert (
        saved.goal_amount,
        saved.locked,
        saved.round_up,
        saved.round_up_multiplier,
        saved.tax_wrapper,
    ) == (None, False, False, None, "none")


@pytest.mark.parametrize("isa_wrapper", ["JISA", 7], ids=["unknown-name", "number"])
async def test_unknown_isa_wrapper_warns(session, run, isa_wrapper: object) -> None:
    connection = await _connection(run)
    await _normalize(session, connection, pots=[make_pot(isa_wrapper="ISA")])

    # The sync completes; the pot keeps the tax wrapper it had.
    await _normalize(session, connection, pots=[make_pot(isa_wrapper=isa_wrapper)])

    pot = await get_account(session, (await _account_map(session, connection))["pot_TEST1"])
    assert pot.tax_wrapper == "isa"
    assert await _warnings(session, connection) == [UNMAPPED.format("pot.isa_wrapper")]


@pytest.mark.parametrize("isa_wrapper", ["missing", None], ids=["missing", "null"])
async def test_pot_without_isa_wrapper_keeps_its_tax_wrapper(
    session, run, isa_wrapper: object
) -> None:
    connection = await _connection(run)
    await _normalize(session, connection, pots=[make_pot(isa_wrapper="LISA")])
    pot = make_pot()
    if isa_wrapper == "missing":
        del pot["isa_wrapper"]
    else:
        pot["isa_wrapper"] = isa_wrapper

    await _normalize(session, connection, pots=[pot])

    saved = await get_account(session, (await _account_map(session, connection))["pot_TEST1"])
    assert saved.tax_wrapper == "lisa"
    assert await _warnings(session, connection) == []


async def test_atm_fees_detailed_warns_once(session, run) -> None:
    connection = await _connection(run)
    withdrawal = make_transaction(amount=-2000, atm_fees_detailed={"withdrawal": -150})

    for _ in range(2):
        report = await _normalize(session, connection, transactions=[withdrawal])

    assert report.unchanged == 1  # posted all the same
    assert await _warnings(session, connection) == [UNMAPPED.format("atm_fees_detailed")]
    # It stays until the owner clears it, even when the field is gone again.
    await _normalize(session, connection, transactions=[make_transaction(amount=-2000)])
    assert await _warnings(session, connection) == [UNMAPPED.format("atm_fees_detailed")]


async def test_unknown_local_currency_warns(session, run) -> None:
    connection = await _connection(run)
    payment = make_transaction(amount=-1234, local_amount=-99000, local_currency="XTS")

    report = await _normalize(session, connection, transactions=[payment])

    assert report.created == 1
    (tx,) = await _transactions(session)
    details = await _details(session, tx.id)
    assert (details.original_quantity, details.original_instrument_id) == (None, None)
    assert await _warnings(session, connection) == [UNMAPPED.format("local_currency")]


@pytest.mark.parametrize(
    ("name", "overrides"),
    [
        ("labels", {"labels": "test-label"}),
        ("labels", {"labels": ["test-label", 7]}),
        ("fees", {"fees": {"deposit.cash": "-100"}}),
        # A fee as large as the amount would leave a zero posting on the account.
        ("fees", {"fees": {"test.fee": -1234}}),
        ("categories", {"categories": ["groceries"]}),
        ("categories", {"categories": {"groceries": True}}),
        ("metadata.mcc", {"metadata": {"mcc": "54A1"}}),
    ],
    ids=[
        "labels-text",
        "labels-number",
        "fees-text",
        "fees-the-whole-amount",
        "categories-list",
        "categories-flag",
        "mcc-not-digits",
    ],
)
async def test_unexpected_shapes_warn_and_still_post(
    session, run, name: str, overrides: dict[str, Any]
) -> None:
    connection = await _connection(run)
    payment = make_transaction(amount=-1234, merchant=make_merchant()) | overrides

    report = await _normalize(session, connection, transactions=[payment])

    assert report.created == 1
    (tx,) = await _transactions(session)
    main = (await _account_map(session, connection))[ACCOUNT_ID]
    assert await _postings(session, tx.id) == [
        (main, Decimal("-12.34"), ""),
        (await _unassigned(run), Decimal("12.34"), ""),
    ]
    assert await _splits(session, tx.id) == []
    details = await _details(session, tx.id)
    assert details.labels == []
    assert (await session.get_one(Entity, details.place_entity_id)).mcc is None
    assert await _warnings(session, connection) == [UNMAPPED.format(name)]


def _set(path: str, value: object) -> Callable[[dict[str, Any]], None]:
    """Set the value at a dotted path of a transaction (made from make_transaction)."""

    def change(tx: dict[str, Any]) -> None:
        *parents, key = path.split(".")
        target = tx
        for parent in parents:
            target = target[parent]
        target[key] = value

    return change


@pytest.mark.parametrize(
    ("name", "value", "column"),
    [
        ("category", {"id": "x"}, "tx.category"),
        ("scheme", 7, "tx.payment_scheme"),
        ("include_in_spending", "yes", "tx.include_in_spending"),
        ("local_amount", "-1234", "tx.original_quantity"),
        ("local_currency", 826, "tx.original_instrument_id"),
        ("merchant", "merch_TEST1", "tx.place_entity_id"),
        ("merchant.id", 7, "tx.place_entity_id"),
        ("merchant.address", "1 Test Street", "place.address_street"),
        ("merchant.address.city", 7, "place.address_city"),
        ("merchant.address.latitude", "north", "place.latitude"),
        ("merchant.address.approximate", "yes", "place.location_approximate"),
        ("merchant.online", "yes", "place.online"),
        ("merchant.logo", 7, "place.logo_url"),
        ("merchant.metadata", "x", "place.website"),
        ("merchant.metadata.website", 7, "place.website"),
        ("metadata.card_acceptor_website", 7, "place.card_acceptor_website"),
    ],
)
async def test_unreadable_transaction_fields_warn_and_read_as_unknown(
    session, run, name: str, value: object, column: str
) -> None:
    connection = await _connection(run)
    payment = make_transaction(
        amount=-1234,
        local_amount=-1234,
        local_currency="GBP",
        category="groceries",
        scheme="mastercard",
        include_in_spending=True,
        merchant=make_merchant(),
        metadata={"mcc": "5411", "card_acceptor_website": "www.example.test"},
    )
    _set(name, value)(payment)

    report = await _normalize(session, connection, transactions=[payment])

    assert report.created == 1
    assert await _warnings(session, connection) == [UNMAPPED.format(name)]
    (tx,) = await _transactions(session)
    details = await _details(session, tx.id)
    table, field_name = column.split(".")
    if table == "tx":
        assert getattr(details, field_name) is None
    else:
        place = await session.get_one(Entity, details.place_entity_id)
        assert getattr(place, field_name) is None


@pytest.mark.parametrize(
    ("name", "change", "field_name"),
    [
        ("account.product_type", {"account": {"product_type": 7}}, "product_type"),
        ("account.owner_type", {"account": {"owner_type": ["personal"]}}, "owner_type"),
        ("pot.type", {"pot": {"type": 7}}, "product_type"),
        ("pot.goal_amount", {"pot": {"goal_amount": "1000"}}, "goal_amount"),
        ("pot.locked", {"pot": {"locked": "no"}}, "locked"),
        ("pot.round_up", {"pot": {"round_up": 1}}, "round_up"),
        ("pot.round_up_multiplier", {"pot": {"round_up_multiplier": "x"}}, "round_up_multiplier"),
    ],
)
async def test_unreadable_account_and_pot_fields_keep_their_value(
    session, run, name: str, change: dict[str, dict[str, object]], field_name: str
) -> None:
    connection = await _connection(run)
    stated: dict[str, object] = {
        "type": "flexible_savings",
        "goal_amount": 100000,
        "locked": True,
        "round_up": True,
        "round_up_multiplier": 2,
    }
    await _normalize(session, connection, pots=[make_pot(**stated)])
    account_map = await _account_map(session, connection)
    target = account_map[ACCOUNT_ID] if name.startswith("account.") else account_map["pot_TEST1"]
    kept = getattr(await get_account(session, target), field_name)
    assert kept is not None

    await _normalize(
        session,
        connection,
        account=make_account(**change.get("account", {})),
        pots=[make_pot(**(stated | change.get("pot", {})))],
    )

    assert getattr(await get_account(session, target), field_name) == kept
    assert await _warnings(session, connection) == [UNMAPPED.format(name)]


@pytest.mark.parametrize("name", [7, None], ids=["unreadable", "absent"])
async def test_a_merchant_without_a_readable_name_still_gets_its_place(
    session, run, name: object
) -> None:
    connection = await _connection(run)
    payment = make_transaction(merchant=make_merchant(id="merch_TEST1", name=name))

    await _normalize(session, connection, transactions=[payment])

    (tx,) = await _transactions(session)
    assert tx.counterparty_entity_id is None  # a brand is known by its name
    place = await session.get_one(Entity, (await _details(session, tx.id)).place_entity_id)
    assert (place.name, place.address_street) == ("merch_TEST1", "1 Test Street")
    warned = [UNMAPPED.format("merchant.name")] if name is not None else []
    assert await _warnings(session, connection) == warned


async def test_a_decline_with_an_unreadable_reason_is_recorded_not_posted(session, run) -> None:
    connection = await _connection(run)
    declined = make_transaction(settled="", decline_reason={"code": "INSUFFICIENT_FUNDS"})

    report = await _normalize(session, connection, transactions=[declined])

    assert report == NormalizeReport(skipped_declined=1)
    assert await _transactions(session) == []
    assert [(r.external_id, r.decline_reason) for r in await _declined(session)] == [
        ("tx_TEST0001", "")
    ]
    assert await _warnings(session, connection) == [UNMAPPED.format("decline_reason")]


# The fields read before the reader existed, through it now (reader-everywhere plan).


def _pot_move(**overrides: Any) -> dict[str, Any]:
    """A settled move into pot_TEST1, described by its counterparty, with a memo."""
    return (
        make_transaction(
            amount=-5000,
            counterparty={"name": "A Friend"},
            notes="weekly shop",
            metadata={"pot_id": "pot_TEST1"},
        )
        | overrides
    )


@pytest.mark.parametrize(
    ("path", "change", "read_as"),
    [
        ("settled", {"settled": False}, {"status": "pending"}),
        ("settled", {"settled": 0}, {"status": "pending"}),
        ("settled", {"settled": "not a time"}, {"status": "pending"}),
        ("settled", {"settled": "2026-03-02T06:00:00"}, {"status": "pending"}),
        ("metadata", {"metadata": "pot_TEST1"}, {"pot": False}),
        ("metadata.pot_id", {"metadata": {"pot_id": 7}}, {"pot": False}),
        ("merchant.name", {"merchant": make_merchant(name=7)}, {}),
        ("counterparty", {"counterparty": "A Friend"}, {"description": "TEST SHOP LONDON"}),
        ("counterparty.name", {"counterparty": {"name": 7}}, {"description": "TEST SHOP LONDON"}),
        ("description", {"counterparty": {}, "description": 7}, {"description": ""}),
        ("notes", {"notes": 7}, {"memo": ""}),
        ("updated", {"merchant": make_merchant(), "updated": 7}, {"description": "Test Grocer"}),
    ],
    ids=[
        "settled-false",
        "settled-zero",
        "settled-text",
        "settled-no-offset",
        "metadata-text",
        "pot-id-number",
        "merchant-name-number",
        "counterparty-text",
        "counterparty-name-number",
        "description-number",
        "notes-number",
        "updated-number",
    ],
)
async def test_older_transaction_fields_warn_and_read_as_unknown(
    session, run, path: str, change: dict[str, Any], read_as: dict[str, object]
) -> None:
    connection = await _connection(run)
    expected = {
        "status": "booked",
        "description": "A Friend",
        "memo": "weekly shop",
        "pot": True,
    } | read_as

    report = await _normalize(
        session, connection, transactions=[_pot_move(**change)], pots=[make_pot()]
    )

    assert report.created == 1
    assert await _warnings(session, connection) == [UNMAPPED.format(path)]
    (tx,) = await _transactions(session)
    booked = expected["status"] == "booked"
    assert (tx.status, tx.value_date, tx.description) == (
        expected["status"],
        date(2026, 3, 2) if booked else None,
        expected["description"],
    )
    account_map = await _account_map(session, connection)
    other = account_map["pot_TEST1"] if expected["pot"] else await _unassigned(run)
    assert await _postings(session, tx.id) == [
        (account_map[ACCOUNT_ID], Decimal("-50.00"), expected["memo"]),
        (other, Decimal("50.00"), ""),
    ]


async def test_a_declined_description_reads_through_the_reader(session, run) -> None:
    connection = await _connection(run)
    declined = make_transaction(
        settled="",
        decline_reason="INSUFFICIENT_FUNDS",
        merchant=make_merchant(name=7),
        counterparty={"name": "A Friend"},
    )

    await _normalize(session, connection, transactions=[declined])

    assert [r.description for r in await _declined(session)] == ["A Friend"]
    assert await _warnings(session, connection) == [UNMAPPED.format("merchant.name")]


@pytest.mark.parametrize("key", ["fees", "categories", "labels", "atm_fees_detailed"])
async def test_an_empty_string_reads_as_absent(session, run, key: str) -> None:
    connection = await _connection(run)

    report = await _normalize(session, connection, transactions=[make_transaction(**{key: ""})])

    assert report.created == 1
    (tx,) = await _transactions(session)
    assert len(await _postings(session, tx.id)) == 2
    assert await _warnings(session, connection) == []


@pytest.mark.parametrize(
    ("path", "change", "name"),
    [
        (
            "account.description",
            {"description": ["uk_retail"], "type": "uk_retail_joint"},
            "Monzo Joint",
        ),
        ("account.type", {"description": "user_TEST1", "type": {"name": "uk_retail"}}, "Monzo"),
    ],
    ids=["description", "type"],
)
async def test_an_account_name_it_cannot_read_falls_back(
    session, run, path: str, change: dict[str, object], name: str
) -> None:
    connection = await _connection(run)

    await _normalize(session, connection, account=make_account(**change))

    account = await get_account(session, (await _account_map(session, connection))[ACCOUNT_ID])
    assert account.name == name
    assert await _warnings(session, connection) == [UNMAPPED.format(path)]


@pytest.mark.parametrize(
    ("path", "change"),
    [
        ("pot.name", {"name": 7}),
        ("pot.created", {"created": "not a time"}),
        ("pot.created", {"created": 7}),
        ("pot.deleted", {"deleted": "false"}),
    ],
    ids=["name-number", "created-text", "created-number", "deleted-text"],
)
async def test_a_new_pot_with_unreadable_fields_is_still_opened(
    session, run, path: str, change: dict[str, object]
) -> None:
    connection = await _connection(run)

    await _normalize(session, connection, pots=[make_pot(**change)])

    pot = await get_account(session, (await _account_map(session, connection))["pot_TEST1"])
    assert (pot.name, pot.status, pot.opened_on) == (
        "Pot" if path == "pot.name" else "Savings",
        "open",
        None if path == "pot.created" else date(2024, 2, 1),
    )
    assert await _warnings(session, connection) == [UNMAPPED.format(path)]


async def test_an_unreadable_deleted_flag_never_closes_a_pot(session, run) -> None:
    connection = await _connection(run)
    await _normalize(session, connection, pots=[make_pot()])

    await _normalize(session, connection, pots=[make_pot(deleted="true")])

    pot = await get_account(session, (await _account_map(session, connection))["pot_TEST1"])
    assert pot.status == "open"
    assert await _warnings(session, connection) == [UNMAPPED.format("pot.deleted")]


async def _snapshots(session, account_id: uuid.UUID) -> list[Decimal]:
    return list(
        await session.scalars(
            text(
                "select quantity from finance.balance_snapshots where account_id = :id"
                " order by as_of"
            ),
            {"id": account_id},
        )
    )


@pytest.mark.parametrize(
    ("path", "change"),
    [
        ("pot.balance", {"balance": True}),
        ("pot.balance", {"balance": "6000"}),
        ("pot.balance", {"balance": 10**43}),
        ("pot.currency", {"currency": 826}),
        ("pot.currency", {"currency": "XTS"}),
    ],
    ids=[
        "balance-flag",
        "balance-text",
        "balance-too-large",
        "currency-number",
        "currency-unknown",
    ],
)
async def test_a_pot_balance_it_cannot_read_records_no_snapshot(
    session, run, path: str, change: dict[str, object]
) -> None:
    connection = await _connection(run)
    await _normalize(session, connection, pots=[make_pot(goal_amount=100000)])
    pot_id = (await _account_map(session, connection))["pot_TEST1"]

    await _normalize(
        session, connection, pots=[make_pot(goal_amount=100000, balance=6000) | change]
    )

    assert await _snapshots(session, pot_id) == [Decimal("50.00")]
    # A goal in a currency it cannot read keeps the stored one.
    assert (await get_account(session, pot_id)).goal_amount == Decimal("1000.00")
    assert await _warnings(session, connection) == [UNMAPPED.format(path)]


_MISSING = object()


def _core(path: str, record: str, key: str, value: object, label: str) -> Any:
    return pytest.param(path, record, key, value, id=f"{path}-{label}")


@pytest.mark.parametrize(
    ("path", "record", "key", "value"),
    [
        _core("id", "transaction", "id", _MISSING, "missing"),
        _core("id", "transaction", "id", 7, "number"),
        _core("amount", "transaction", "amount", _MISSING, "missing"),
        _core("amount", "transaction", "amount", True, "flag"),
        _core("amount", "transaction", "amount", 12.5, "fraction"),
        _core("amount", "transaction", "amount", "-1234", "text"),
        _core("currency", "transaction", "currency", _MISSING, "missing"),
        _core("currency", "transaction", "currency", 826, "number"),
        _core("created", "transaction", "created", _MISSING, "missing"),
        _core("created", "transaction", "created", "not a time", "text"),
        _core("created", "transaction", "created", "2026-03-01T12:00:00", "no-offset"),
        _core("currency", "declined", "currency", 826, "declined-number"),
        _core("created", "declined", "created", "not a time", "declined-text"),
        _core("account.id", "account", "id", 7, "number"),
        _core("account.created", "account", "created", _MISSING, "missing"),
        _core("account.created", "account", "created", "2024-01-15", "date-only"),
        _core("pot.id", "pot", "id", _MISSING, "missing"),
        _core("balance.balance", "balance", "balance", _MISSING, "missing"),
        _core("balance.balance", "balance", "balance", True, "flag"),
        _core("balance.currency", "balance", "currency", 826, "number"),
    ],
)
async def test_core_fields_fail_loudly(
    session, run, path: str, record: str, key: str, value: object
) -> None:
    connection = await _connection(run)
    records = {
        "transaction": make_transaction(),
        "declined": make_transaction(settled="", decline_reason="INSUFFICIENT_FUNDS"),
        "account": make_account(),
        "pot": make_pot(),
        "balance": make_balance(),
    }
    if value is _MISSING:
        del records[record][key]
    else:
        records[record][key] = value

    with pytest.raises(MonzoFieldError) as raised:
        await _normalize(
            session,
            connection,
            transactions=[records["declined" if record == "declined" else "transaction"]],
            account=records["account"],
            pots=[records["pot"]],
            balance_record=records["balance"],
        )

    assert raised.value.path == path
    assert str(raised.value) == f"Monzo field {path} is missing or unreadable"


async def test_a_reversed_transaction_needs_no_currency_or_creation(session, run) -> None:
    connection = await _connection(run)
    reversed_payment = make_transaction(amount=0, settled="", created="not a time")
    del reversed_payment["currency"]

    report = await _normalize(session, connection, transactions=[reversed_payment])

    assert report == NormalizeReport()


def _statement(n: int, day: int, **merchant: object) -> dict[str, Any]:
    """A payment at merchant merch_TEST1, stated on the given day of March 2026."""
    mcc = merchant.pop("mcc", None)
    return make_transaction(
        id=f"tx_TEST000{n}",
        created=f"2026-03-0{day}T12:00:00.000Z",
        updated=f"2026-03-0{day}T12:00:00.000Z",
        merchant=make_merchant(**merchant),
        metadata={} if mcc is None else {"mcc": mcc},
    )


async def _place_of_merchant(session) -> Entity:
    place_id = await session.scalar(
        text(
            "select id from core.entities"
            " where kind = 'place' and external_ids ->> 'monzo_merchant' = 'merch_TEST1'"
        )
    )
    return await session.get_one(Entity, place_id, populate_existing=True)


async def _place_changes(session, place_id: uuid.UUID) -> int:
    return await session.scalar(
        text("select count(*) from core.audit_log where changes ? :key"),
        {"key": f"entity:{place_id}"},
    )


@pytest.mark.parametrize(
    "order",
    [(0, 1, 2), (2, 1, 0), (1, 0, 2), (2, 0, 1)],
    ids=["oldest-first", "newest-first", "mixed-1", "mixed-2"],
)
async def test_the_newest_statement_about_a_merchant_wins(
    session, run, order: tuple[int, ...]
) -> None:
    connection = await _connection(run)
    statements = [
        _statement(1, 2, name="Old Name", mcc="5411"),
        _statement(2, 4, name="Old Name", mcc="5499"),
        # The newest says nothing of the mcc: it keeps the newest one stated before it.
        _statement(3, 6, name="New Name"),
    ]
    payments = [statements[i] for i in order]

    await _normalize(session, connection, transactions=payments)

    place = await _place_of_merchant(session)
    assert (place.name, place.mcc) == ("New Name", "5499")
    # The same disagreeing statements again write nothing.
    place_id = place.id
    changes = await _place_changes(session, place_id)
    await _normalize(session, connection, transactions=payments)
    assert await _place_changes(session, place_id) == changes


@pytest.mark.parametrize(
    "updated",
    [
        "9999-12-31T23:59:59.000-01:00",
        "0001-01-01T00:00:00.000+01:00",
        "2026-03-09",
        "2026-03-09T12:00:00",
    ],
    ids=["past-the-last-year", "before-the-first-year", "date-only", "no-offset"],
)
async def test_a_time_it_cannot_read_neither_fails_nor_wins(session, run, updated: str) -> None:
    connection = await _connection(run)
    unreadable = _statement(1, 2, mcc="5411") | {"updated": updated}
    newer = _statement(2, 4, mcc="5499")

    # Read as no time, it falls back to its creation (the 2nd), older than the other (the 4th).
    await _normalize(session, connection, transactions=[newer, unreadable])
    assert (await _place_of_merchant(session)).mcc == "5499"

    # The replay ranks its copies by the same rule, and fails on none of them: a readable
    # copy of the same transaction beats the unreadable one, whichever was fetched last.
    await run(_before_structured_fields, connection)
    readable = _statement(1, 3, mcc="5411") | {"category": "transport"}
    await _store_page(run, connection, [readable])
    await _store_page(run, connection, [unreadable | {"category": "groceries"}])
    await _normalize(session, connection)
    first = next(r for r in await _transactions(session) if r.external_id == "tx_TEST0001")
    assert (await _details(session, first.id)).category == "transport"
    assert (await _place_of_merchant(session)).mcc == "5499"


@pytest.mark.parametrize(
    "value",
    [
        "2026-01-10T12:00:00.123Z",
        "2026-01-10T12:00:00+01:00",
        "2026-01-10T12:00:00-0130",
        "2026-01-10T12:00:00-15:59",
        "2026-01-10T24:00:00Z",
        "2026-01-10T23:59:60Z",
        "2026-01-10T12:00:00+16:00",
        "2026-01-10T12:00:00+15:60",
        "2026-01-10T12:00:00.9999995Z",
        "2026-02-30T12:00:00Z",
        "1899-12-31T23:59:59Z",
        "infinity",
    ],
)
async def test_the_replay_and_the_fold_read_a_time_alike(session, value: str) -> None:
    """One rule for a readable time: the replay's SQL and the fold's Python read the same
    strings, as the same instants."""
    readable = await session.scalar(
        text("select :v ~ :pattern and pg_input_is_valid(:v, 'timestamptz')"),
        {"v": value, "pattern": f"^{_ISO_TIME}$"},
    )
    in_sql = (
        await session.scalar(text("select cast(:v as timestamptz)"), {"v": value})
        if readable
        else None
    )
    assert in_sql == _readable_time(value)


async def test_a_newer_replayed_statement_beats_an_older_page(session, run) -> None:
    connection = await _connection(run)
    newer = make_transaction(
        created="2026-03-05T12:00:00.000Z",
        updated="2026-03-05T12:00:00.000Z",
        merchant=make_merchant(),
        metadata={"mcc": "5499"},
    )
    await _normalize(session, connection, transactions=[newer])
    await run(_before_structured_fields, connection)
    older = make_transaction(
        id="tx_TEST0002",
        created="2026-03-01T12:00:00.000Z",
        updated="2026-03-01T12:00:00.000Z",
        merchant=make_merchant(),
        metadata={"mcc": "5411"},
    )

    await _normalize(session, connection, transactions=[older])

    assert (await _place_of_merchant(session)).mcc == "5499"


async def test_the_replay_does_not_override_newer_merchant_facts(session, run) -> None:
    connection = await _connection(run)
    older = make_transaction(
        merchant=make_merchant(), metadata={"mcc": "5411"}, updated="2026-03-01T12:00:00.000Z"
    )
    await _normalize(session, connection, transactions=[older])
    await run(_before_structured_fields, connection)
    newer = make_transaction(
        id="tx_TEST0002",
        created="2026-03-05T12:00:00.000Z",
        updated="2026-03-05T12:00:00.000Z",
        merchant=make_merchant(),
        metadata={"mcc": "5499"},
    )

    # The replay meets the older copy before this sync's own page.
    await _normalize(session, connection, transactions=[newer])

    place_ids = {
        (await _details(session, r.id)).place_entity_id for r in await _transactions(session)
    }
    (place_id,) = place_ids
    assert (await session.get_one(Entity, place_id, populate_existing=True)).mcc == "5499"


@pytest.mark.parametrize(
    ("name", "overrides"),
    [
        ("local_amount", {"local_amount": -1234, "local_currency": None}),
        ("local_currency", {"local_amount": None, "local_currency": "USD"}),
        ("local_amount", {"local_amount": -(10**43), "local_currency": "GBP"}),
        ("categories", {"categories": {"groceries": -(10**43), "household": 0}}),
        ("fees", {"fees": {"test.fee": -(10**43)}}),
    ],
    ids=[
        "amount-without-currency",
        "currency-without-amount",
        "local-too-large",
        "split-too-large",
        "fee-too-large",
    ],
)
async def test_amounts_it_cannot_store_warn_and_still_post(
    session, run, name: str, overrides: dict[str, Any]
) -> None:
    connection = await _connection(run)
    payment = make_transaction(amount=-1234) | overrides

    report = await _normalize(session, connection, transactions=[payment])

    assert report.created == 1
    (tx,) = await _transactions(session)
    details = await _details(session, tx.id)
    assert (details.original_quantity, details.original_instrument_id) in [
        (None, None),
        (Decimal("-12.34"), (await get_currency(session, "GBP")).id),
    ]
    assert await _splits(session, tx.id) == []
    assert len(await _postings(session, tx.id)) == 2
    assert await _warnings(session, connection) == [UNMAPPED.format(name)]


async def test_a_fee_longer_than_28_digits_still_balances(session, run) -> None:
    connection = await _connection(run)
    payment = make_transaction(amount=-1234, fees={"card.fee": -(10**30 + 7)})

    report = await _normalize(session, connection, transactions=[payment])

    assert report.created == 1
    (tx,) = await _transactions(session)
    postings = await _postings(session, tx.id)
    assert [quantity for _, quantity, _ in postings] == [
        Decimal("9999999999999999999999999987.73"),
        Decimal("12.34"),
        Decimal("-10000000000000000000000000000.07"),
    ]
    with localcontext(prec=MAX_PREC):
        assert sum(quantity for _, quantity, _ in postings) == 0
    assert await _warnings(session, connection) == []


@pytest.mark.parametrize("fees", [None, {"card.fee": -100}], ids=["no-fee", "fee"])
async def test_an_amount_longer_than_28_digits_posts_exactly(
    session, run, fees: dict[str, int] | None
) -> None:
    connection = await _connection(run)
    amount = -(10**30 + 7)
    payment = make_transaction(amount=amount, fees=fees)

    report = await _normalize(
        session,
        connection,
        transactions=[payment],
        balance=amount,
        reconciled=[ACCOUNT_ID],
        openings=[ACCOUNT_ID],
    )

    assert report.created == 1 and report.mismatches == []
    (tx,) = [r for r in await _transactions(session) if r.external_id == "tx_TEST0001"]
    postings = await _postings(session, tx.id)
    assert postings[1][1] == Decimal("10000000000000000000000000000.07")
    with localcontext(prec=MAX_PREC):
        assert sum(quantity for _, quantity, _ in postings) == 0


async def test_a_pot_goal_it_cannot_store_keeps_the_stored_one(session, run) -> None:
    connection = await _connection(run)
    await _normalize(session, connection, pots=[make_pot(goal_amount=100000)])

    await _normalize(session, connection, pots=[make_pot(goal_amount=10**43)])

    pot = await get_account(session, (await _account_map(session, connection))["pot_TEST1"])
    assert pot.goal_amount == Decimal("1000.00")
    assert await _warnings(session, connection) == [UNMAPPED.format("pot.goal_amount")]


async def test_a_reversed_transaction_still_records_its_merchant(session, run) -> None:
    connection = await _connection(run)
    reversed_payment = make_transaction(amount=0, settled="", merchant=make_merchant())

    report = await _normalize(session, connection, transactions=[reversed_payment])

    assert report == NormalizeReport()
    assert await _transactions(session) == []
    places = await session.scalar(
        text("select count(*) from core.entities where kind = 'place' and name = :name"),
        {"name": make_merchant()["name"]},
    )
    assert places == 1


async def test_transaction_without_new_keys_still_posts(session, run) -> None:
    connection = await _connection(run)
    bare = make_transaction(merchant=MERCHANT)
    for key in ("local_amount", "local_currency", "category"):
        del bare[key]

    report = await _normalize(session, connection, transactions=[bare])

    assert report.created == 1
    (tx,) = await _transactions(session)
    main = (await _account_map(session, connection))[ACCOUNT_ID]
    assert await _postings(session, tx.id) == [
        (main, Decimal("-12.34"), ""),
        (await _unassigned(run), Decimal("12.34"), ""),
    ]
    details = await _details(session, tx.id)
    assert tuple(details)[:6] == (None, None, None, [], None, None)
    assert await _splits(session, tx.id) == []
    place = await session.get_one(Entity, details.place_entity_id)
    assert (place.kind, place.name, place.address_street, place.latitude, place.mcc) == (
        "place",
        "Test Grocer",
        None,
        None,
        None,
    )
    assert await _warnings(session, connection) == []


async def test_resync_with_details_writes_nothing(session, run) -> None:
    connection = await _connection(run)
    history = [
        make_transaction(id="tx_TEST0001", amount=12300, fees={"deposit.cash": -100}),
        make_transaction(
            id="tx_TEST0002",
            created="2026-03-01T13:00:00.000Z",
            amount=-3000,
            local_amount=-3600,
            local_currency="USD",
            categories={"groceries": -2000, "household": -1000},
            labels=["test-label"],
            scheme="mastercard",
            include_in_spending=True,
            merchant=make_merchant(),
            metadata={"mcc": "5411"},
        ),
        make_transaction(
            id="tx_TEST0003",
            created="2026-03-01T14:00:00.000Z",
            amount=-2500,
            settled="",
            decline_reason="INSUFFICIENT_FUNDS",
            merchant=make_merchant(),
        ),
    ]
    run_args: dict[str, Any] = {
        "transactions": history,
        "pots": [make_pot(isa_wrapper="ISA", goal_amount=100000)],
        "reconciled": [ACCOUNT_ID],
        "openings": [ACCOUNT_ID],
    }
    first = await _normalize(session, connection, **run_args)
    assert (first.created, first.skipped_declined) == (3, 1)  # two and the opening

    async def state() -> list[list[str]]:
        tables = (
            "finance.transactions",
            "finance.postings",
            "finance.transaction_category_splits",
            "finance.declined_transactions",
            "finance.accounts",
            "core.entities",
            "core.connections",
            "core.events",
        )
        return [await _rows(session, table) for table in tables]

    before = await state()

    second = await _normalize(session, connection, **run_args)

    assert second == NormalizeReport(unchanged=3, skipped_declined=1)
    assert await state() == before


async def test_device_fingerprint_and_ip_never_stored(session, run) -> None:
    connection = await _connection(run)
    secret = {"device_fingerprint": "fp-SYNTHETIC-5e1f", "ip_address_attempt": "198.51.100.77"}
    metadata = {"mcc": "5411", **secret}
    transactions = [
        make_transaction(
            id="tx_TEST0001",
            merchant=make_merchant(),
            metadata=metadata,
            categories={"groceries": -1000, "household": -234},
            fees={"test.fee": -10},
        ),
        make_transaction(
            id="tx_TEST0002",
            created="2026-03-01T13:00:00.000Z",
            settled="",
            decline_reason="INSUFFICIENT_FUNDS",
            merchant=make_merchant(),
            metadata=metadata,
        ),
    ]

    await _normalize(session, connection, transactions=transactions, pots=[make_pot()])

    tables = (
        "finance.transactions",
        "finance.postings",
        "finance.declined_transactions",
        "finance.transaction_category_splits",
        "finance.accounts",
        "core.entities",
        "core.events",
        "core.audit_log",
    )
    for table in tables:
        rows = await _rows(session, table)
        assert rows, table  # the search looks at something
        for value in secret.values():
            assert not [row for row in rows if value in row], (table, value)


# Backfill by replay (plan Decision 12).


def _detailed(**overrides: Any) -> dict[str, Any]:
    """A card payment with the fields the structured-fields normalizer stores."""
    payment = make_transaction(
        amount=-850,
        local_amount=-1000,
        local_currency="USD",
        categories={"groceries": -500, "household": -350},
        labels=["test-label"],
        scheme="mastercard",
        include_in_spending=True,
        merchant=make_merchant(),
        metadata={"mcc": "5411"},
    )
    return payment | overrides


async def _before_structured_fields(ctx: CommandContext, connection: ConnectionRecord) -> None:
    """Make the ledger and the connection look as they did before the structured fields: no
    typed fields on the transactions, no category splits or declined rows, and no normalizer
    version in the config."""
    for statement in (
        "update finance.transactions set original_quantity = null,"
        " original_instrument_id = null, category = null, labels = '{}',"
        " include_in_spending = null, payment_scheme = null, place_entity_id = null",
        "delete from finance.transaction_category_splits",
        "delete from finance.declined_transactions",
        "delete from core.entities where kind = 'place'",
    ):
        await ctx.session.execute(text(statement))
    await update_config(ctx, connection.id, remove=[NORMALIZER_VERSION_KEY])


async def _store_page(
    run,
    connection: ConnectionRecord,
    transactions: Sequence[dict[str, Any]],
    *,
    copy: tuple[datetime, int] | None = None,
) -> None:
    """Store a transactions page in a sync of its own without normalizing it, as a sync keeps
    the pages of an attempt it discards. Fetched now, unless `copy` gives the fetch time and
    the record id (as an integer)."""
    content = list(transactions)
    request: dict[str, JSONValue] = {"account_id": ACCOUNT_ID}

    async def store(ctx: CommandContext) -> None:
        if copy is None:
            await store_raw_record(
                ctx,
                connection_id=connection.id,
                record_type="transactions_page",
                request=request,
                content=content,
                connector_version="monzo-sync-1",
                sync_id=uuid.uuid4(),
            )
            return
        fetched_at, record_id = copy
        await ctx.session.execute(
            insert(RawRecord).values(
                id=uuid.UUID(int=record_id),
                connection_id=connection.id,
                sync_id=uuid.uuid4(),
                record_type="transactions_page",
                request=request,
                fetched_at=fetched_at,
                content=content,
                content_hash=_content_hash(content),
                connector_version="monzo-sync-1",
            )
        )

    await run(store)


async def test_replay_fills_history_outside_this_sync(session, run) -> None:
    connection = await _connection(run)
    other = make_transaction(
        id="tx_TEST0002", created="2026-03-01T13:00:00.000Z", amount=-500, local_amount=-500
    )
    await _normalize(session, connection, transactions=[_detailed(), other])
    await run(_before_structured_fields, connection)

    # This sync fetched only the other one, changed since.
    report = await _normalize(session, connection, transactions=[other | {"notes": "lunch"}])

    # One update each: the first by the replay, the other by this sync's page alone, as the
    # replay skips what this sync fetched.
    assert (report.created, report.updated, report.unchanged) == (0, 2, 0)
    by_id = {r.external_id: r for r in await _transactions(session)}
    replayed = by_id["tx_TEST0001"]
    details = await _details(session, replayed.id)
    usd = await get_currency(session, "USD")
    assert (
        details.original_quantity,
        details.original_instrument_id,
        details.category,
        details.labels,
        details.include_in_spending,
        details.payment_scheme,
    ) == (Decimal("-10.00"), usd.id, "groceries", ["test-label"], True, "mastercard")
    place = await session.get_one(Entity, details.place_entity_id)
    assert (place.kind, place.external_ids) == ("place", {"monzo_merchant": "merch_TEST1"})
    assert await _splits(session, replayed.id) == [
        ("groceries", Decimal("-5.00")),
        ("household", Decimal("-3.50")),
    ]
    assert (await _postings(session, by_id["tx_TEST0002"].id))[0][2] == "lunch"
    config = (await get_connection(session, connection.id)).config
    assert config[NORMALIZER_VERSION_KEY] == "monzo-norm-2"


async def test_replay_uses_the_latest_by_updated(session, run) -> None:
    connection = await _connection(run)
    newest = make_transaction(amount=-1234, updated="2026-03-02T06:00:00.000Z")
    await _normalize(session, connection, transactions=[newest])
    # Stored after the newest: a page seen before keeps its first fetch time, so the order of
    # fetching does not tell which is newer.
    older = make_transaction(
        amount=-1500, settled="", category="shopping", updated="2026-03-01T12:00:05.000Z"
    )
    undated = make_transaction(amount=-999, settled="", category="eating_out")  # the oldest
    for tx in (older, undated):
        await _store_page(run, connection, [tx])
    await run(_before_structured_fields, connection)

    report = await _normalize(session, connection)

    assert report == NormalizeReport(updated=1)
    (tx,) = await _transactions(session)
    main = (await _account_map(session, connection))[ACCOUNT_ID]
    assert (tx.status, tx.value_date) == ("booked", date(2026, 3, 2))
    assert (await _postings(session, tx.id))[0] == (main, Decimal("-12.34"), "")
    assert (await _details(session, tx.id)).category == "groceries"


async def test_replay_breaks_ties_by_fetch_time_then_id(session, run) -> None:
    connection = await _connection(run)
    early, late = datetime(2026, 3, 2, 7, tzinfo=UTC), datetime(2026, 3, 2, 8, tzinfo=UTC)

    def payment(n: int, category: str, updated: str = "2026-03-02T06:00:00.000Z") -> dict[str, Any]:
        return make_transaction(
            id=f"tx_TEST{n:04d}",
            created=f"2026-03-01T12:{n:02d}:00.000Z",
            category=category,
            updated=updated,
        )

    # Posted from an older copy of each, fetched after all the copies below (now) but beaten
    # by their later `updated`.
    stale = [payment(n, "eating_out", "2026-03-02T05:00:00.000Z") for n in range(1, 8)]
    await _normalize(session, connection, transactions=stale)
    # The same `updated`; fetched later beats a greater id.
    await _store_page(run, connection, [payment(1, "groceries")], copy=(late, 1))
    await _store_page(run, connection, [payment(1, "shopping")], copy=(early, 2))
    # The same fetch time too: the greater id wins. Stored last for some and first for the
    # others, so that no order but the id's picks them all.
    for n in range(2, 8):
        winner, loser = (payment(n, "household"), 10 * n + 1), (payment(n, "shopping"), 10 * n)
        for page, record_id in (winner, loser) if n % 2 else (loser, winner):
            await _store_page(run, connection, [page], copy=(late, record_id))
    await run(_before_structured_fields, connection)

    await _normalize(session, connection)

    categories = {
        r.external_id: (await _details(session, r.id)).category
        for r in await _transactions(session)
    }
    assert categories == {"tx_TEST0001": "groceries"} | {
        f"tx_TEST{n:04d}": "household" for n in range(2, 8)
    }


async def test_replay_posts_nothing_new(session, run) -> None:
    connection = await _connection(run)
    await _normalize(session, connection)
    pending = make_transaction(settled="")
    declined = make_transaction(
        id="tx_TEST0002",
        created="2026-03-01T13:00:00.000Z",
        amount=-2500,
        settled="",
        decline_reason="INSUFFICIENT_FUNDS",
    )
    # Only ever in a page no sync normalized.
    await _store_page(run, connection, [pending, declined])
    await run(_before_structured_fields, connection)

    report = await _normalize(session, connection)

    # The replay only backfills what the ledger holds; a decline is recorded outside it.
    assert report == NormalizeReport(skipped_declined=1)
    assert await _transactions(session) == []
    assert [r.external_id for r in await _declined(session)] == ["tx_TEST0002"]


@pytest.mark.parametrize("updated", ["not a date", "2026-02-30T06:00:00.000Z"])
async def test_replay_treats_a_malformed_updated_as_the_oldest(session, run, updated) -> None:
    connection = await _connection(run)
    await _normalize(
        session, connection, transactions=[make_transaction(updated="2026-03-02T06:00:00.000Z")]
    )
    await _store_page(run, connection, [make_transaction(category="shopping", updated=updated)])
    await run(_before_structured_fields, connection)

    report = await _normalize(session, connection)

    assert report == NormalizeReport(updated=1)
    (tx,) = await _transactions(session)
    assert (await _details(session, tx.id)).category == "groceries"


@pytest.mark.parametrize("updated", ["infinity", "now"])
async def test_replay_ignores_special_times_in_updated(session, run, updated) -> None:
    connection = await _connection(run)
    await _normalize(
        session, connection, transactions=[make_transaction(updated="2026-03-02T06:00:00.000Z")]
    )
    # PostgreSQL reads these as times, later than the real one above.
    await _store_page(run, connection, [make_transaction(category="shopping", updated=updated)])
    await run(_before_structured_fields, connection)

    await _normalize(session, connection)

    (tx,) = await _transactions(session)
    assert (await _details(session, tx.id)).category == "groceries"


async def test_replay_runs_once(session, run) -> None:
    connection = await _connection(run)
    await _normalize(session, connection, transactions=[_detailed()])
    await run(_before_structured_fields, connection)

    replayed = await _normalize(session, connection)
    again = await _normalize(session, connection)

    assert replayed == NormalizeReport(updated=1)
    # The version is stored: the next sync replays nothing.
    assert again == NormalizeReport()


async def test_replay_runs_when_an_older_version_is_stored(session, run) -> None:
    connection = await _connection(run)
    await _normalize(session, connection, transactions=[_detailed()])
    await run(_before_structured_fields, connection)
    await run(update_config, connection.id, {NORMALIZER_VERSION_KEY: "monzo-norm-1"})

    replayed = await _normalize(session, connection)

    assert replayed == NormalizeReport(updated=1)
    config = (await get_connection(session, connection.id)).config
    assert config[NORMALIZER_VERSION_KEY] == NORMALIZER_VERSION


async def test_replay_records_places_seen_only_on_reversed_copies(session, run) -> None:
    connection = await _connection(run)
    posted = make_transaction(
        settled="", merchant=make_merchant(id="merch_TEST1"), updated="2026-03-01T12:00:00.000Z"
    )
    await _normalize(session, connection, transactions=[posted])
    await run(_before_structured_fields, connection)
    reversed_later = posted | {"amount": 0, "updated": "2026-03-02T12:00:00.000Z"}
    never_posted = make_transaction(
        id="tx_TEST0002",
        amount=0,
        settled="",
        merchant=make_merchant(id="merch_TEST2", name="Test Kiosk"),
        created="2026-03-01T13:00:00.000Z",
    )
    await _store_page(run, connection, [reversed_later, never_posted])

    await _normalize(session, connection)

    (tx,) = await _transactions(session)
    assert tx.status == "void"
    ids = await session.scalars(
        text(
            "select external_ids ->> 'monzo_merchant' from core.entities"
            " where kind = 'place' order by 1"
        )
    )
    assert list(ids) == ["merch_TEST1", "merch_TEST2"]


async def test_replay_keeps_owner_void(session, run) -> None:
    connection = await _connection(run)
    await _normalize(session, connection, transactions=[_detailed()])
    (posted,) = await _transactions(session)
    async with command_context(session, Actor("user", "owner"), "void_transaction", {}) as ctx:
        await void_transaction(ctx, posted.id)
    await run(_before_structured_fields, connection)

    report = await _normalize(session, connection)

    assert report == NormalizeReport(unchanged=1)
    status = await session.execute(
        text("select status, voided_by from finance.transactions where id = :id"),
        {"id": posted.id},
    )
    assert tuple(status.one()) == ("void", "user")


async def test_replay_records_old_declines(session, run) -> None:
    connection = await _connection(run)
    never_posted = make_transaction(
        amount=-2500, settled="", decline_reason="INSUFFICIENT_FUNDS", merchant=make_merchant()
    )
    pending = make_transaction(
        id="tx_TEST0002",
        created="2026-03-01T13:00:00.000Z",
        amount=-4000,
        settled="",
        updated="2026-03-01T13:00:01.000Z",
    )
    declined_later = pending | {
        "decline_reason": "CARD_INACTIVE",
        "updated": "2026-03-01T13:05:00.000Z",
    }
    await _normalize(session, connection, transactions=[never_posted, pending])
    await _normalize(session, connection, transactions=[declined_later])
    # The normalizer before declined rows existed posted nothing for the first, and only
    # voided the second.
    await run(_before_structured_fields, connection)

    report = await _normalize(session, connection)

    assert report == NormalizeReport(unchanged=1, skipped_declined=1)
    assert [r.status for r in await _transactions(session)] == ["void"]
    rows = [
        (r.external_id, r.quantity, r.decline_reason, r.description)
        for r in await _declined(session)
    ]
    assert rows == [
        ("tx_TEST0001", Decimal("-25.00"), "INSUFFICIENT_FUNDS", "Test Grocer"),
        ("tx_TEST0002", Decimal("-40.00"), "CARD_INACTIVE", "TEST SHOP LONDON"),
    ]
