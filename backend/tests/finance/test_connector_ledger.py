from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import exc, text

from lararium.core.connections import create_connection
from lararium.modules.finance.errors import FinanceValidationError
from lararium.modules.finance.ledger import (
    PostingInput,
    TransactionInput,
    post_transaction,
    upsert_connector_transaction,
    void_transaction,
)
from lararium.modules.finance.system_accounts import get_or_create_system_account
from lararium.modules.finance.valuation import balances_asof, net_worth_series

WHEN = datetime(2026, 11, 2, 12, tzinfo=UTC)


async def _connection(run):
    return await run(create_connection, connector="monzo", display_name="m", config={})


def _tx(connection_id, a, b, gbp, amount: str, description: str = "Coffee", **kw):
    return TransactionInput(
        occurred_at=WHEN,
        description=description,
        origin="connector",
        connection_id=connection_id,
        external_id="tx_1",
        postings=[
            PostingInput(a.id, gbp.id, Decimal(f"-{amount}")),
            PostingInput(b.id, gbp.id, Decimal(amount)),
        ],
        **kw,
    )


async def test_upsert_creates_then_unchanged(session, run, make_account, gbp) -> None:
    conn = await _connection(run)
    a, b = await make_account(name="A"), await make_account(name="B")
    tx = _tx(conn.id, a, b, gbp, "5.00")
    created, outcome = await run(upsert_connector_transaction, tx)
    assert outcome == "created"
    events = await session.scalar(text("select count(*) from core.events"))
    same, outcome = await run(upsert_connector_transaction, tx)
    assert outcome == "unchanged"
    assert same.id == created.id
    assert await session.scalar(text("select count(*) from core.events")) == events
    # the command's own audit row is still written, but it records no change
    changes = await session.scalar(
        text("select changes from core.audit_log order by id desc limit 1")
    )
    assert changes == {}


async def test_upsert_replaces_postings_in_place(session, run, make_account, gbp) -> None:
    conn = await _connection(run)
    monzo = await make_account(name="Monzo", balance_mode="transactions")
    other = await make_account(name="Other", ledger_type="equity", subtype="other")
    created, _ = await run(upsert_connector_transaction, _tx(conn.id, other, monzo, gbp, "5.00"))
    updated, outcome = await run(
        upsert_connector_transaction,
        _tx(conn.id, other, monzo, gbp, "7.50", description="Coffee and cake"),
    )
    assert outcome == "updated"
    assert updated.id == created.id
    assert updated.description == "Coffee and cake"
    old_ids = {p.id for p in created.postings}
    remaining = {r.id for r in await session.execute(text("select id from finance.postings"))}
    assert not old_ids & remaining
    assert await session.scalar(text("select count(*) from finance.transactions")) == 1
    rows = {r.account_id: r.quantity for r in await balances_asof(session, date(2026, 11, 3))}
    assert rows[monzo.id] == Decimal("7.50")
    updates = await session.scalar(
        text("select count(*) from core.events where type = 'finance.transaction.updated'")
    )
    assert updates == 1


async def test_upsert_refuses_manual_rows(session, run, make_account, gbp) -> None:
    conn = await _connection(run)
    a, b = await make_account(name="A"), await make_account(name="B")
    manual = await run(
        post_transaction,
        TransactionInput(
            occurred_at=WHEN,
            description="manual",
            postings=[
                PostingInput(a.id, gbp.id, Decimal("-1")),
                PostingInput(b.id, gbp.id, Decimal("1")),
            ],
            connection_id=conn.id,
            external_id="tx_1",
        ),
    )
    with pytest.raises(FinanceValidationError):
        await run(upsert_connector_transaction, _tx(conn.id, a, b, gbp, "2.00"))
    row = await session.scalar(
        text("select description from finance.transactions where id = :i"), {"i": manual.id}
    )
    assert row == "manual"


async def test_refs_must_be_paired(session, run, make_account, gbp) -> None:
    conn = await _connection(run)
    with pytest.raises(exc.IntegrityError):
        await session.execute(
            text(
                "insert into finance.transactions"
                " (occurred_at, booking_date, status, origin, owner_entity_id, connection_id)"
                " select now(), current_date, 'booked', 'connector', id, :c"
                " from core.entities limit 1"
            ),
            {"c": conn.id},
        )


async def test_system_accounts_reused_and_excluded_from_net_worth(
    session, run, make_account, gbp
) -> None:
    unassigned = await run(get_or_create_system_account, "unassigned")
    assert await run(get_or_create_system_account, "unassigned") == unassigned
    opening = await run(get_or_create_system_account, "opening_balances")
    assert opening != unassigned
    row = (
        await session.execute(
            text("select ledger_type, subtype, balance_mode from finance.accounts where id = :i"),
            {"i": unassigned},
        )
    ).one()
    assert tuple(row) == ("equity", "other", "transactions")
    monzo = await make_account(name="Monzo", balance_mode="transactions")
    await run(
        post_transaction,
        TransactionInput(
            occurred_at=WHEN,
            description="opening",
            postings=[
                PostingInput(monzo.id, gbp.id, Decimal("100")),
                PostingInput(opening, gbp.id, Decimal("-100")),
            ],
        ),
    )
    [point] = await net_worth_series(session, date(2026, 11, 3), date(2026, 11, 3))
    assert point.value == Decimal("100")


async def test_owner_void_is_sticky(session, run, make_account, gbp) -> None:
    conn = await _connection(run)
    a, b = await make_account(name="A"), await make_account(name="B")
    created, _ = await run(upsert_connector_transaction, _tx(conn.id, a, b, gbp, "5.00"))
    await run(void_transaction, created.id)
    record, outcome = await run(
        upsert_connector_transaction, _tx(conn.id, a, b, gbp, "9.00", status="booked")
    )
    assert outcome == "unchanged"
    assert record.status == "void"
    status = await session.scalar(
        text("select status from finance.transactions where id = :i"), {"i": created.id}
    )
    assert status == "void"
    quantities = {
        r.quantity
        for r in await session.execute(
            text("select quantity from finance.postings where transaction_id = :i"),
            {"i": created.id},
        )
    }
    assert quantities == {Decimal("5.00"), Decimal("-5.00")}
    updates = await session.scalar(
        text("select count(*) from core.events where type = 'finance.transaction.updated'")
    )
    assert updates == 0
