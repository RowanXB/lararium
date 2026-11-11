from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import exc, text

from lararium.modules.finance.errors import LedgerImbalanceError
from lararium.modules.finance.instruments import create_instrument
from lararium.modules.finance.ledger import (
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


async def test_residual_just_above_tolerance_rejected_at_full_precision(
    session, run, make_account, gbp
) -> None:
    # The residual has 31 significant digits; rounding it to 28 would land exactly on the
    # GBP tolerance (0.005) and wrongly accept it.
    a, b = await make_account(name="A"), await make_account(name="B")
    postings = [
        PostingInput(a.id, gbp.id, Decimal("-100.005000000000000000000000000000001")),
        PostingInput(b.id, gbp.id, Decimal("100")),
    ]
    with pytest.raises(LedgerImbalanceError) as err:
        await run(
            post_transaction,
            TransactionInput(occurred_at=LATE_31_MARCH, description="x", postings=postings),
        )
    assert err.value.residuals == {gbp.id: Decimal("-0.005000000000000000000000000000001")}

    # The database check agrees once the same residual is written behind the ledger's back.
    tx = await run(
        post_transaction,
        TransactionInput(
            occurred_at=LATE_31_MARCH,
            description="x",
            postings=[
                PostingInput(a.id, gbp.id, Decimal("-100")),
                PostingInput(b.id, gbp.id, Decimal("100")),
            ],
        ),
    )
    await session.execute(
        text(
            "update finance.postings set quantity = -100.005000000000000000000000000000001"
            " where transaction_id = :t and quantity = -100"
        ),
        {"t": tx.id},
    )
    assert await find_unbalanced_transactions(session) == [tx.id]


@pytest.mark.parametrize("count", [0, 1])
async def test_fewer_than_two_postings_rejected(run, make_account, gbp, count) -> None:
    a = await make_account(name="A")
    with pytest.raises(ValueError, match="at least two postings"):
        await run(
            post_transaction,
            TransactionInput(
                occurred_at=LATE_31_MARCH,
                description="x",
                postings=[PostingInput(a.id, gbp.id, Decimal("1"))][:count],
            ),
        )


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "sNaN"])
@pytest.mark.parametrize("field", ["quantity", "cost_per_unit", "price_per_unit"])
async def test_non_finite_posting_amounts_rejected(
    session, run, make_account, gbp, usd, field, bad
) -> None:
    a, b = await make_account(name="A"), await make_account(name="B")
    amounts = {
        "quantity": Decimal("1"),
        "cost_per_unit": None,
        "price_per_unit": None,
    } | {field: Decimal(bad)}
    posting = PostingInput(
        a.id,
        usd.id,
        amounts["quantity"],
        cost_per_unit=amounts["cost_per_unit"],
        cost_instrument_id=gbp.id if amounts["cost_per_unit"] is not None else None,
        price_per_unit=amounts["price_per_unit"],
        price_instrument_id=gbp.id if amounts["price_per_unit"] is not None else None,
    )
    with pytest.raises(ValueError, match="finite"):
        await run(
            post_transaction,
            TransactionInput(
                occurred_at=LATE_31_MARCH,
                description="x",
                postings=[posting, PostingInput(b.id, gbp.id, Decimal("-1"))],
            ),
        )
    assert await session.scalar(text("select count(*) from finance.transactions")) == 0


async def test_database_rejects_non_finite_posting_amounts(session, run, make_account, gbp, usd):
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
    with pytest.raises(exc.IntegrityError, match="check constraint"):
        await session.execute(
            text(
                "update finance.postings set cost_per_unit = 'NaN', cost_instrument_id = :usd"
                " where transaction_id = :t"
            ),
            {"t": tx.id, "usd": usd.id},
        )


async def _post_annotated(run, a, b, gbp, usd, **annotation):
    """2 USD bought for 1.60 GBP, the USD leg annotated with a cost or a price in GBP."""
    return await run(
        post_transaction,
        TransactionInput(
            occurred_at=LATE_31_MARCH,
            description="annotated",
            postings=[
                PostingInput(a.id, gbp.id, Decimal("-1.60")),
                PostingInput(b.id, usd.id, Decimal("2"), **annotation),
            ],
        ),
    )


@pytest.mark.parametrize("column", ["cost_per_unit", "price_per_unit"])
async def test_integrity_check_weighs_cost_and_price_legs(
    session, run, make_account, gbp, usd, column
) -> None:
    a, b = await make_account(name="A"), await make_account(name="B")
    instrument_column = column.replace("_per_unit", "_instrument_id")
    tx = await _post_annotated(
        run, a, b, gbp, usd, **{column: Decimal("0.80"), instrument_column: gbp.id}
    )
    assert await find_unbalanced_transactions(session) == []
    await session.execute(
        text(
            f"update finance.postings set {column} = 0.81"
            " where transaction_id = :t and instrument_id = :usd"
        ),
        {"t": tx.id, "usd": usd.id},
    )
    assert await find_unbalanced_transactions(session) == [tx.id]
