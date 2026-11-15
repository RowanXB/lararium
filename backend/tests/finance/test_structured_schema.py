import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.connections import create_connection
from lararium.core.entities import get_self_entity_id
from lararium.modules.finance.ledger import PostingInput, TransactionInput, post_transaction

WHEN = datetime(2026, 11, 2, 12, tzinfo=UTC)


async def _rejects(session: AsyncSession, sql: str, **params: object) -> None:
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(text(sql), params)


async def _transaction(run, make_account, gbp):
    a, b = await make_account(name="A"), await make_account(name="B")
    tx = await run(
        post_transaction,
        TransactionInput(
            occurred_at=WHEN,
            description="Coffee",
            origin="manual",
            postings=[
                PostingInput(account_id=a.id, instrument_id=gbp.id, quantity=Decimal("-3.00")),
                PostingInput(account_id=b.id, instrument_id=gbp.id, quantity=Decimal("3.00")),
            ],
        ),
    )
    return tx, a, gbp


async def test_original_amount_needs_both_columns(session, run, make_account, gbp) -> None:
    tx, _, _ = await _transaction(run, make_account, gbp)
    await _rejects(
        session,
        "update finance.transactions set original_quantity = 5 where id = :id",
        id=tx.id,
    )
    await _rejects(
        session,
        "update finance.transactions set original_instrument_id = :i where id = :id",
        id=tx.id,
        i=gbp.id,
    )
    await session.execute(
        text(
            "update finance.transactions set original_quantity = 5, original_instrument_id = :i"
            " where id = :id"
        ),
        {"id": tx.id, "i": gbp.id},
    )


async def test_new_transaction_columns_default_empty(session, run, make_account, gbp) -> None:
    tx, _, _ = await _transaction(run, make_account, gbp)
    row = (
        await session.execute(
            text(
                "select labels, category, include_in_spending from finance.transactions"
                " where id = :id"
            ),
            {"id": tx.id},
        )
    ).one()
    assert row.labels == [] and row.category is None and row.include_in_spending is None


async def test_mcc_must_be_four_digits(session) -> None:
    for bad in ("123", "12345", "abcd"):
        await _rejects(
            session,
            "insert into core.entities (kind, name, mcc) values ('place', 'Shop', :mcc)",
            mcc=bad,
        )
    await session.execute(
        text("insert into core.entities (kind, name, mcc) values ('place', 'Shop', '5812')")
    )


async def test_numeric_columns_reject_nan(session, make_account) -> None:
    account = await make_account()
    for column in ("goal_amount", "round_up_multiplier"):
        await _rejects(
            session,
            f"update finance.accounts set {column} = 'NaN' where id = :id",
            id=account.id,
        )
    for column in ("latitude", "longitude"):
        await _rejects(
            session,
            f"insert into core.entities (kind, name, {column}) values ('place', 'P', 'NaN')",
        )


async def test_splits_cascade_and_are_unique_per_position(session, run, make_account, gbp) -> None:
    tx, _, _ = await _transaction(run, make_account, gbp)
    insert = (
        "insert into finance.transaction_category_splits"
        " (transaction_id, position, category, quantity) values (:id, :p, 'eating_out', :q)"
    )
    await session.execute(text(insert), {"id": tx.id, "p": 0, "q": "-1.00"})
    await _rejects(session, insert, id=tx.id, p=0, q="-2.00")
    await _rejects(session, insert, id=tx.id, p=1, q="NaN")
    await session.execute(
        text("delete from finance.postings where transaction_id = :id"), {"id": tx.id}
    )
    await session.execute(text("delete from finance.transactions where id = :id"), {"id": tx.id})
    count = await session.scalar(
        text("select count(*) from finance.transaction_category_splits where transaction_id = :id"),
        {"id": tx.id},
    )
    assert count == 0


async def test_declined_unique_per_connection(session, run, make_account, gbp) -> None:
    connection = await run(create_connection, connector="monzo", display_name="m", config={})
    account = await make_account()
    owner = await get_self_entity_id(session)
    insert = """
        insert into finance.declined_transactions
          (connection_id, external_id, account_id, occurred_at, quantity, instrument_id,
           decline_reason, owner_entity_id)
        values (:c, :e, :a, :w, '-3.00', :i, 'INSUFFICIENT_FUNDS', :o)
    """
    params = {
        "c": connection.id,
        "a": account.id,
        "w": WHEN,
        "i": gbp.id,
        "o": owner,
    }
    await session.execute(text(insert), params | {"e": "tx_1"})
    await _rejects(session, insert, **params, e="tx_1")
    await session.execute(text(insert), params | {"e": "tx_2"})
    row = (
        await session.execute(
            text(
                "select description, recorded_at, updated_at from finance.declined_transactions"
                " where external_id = 'tx_1'"
            )
        )
    ).one()
    assert row.description == "" and row.recorded_at and row.updated_at
    await _rejects(session, insert, **(params | {"e": "tx_3", "o": uuid.uuid4()}))
