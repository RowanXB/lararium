from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import exc, text

from lararium.core.connections import create_connection
from lararium.modules.finance import ledger
from lararium.modules.finance.errors import FinanceValidationError, TransactionNotFoundError
from lararium.modules.finance.identifiers import get_or_create_identifier
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    delete_derived_transaction,
    find_unbalanced_transactions,
    post_transaction,
    repoint_contra,
    set_counterparty_fields,
    upsert_connector_transaction,
)
from lararium.modules.finance.system_accounts import get_or_create_system_account
from lararium.modules.finance.valuation import balances_asof, net_worth_series

WHEN = datetime(2026, 11, 2, 12, tzinfo=UTC)


async def _event_count(session, event_type: str) -> int:
    return await session.scalar(
        text("select count(*) from core.events where type = :t"), {"t": event_type}
    )


def _connector_tx(connection_id, own, contra, gbp, counterparty: CounterpartyFields):
    return TransactionInput(
        occurred_at=WHEN,
        description="To Test Owner",
        origin="connector",
        connection_id=connection_id,
        external_id="tx_1",
        postings=[
            PostingInput(own.id, gbp.id, Decimal("-100")),
            PostingInput(contra, gbp.id, Decimal("100")),
        ],
        counterparty=counterparty,
    )


async def test_counterparty_fields_round_trip(session, run, make_account, gbp) -> None:
    identifier = await run(get_or_create_identifier, "uk_bank_account", "11-22-33 12345678")
    a, b = await make_account(name="A"), await make_account(name="B")
    fields = CounterpartyFields(
        identifier_id=identifier, name="Test Owner", reference="rent", bank_transfer=True
    )
    posted = await run(
        post_transaction,
        TransactionInput(
            occurred_at=WHEN,
            description="Rent",
            postings=[
                PostingInput(a.id, gbp.id, Decimal("-100")),
                PostingInput(b.id, gbp.id, Decimal("100")),
            ],
            counterparty=fields,
        ),
    )
    assert posted.counterparty == fields
    row = (
        await session.execute(
            text(
                "select counterparty_identifier_id, counterparty_name, payment_reference,"
                " bank_transfer from finance.transactions where id = :i"
            ),
            {"i": posted.id},
        )
    ).one()
    assert tuple(row) == (identifier, "Test Owner", "rent", True)
    # The audit row shows them too.
    changes = await session.scalar(
        text("select changes from core.audit_log order by id desc limit 1")
    )
    assert changes[f"transaction:{posted.id}"]["counterparty"] == {
        "identifier_id": str(identifier),
        "name": "Test Owner",
        "reference": "rent",
        "bank_transfer": True,
    }
    # A transaction posted without them gets the defaults.
    plain = await run(
        post_transaction,
        TransactionInput(
            occurred_at=WHEN,
            description="Plain",
            postings=[
                PostingInput(a.id, gbp.id, Decimal("-1")),
                PostingInput(b.id, gbp.id, Decimal("1")),
            ],
        ),
    )
    assert plain.counterparty == CounterpartyFields()


async def test_upsert_treats_changed_counterparty_as_update(
    session, run, make_account, gbp
) -> None:
    conn = await run(create_connection, connector="monzo", display_name="m", config={})
    a = await make_account(name="A", balance_mode="transactions")
    unassigned = await run(get_or_create_system_account, "unassigned")
    created, _ = await run(
        upsert_connector_transaction,
        _connector_tx(conn.id, a, unassigned, gbp, CounterpartyFields(name="Test Owner")),
    )
    changed = CounterpartyFields(name="Test Owner", reference="savings", bank_transfer=True)
    updated, outcome = await run(
        upsert_connector_transaction, _connector_tx(conn.id, a, unassigned, gbp, changed)
    )
    assert outcome == "updated"
    assert updated.id == created.id
    assert updated.counterparty == changed
    assert await _event_count(session, "finance.transaction.updated") == 1


async def test_upsert_same_counterparty_unchanged(session, run, make_account, gbp) -> None:
    conn = await run(create_connection, connector="monzo", display_name="m", config={})
    identifier = await run(get_or_create_identifier, "uk_bank_account", "112233:12345678")
    a = await make_account(name="A", balance_mode="transactions")
    unassigned = await run(get_or_create_system_account, "unassigned")
    fields = CounterpartyFields(identifier_id=identifier, name="Test Owner", bank_transfer=True)
    tx = _connector_tx(conn.id, a, unassigned, gbp, fields)
    created, _ = await run(upsert_connector_transaction, tx)
    same, outcome = await run(upsert_connector_transaction, tx)
    assert outcome == "unchanged"
    assert same == created


async def test_set_counterparty_fields_writes_only_when_different(
    session, run, make_account, gbp
) -> None:
    a, b = await make_account(name="A"), await make_account(name="B")
    posted = await run(
        post_transaction,
        TransactionInput(
            occurred_at=WHEN,
            description="Rent",
            postings=[
                PostingInput(a.id, gbp.id, Decimal("-100")),
                PostingInput(b.id, gbp.id, Decimal("100")),
            ],
        ),
    )
    fields = CounterpartyFields(name="Test Owner", reference="rent", bank_transfer=True)
    assert await run(set_counterparty_fields, posted.id, fields) is True
    assert await run(set_counterparty_fields, posted.id, fields) is False
    assert await _event_count(session, "finance.transaction.updated") == 1
    stored = await session.scalar(
        text("select payment_reference from finance.transactions where id = :i"),
        {"i": posted.id},
    )
    assert stored == "rent"
    with pytest.raises(TransactionNotFoundError):
        await run(set_counterparty_fields, a.id, fields)


async def test_repoint_contra_moves_one_posting_and_keeps_balance(
    session, run, make_account, gbp
) -> None:
    a = await make_account(name="A", balance_mode="transactions")
    unassigned = await run(get_or_create_system_account, "unassigned")
    in_transit = await run(get_or_create_system_account, "in_transit")
    posted = await run(
        post_transaction,
        TransactionInput(
            occurred_at=WHEN,
            description="To savings",
            postings=[
                PostingInput(a.id, gbp.id, Decimal("-100")),
                PostingInput(unassigned, gbp.id, Decimal("100")),
            ],
        ),
    )
    contra_posting = posted.postings[1]
    assert await run(repoint_contra, posted.id, unassigned, in_transit) is True
    rows = {
        r.id: (r.account_id, r.quantity)
        for r in await session.execute(
            text("select id, account_id, quantity from finance.postings where transaction_id = :i"),
            {"i": posted.id},
        )
    }
    assert rows == {
        posted.postings[0].id: (a.id, Decimal("-100")),
        contra_posting.id: (in_transit, Decimal("100")),
    }
    assert await find_unbalanced_transactions(session) == []
    assert await _event_count(session, "finance.transaction.updated") == 1
    balances = {r.account_id: r.quantity for r in await balances_asof(session, date(2026, 11, 3))}
    assert balances[in_transit] == Decimal("100")
    assert unassigned not in balances
    # Nothing left on Unassigned: no posting to move.
    assert await run(repoint_contra, posted.id, unassigned, in_transit) is False
    assert await _event_count(session, "finance.transaction.updated") == 1
    with pytest.raises(FinanceValidationError):
        await run(repoint_contra, posted.id, in_transit, in_transit)


async def test_repoint_contra_after_postings_replaced_does_nothing(
    session, run, make_account, gbp, monkeypatch
) -> None:
    a = await make_account(name="A", balance_mode="transactions")
    unassigned = await run(get_or_create_system_account, "unassigned")
    in_transit = await run(get_or_create_system_account, "in_transit")
    posted = await run(
        post_transaction,
        TransactionInput(
            occurred_at=WHEN,
            description="To savings",
            postings=[
                PostingInput(a.id, gbp.id, Decimal("-100")),
                PostingInput(unassigned, gbp.id, Decimal("100")),
            ],
        ),
    )
    get_account = ledger.get_account

    async def upsert_replaces_postings_meanwhile(*args, **kwargs):
        # Between repoint_contra's read and its UPDATE, as a concurrent upsert would.
        await session.execute(
            text("delete from finance.postings where transaction_id = :t"), {"t": posted.id}
        )
        for position, (account, quantity) in enumerate(((a.id, -100), (unassigned, 100))):
            await session.execute(
                text(
                    "insert into finance.postings"
                    " (transaction_id, position, account_id, instrument_id, quantity)"
                    " values (:t, :p, :a, :i, :q)"
                ),
                {"t": posted.id, "p": position, "a": account, "i": gbp.id, "q": quantity},
            )
        return await get_account(*args, **kwargs)

    monkeypatch.setattr(ledger, "get_account", upsert_replaces_postings_meanwhile)
    assert await run(repoint_contra, posted.id, unassigned, in_transit) is False
    assert await _event_count(session, "finance.transaction.updated") == 0
    on_in_transit = await session.scalar(
        text("select count(*) from finance.postings where transaction_id = :t and account_id = :a"),
        {"t": posted.id, "a": in_transit},
    )
    assert on_in_transit == 0


async def test_repoint_contra_refuses_several_postings(session, run, make_account, gbp) -> None:
    a = await make_account(name="A", balance_mode="transactions")
    unassigned = await run(get_or_create_system_account, "unassigned")
    in_transit = await run(get_or_create_system_account, "in_transit")
    posted = await run(
        post_transaction,
        TransactionInput(
            occurred_at=WHEN,
            description="Split",
            postings=[
                PostingInput(a.id, gbp.id, Decimal("-100")),
                PostingInput(unassigned, gbp.id, Decimal("60")),
                PostingInput(unassigned, gbp.id, Decimal("40")),
            ],
        ),
    )
    with pytest.raises(FinanceValidationError):
        await run(repoint_contra, posted.id, unassigned, in_transit)
    with pytest.raises(TransactionNotFoundError):
        await run(repoint_contra, a.id, unassigned, in_transit)


async def test_delete_derived_refuses_connector_rows(session, run, make_account, gbp) -> None:
    conn = await run(create_connection, connector="monzo", display_name="m", config={})
    a = await make_account(name="A", balance_mode="transactions")
    b = await make_account(name="B", balance_mode="hybrid")
    unassigned = await run(get_or_create_system_account, "unassigned")
    in_transit = await run(get_or_create_system_account, "in_transit")
    connector, _ = await run(
        upsert_connector_transaction,
        _connector_tx(conn.id, a, unassigned, gbp, CounterpartyFields()),
    )
    with pytest.raises(FinanceValidationError):
        await run(delete_derived_transaction, connector.id)
    assert await session.scalar(
        text("select count(*) from finance.transactions where id = :i"), {"i": connector.id}
    )
    placeholder = await run(
        post_transaction,
        TransactionInput(
            occurred_at=WHEN,
            description="Transfer from A",
            status="inferred",
            origin="inference",
            postings=[
                PostingInput(b.id, gbp.id, Decimal("100")),
                PostingInput(in_transit, gbp.id, Decimal("-100")),
            ],
        ),
    )
    await run(delete_derived_transaction, placeholder.id)
    for table, column in (("transactions", "id"), ("postings", "transaction_id")):
        assert (
            await session.scalar(
                text(f"select count(*) from finance.{table} where {column} = :i"),
                {"i": placeholder.id},
            )
            == 0
        )
    assert await _event_count(session, "finance.transaction.deleted") == 1
    with pytest.raises(TransactionNotFoundError):
        await run(delete_derived_transaction, placeholder.id)


async def test_in_transit_is_an_asset_counted_in_net_worth(session, run, gbp) -> None:
    in_transit = await run(get_or_create_system_account, "in_transit")
    assert await run(get_or_create_system_account, "in_transit") == in_transit
    row = (
        await session.execute(
            text(
                "select name, ledger_type, subtype, balance_mode from finance.accounts"
                " where id = :i"
            ),
            {"i": in_transit},
        )
    ).one()
    assert tuple(row) == ("In transit", "asset", "in_transit", "transactions")
    opening = await run(get_or_create_system_account, "opening_balances")
    await run(
        post_transaction,
        TransactionInput(
            occurred_at=WHEN,
            description="parked",
            postings=[
                PostingInput(in_transit, gbp.id, Decimal("100")),
                PostingInput(opening, gbp.id, Decimal("-100")),
            ],
        ),
    )
    [point] = await net_worth_series(session, date(2026, 11, 3), date(2026, 11, 3))
    assert point.value == Decimal("100")


async def test_transfer_needs_a_leg_and_distinct_accounts(session, run, make_account, gbp) -> None:
    a, b = await make_account(name="A"), await make_account(name="B")
    posted = await run(
        post_transaction,
        TransactionInput(
            occurred_at=WHEN,
            description="leg",
            postings=[
                PostingInput(a.id, gbp.id, Decimal("-100")),
                PostingInput(b.id, gbp.id, Decimal("100")),
            ],
        ),
    )
    insert = text(
        "insert into finance.transfers"
        " (from_account_id, to_account_id, instrument_id, amount, status, out_transaction_id,"
        "  opened_at)"
        " values (:f, :t, :i, :amount, :status, :out, now())"
    )
    valid = {
        "f": a.id,
        "t": b.id,
        "i": gbp.id,
        "amount": Decimal("100"),
        "status": "awaiting",
        "out": posted.id,
    }
    for broken in (
        {"t": a.id},
        {"out": None},
        {"amount": Decimal("0")},
        {"status": "pending"},
    ):
        with pytest.raises(exc.IntegrityError, match="check constraint"):
            async with session.begin_nested():
                await session.execute(insert, valid | broken)
    async with session.begin_nested():
        await session.execute(insert, valid)
    # A transaction is a leg of at most one transfer.
    with pytest.raises(exc.IntegrityError, match="unique constraint"):
        async with session.begin_nested():
            await session.execute(insert, valid)
    # A linked transaction can't be deleted out from under its transfer.
    with pytest.raises(exc.IntegrityError, match="foreign key"):
        async with session.begin_nested():
            await session.execute(
                text("delete from finance.transactions where id = :i"), {"i": posted.id}
            )


async def test_decisions_are_unique_with_nulls(session) -> None:
    insert = text(
        "insert into finance.transfer_decisions (kind, subject_kind, subject_ref)"
        " values ('reject', 'connection', 'c:tx_1')"
    )
    async with session.begin_nested():
        await session.execute(insert)
    with pytest.raises(exc.IntegrityError, match="unique constraint"):
        async with session.begin_nested():
            await session.execute(insert)
    with pytest.raises(exc.IntegrityError, match="check constraint"):
        async with session.begin_nested():
            await session.execute(
                text(
                    "insert into finance.transfer_decisions (kind, subject_kind, subject_ref)"
                    " values ('pair', 'connection', 'c:tx_2')"
                )
            )
