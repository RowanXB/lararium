from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from financial_gate.modules.finance.errors import LedgerImbalanceError
from financial_gate.modules.finance.instruments import create_instrument
from financial_gate.modules.finance.ledger import (
    PostingInput,
    TransactionInput,
    find_unbalanced_transactions,
    post_transaction,
    void_transaction,
)

LATE_31_MARCH = datetime(2026, 3, 31, 23, 30, tzinfo=UTC)


async def test_booking_date_derived_in_reporting_timezone(run, make_account, gbp) -> None:
    monzo, manual_bank = await make_account(name="Monzo"), await make_account(name="Manual bank")
    tx = await run(
        post_transaction,
        TransactionInput(
            occurred_at=LATE_31_MARCH,
            description="Monzo to manual bank",
            postings=[
                PostingInput(monzo.id, gbp.id, Decimal("-500.00")),
                PostingInput(manual_bank.id, gbp.id, Decimal("500.00")),
            ],
        ),
    )
    assert tx.booking_date == date(2026, 4, 1)
    assert [p.quantity for p in tx.postings] == [Decimal("-500.00"), Decimal("500.00")]


async def test_unbalanced_rejected_and_nothing_written(session, run, make_account, gbp) -> None:
    a, b = await make_account(name="A"), await make_account(name="B")
    with pytest.raises(LedgerImbalanceError) as err:
        await run(
            post_transaction,
            TransactionInput(
                occurred_at=LATE_31_MARCH,
                description="bad",
                postings=[
                    PostingInput(a.id, gbp.id, Decimal("-100")),
                    PostingInput(b.id, gbp.id, Decimal("99.99")),
                ],
            ),
        )
    assert err.value.residuals == {gbp.id: Decimal("-0.01")}
    assert await session.scalar(text("select count(*) from finance.transactions")) == 0


async def test_residual_within_tolerance_accepted(run, make_account, gbp, usd) -> None:
    a, b = await make_account(name="A"), await make_account(name="B")
    tx = await run(
        post_transaction,
        TransactionInput(
            occurred_at=LATE_31_MARCH,
            description="fx",
            postings=[
                PostingInput(a.id, gbp.id, Decimal("-100.00")),
                PostingInput(
                    b.id,
                    usd.id,
                    Decimal("125.00"),
                    price_per_unit=Decimal("0.80003"),
                    price_instrument_id=gbp.id,
                ),
            ],
        ),
    )
    assert tx.status == "booked"


async def test_void_transaction(run, make_account, gbp) -> None:
    a, b = await make_account(name="A"), await make_account(name="B")
    tx = await run(
        post_transaction,
        TransactionInput(
            occurred_at=LATE_31_MARCH,
            description="x",
            postings=[
                PostingInput(a.id, gbp.id, Decimal("-1")),
                PostingInput(b.id, gbp.id, Decimal("1")),
            ],
        ),
    )
    assert (await run(void_transaction, tx.id)).status == "void"


async def test_integrity_check_finds_tampered_transaction(session, run, make_account, gbp) -> None:
    a, b = await make_account(name="A"), await make_account(name="B")
    tx = await run(
        post_transaction,
        TransactionInput(
            occurred_at=LATE_31_MARCH,
            description="x",
            postings=[
                PostingInput(a.id, gbp.id, Decimal("-1")),
                PostingInput(b.id, gbp.id, Decimal("1")),
            ],
        ),
    )
    await session.execute(
        text("update finance.postings set quantity = 2 where transaction_id = :t and quantity = 1"),
        {"t": tx.id},
    )
    assert await find_unbalanced_transactions(session) == [tx.id]


async def test_integrity_check_skips_void_transactions(session, run, make_account, gbp) -> None:
    a, b = await make_account(name="A"), await make_account(name="B")
    tx = await run(
        post_transaction,
        TransactionInput(
            occurred_at=LATE_31_MARCH,
            description="x",
            postings=[
                PostingInput(a.id, gbp.id, Decimal("-1")),
                PostingInput(b.id, gbp.id, Decimal("1")),
            ],
        ),
    )
    await run(void_transaction, tx.id)
    await session.execute(
        text("update finance.postings set quantity = 2 where transaction_id = :t and quantity = 1"),
        {"t": tx.id},
    )
    assert await find_unbalanced_transactions(session) == []


async def test_eighteen_decimals_round_trip_and_balance_in_database(
    session, run, make_account
) -> None:
    eth = await run(
        create_instrument,
        kind="crypto",
        symbol="ETH",
        name="Ether",
        decimals=18,
        quote_instrument_id=None,
        identifiers=[],
    )
    a, b, fees = [await make_account(name=n) for n in ("A", "B", "Fees")]
    quantities = [
        Decimal("-1.000000000000000001"),
        Decimal("0.999999999999999000"),
        Decimal("0.000000000000001001"),
    ]
    tx = await run(
        post_transaction,
        TransactionInput(
            occurred_at=LATE_31_MARCH,
            description="eth",
            postings=[
                PostingInput(acc.id, eth.id, q)
                for acc, q in zip((a, b, fees), quantities, strict=True)
            ],
        ),
    )
    assert [p.quantity for p in tx.postings] == quantities
    assert await find_unbalanced_transactions(session) == []
