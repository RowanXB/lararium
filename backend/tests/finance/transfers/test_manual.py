from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import insert, text

from lararium.modules.finance import models
from lararium.modules.finance.identifiers import assign_identifier, get_or_create_identifier
from lararium.modules.finance.ledger import find_unbalanced_transactions
from lararium.modules.finance.snapshots import record_snapshot
from lararium.modules.finance.transfers import (
    find_transfer_violations,
    list_transfers,
    source_ref,
    transfer_contra,
)
from lararium.modules.finance.valuation import balances_asof, net_worth_series

from .conftest import B_IDENTIFIER, WHEN

DAY = date(2026, 11, 3)


async def _balances(session, day: date = DAY) -> dict:
    return {r.account_id: r.quantity for r in await balances_asof(session, day)}


async def _net_worth(session, day: date = DAY) -> Decimal:
    [point] = await net_worth_series(session, day, day)
    return point.value


async def _contra(session, transaction_id, own_account_id):
    return await session.scalar(
        text(
            "select account_id from finance.postings"
            " where transaction_id = :t and account_id <> :own"
        ),
        {"t": transaction_id, "own": own_account_id},
    )


async def _placeholders(session) -> list:
    rows = await session.execute(
        text(
            "select t.id, t.description, t.status, t.occurred_at, t.booking_date"
            " from finance.transactions t where t.origin = 'inference' order by t.occurred_at"
        )
    )
    return list(rows)


async def _postings(session, transaction_id) -> set[tuple]:
    rows = await session.execute(
        text("select account_id, quantity from finance.postings where transaction_id = :t"),
        {"t": transaction_id},
    )
    return {(r.account_id, r.quantity) for r in rows}


async def _consistent(session) -> None:
    assert await find_transfer_violations(session) == []
    assert await find_unbalanced_transactions(session) == []


async def test_outgoing_to_manual_account(
    session, run, ids, connected_bank, manual_bank, connector_tx, heal
) -> None:
    a = await connected_bank("A")
    b = await manual_bank("B")
    worth_before = await _net_worth(session)
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)

    record = await connector_tx(a, "-100", identifier_id=identifier)
    # Identified through the registry, so it sits in In transit before matching runs.
    assert await _contra(session, record.id, a.account.id) == ids.in_transit
    assert await _net_worth(session) == worth_before

    assert await heal() == 1

    assert await _contra(session, record.id, a.account.id) == ids.in_transit
    [placeholder] = await _placeholders(session)
    assert (placeholder.description, placeholder.status) == ("Transfer from A", "inferred")
    assert (placeholder.occurred_at, placeholder.booking_date) == (
        record.occurred_at,
        record.booking_date,
    )
    assert await _postings(session, placeholder.id) == {
        (b.id, Decimal("100")),
        (ids.in_transit, Decimal("-100")),
    }
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.review_reason) == ("inferred", None)
    assert (transfer.from_account_id, transfer.to_account_id, transfer.amount) == (
        a.account.id,
        b.id,
        Decimal("100"),
    )
    assert (
        transfer.out_transaction_id,
        transfer.in_transaction_id,
        transfer.placeholder_transaction_id,
    ) == (record.id, None, placeholder.id)
    assert transfer.opened_at == record.occurred_at
    balances = await _balances(session)
    assert balances[ids.in_transit] == 0
    assert balances[b.id] == Decimal("1100")
    assert await _net_worth(session) == worth_before
    await _consistent(session)


async def test_incoming_from_manual_account(
    session, run, ids, connected_bank, manual_bank, connector_tx, heal
) -> None:
    a = await connected_bank("A")
    b = await manual_bank("B")
    worth_before = await _net_worth(session)
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)

    record = await connector_tx(a, "100", identifier_id=identifier)
    assert await heal() == 1

    assert await _contra(session, record.id, a.account.id) == ids.in_transit
    [placeholder] = await _placeholders(session)
    assert placeholder.description == "Transfer to A"
    assert await _postings(session, placeholder.id) == {
        (b.id, Decimal("-100")),
        (ids.in_transit, Decimal("100")),
    }
    [transfer] = await list_transfers(session)
    assert (transfer.status, transfer.from_account_id, transfer.to_account_id) == (
        "inferred",
        b.id,
        a.account.id,
    )
    assert (transfer.out_transaction_id, transfer.in_transaction_id) == (None, record.id)
    balances = await _balances(session)
    assert balances[ids.in_transit] == 0
    assert balances[b.id] == Decimal("900")
    assert await _net_worth(session) == worth_before
    await _consistent(session)


async def test_unidentified_stays_unassigned(
    session, run, ids, connected_bank, manual_bank, connector_tx, heal
) -> None:
    a = await connected_bank("A")
    await manual_bank("B")
    stranger = await run(get_or_create_identifier, "uk_bank_account", "998877:11223344")

    named = await connector_tx(a, "-100", external_id="tx_1", identifier_id=stranger)
    unnamed = await connector_tx(a, "-50", external_id="tx_2")
    assert await heal() == 0

    for record in (named, unnamed):
        assert await _contra(session, record.id, a.account.id) == ids.unassigned
    assert await list_transfers(session) == []
    assert await _placeholders(session) == []
    await _consistent(session)


async def test_identifier_of_own_account_is_not_a_transfer(
    session, run, ids, connected_bank, connector_tx, heal
) -> None:
    a = await connected_bank("A")
    own = await run(assign_identifier, a.account.id, "uk_bank_account", "223344:87654321")

    record = await connector_tx(a, "-100", identifier_id=own.id)
    assert await heal() == 0

    assert await _contra(session, record.id, a.account.id) == ids.unassigned
    assert await list_transfers(session) == []
    await _consistent(session)


async def test_rejected_record_stays_unassigned(
    session, run, ids, connected_bank, manual_bank, connector_tx, heal
) -> None:
    a = await connected_bank("A")
    await manual_bank("B")
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    source = ("connection", f"{a.connection_id}:tx_1")
    # Writing decisions is a later task; the engine only has to honour one.
    await session.execute(
        insert(models.TransferDecision).values(
            kind="reject", subject_kind=source[0], subject_ref=source[1]
        )
    )

    record = await connector_tx(a, "-100", identifier_id=identifier)
    assert await source_ref(session, record.id) == source
    assert await _contra(session, record.id, a.account.id) == ids.unassigned
    assert await heal() == 0
    assert await list_transfers(session) == []
    await _consistent(session)


async def test_heal_twice_writes_nothing(
    session, run, connected_bank, manual_bank, connector_tx, heal
) -> None:
    a = await connected_bank("A")
    await manual_bank("B")
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    await connector_tx(a, "-100", external_id="tx_1", identifier_id=identifier)
    await connector_tx(a, "40", external_id="tx_2", identifier_id=identifier)
    assert await heal() == 2

    async def state() -> tuple:
        tables = []
        for query in (
            "select id, occurred_at, booking_date, description, status, origin"
            " from finance.transactions order by id",
            "select id, transaction_id, account_id, quantity from finance.postings order by id",
            "select * from finance.transfers order by id",
            "select count(*) from core.events",
        ):
            tables.append([tuple(r) for r in await session.execute(text(query))])
        return tuple(tables)

    before = await state()
    assert await heal() == 0
    assert await state() == before
    changes = await session.scalar(
        text(
            "select changes from core.audit_log where command = 'finance.heal_transfers'"
            " order by id desc limit 1"
        )
    )
    assert changes == {}


async def test_placeholder_follows_its_record(
    session, run, ids, connected_bank, manual_bank, connector_tx, heal
) -> None:
    a = await connected_bank("A")
    b = await manual_bank("B")
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    await connector_tx(a, "-100", identifier_id=identifier)
    assert await heal() == 1

    # The upstream reports another amount and a later time for the same payment.
    later = WHEN + timedelta(hours=3)
    record = await connector_tx(a, "-120", identifier_id=identifier, occurred_at=later)
    assert await _contra(session, record.id, a.account.id) == ids.in_transit
    assert await heal() == 1

    [placeholder] = await _placeholders(session)
    assert placeholder.occurred_at == later
    assert await _postings(session, placeholder.id) == {
        (b.id, Decimal("120")),
        (ids.in_transit, Decimal("-120")),
    }
    [transfer] = await list_transfers(session)
    assert (transfer.amount, transfer.status, transfer.out_transaction_id) == (
        Decimal("120"),
        "inferred",
        record.id,
    )
    assert (await _balances(session))[b.id] == Decimal("1120")
    await _consistent(session)

    # Voided upstream: the placeholder and the transfer go, and B is back where it was.
    await connector_tx(a, "-120", identifier_id=identifier, occurred_at=later, status="void")
    assert await heal() == 1
    assert await list_transfers(session) == []
    assert await _placeholders(session) == []
    balances = await _balances(session)
    assert balances[b.id] == Decimal("1000")
    assert balances.get(ids.in_transit, Decimal(0)) == 0
    await _consistent(session)


async def test_placeholder_before_snapshot_absorbed(
    session, run, ids, gbp, connected_bank, manual_bank, connector_tx, heal
) -> None:
    a = await connected_bank("A")
    b = await manual_bank("B")
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    await connector_tx(a, "-100", identifier_id=identifier)
    assert await heal() == 1
    worth = await _net_worth(session)

    # The owner reads B's balance a day later: the 100 is already in it.
    await run(
        record_snapshot,
        account_id=b.id,
        instrument_id=gbp.id,
        quantity=Decimal("1100"),
        as_of=WHEN + timedelta(days=1),
    )
    day = DAY + timedelta(days=1)
    balances = await _balances(session, day)
    assert balances[b.id] == Decimal("1100")
    assert balances[ids.in_transit] == 0
    assert await _net_worth(session, day) == worth
    await _consistent(session)


async def test_registering_later_matches_history(
    session, run, ids, connected_bank, manual_bank, connector_tx, heal
) -> None:
    a = await connected_bank("A")
    b = await manual_bank("B", identifier=None)
    identifier = await run(get_or_create_identifier, "uk_bank_account", B_IDENTIFIER)
    records = [
        await connector_tx(
            a,
            "-100",
            external_id=f"tx_{n}",
            occurred_at=WHEN - timedelta(days=30 * n),
            identifier_id=identifier,
        )
        for n in range(3)
    ]
    assert await heal() == 0
    for record in records:
        assert await _contra(session, record.id, a.account.id) == ids.unassigned

    await run(assign_identifier, b.id, "uk_bank_account", B_IDENTIFIER)
    # The next sync computes the same contra as the engine now gives the records.
    assert (
        await transfer_contra(
            session,
            source=None,
            own_account_id=a.account.id,
            counterparty_identifier_id=identifier,
        )
        == ids.in_transit
    )
    assert await heal() == 3

    transfers = await list_transfers(session)
    assert [t.status for t in transfers] == ["inferred"] * 3
    # Newest first.
    assert [t.out_transaction_id for t in transfers] == [r.id for r in records]
    for record in records:
        assert await _contra(session, record.id, a.account.id) == ids.in_transit
    assert len(await _placeholders(session)) == 3
    await _consistent(session)
