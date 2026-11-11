from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import exc, text

from financial_gate.modules.finance.errors import NotACurrencyError
from financial_gate.modules.finance.pricing import record_fx_rate, record_price

DAY = date(2026, 11, 10)


async def test_same_day_same_source_price_is_replaced(session, run, btc, usd) -> None:
    await run(
        record_price,
        instrument_id=btc.id,
        quote_instrument_id=usd.id,
        price=Decimal("60000"),
        as_of_date=DAY,
    )
    await run(
        record_price,
        instrument_id=btc.id,
        quote_instrument_id=usd.id,
        price=Decimal("61000"),
        as_of_date=DAY,
    )
    rows = (
        (
            await session.execute(
                text("select price from finance.prices where instrument_id = :i"), {"i": btc.id}
            )
        )
        .scalars()
        .all()
    )
    assert rows == [Decimal("61000")]


async def test_fx_rate_requires_currencies(run, btc, gbp) -> None:
    with pytest.raises(NotACurrencyError):
        await run(
            record_fx_rate,
            base_instrument_id=btc.id,
            quote_instrument_id=gbp.id,
            rate=Decimal("50000"),
            as_of_date=DAY,
        )


async def test_fx_rate_must_be_positive(run, usd, gbp) -> None:
    with pytest.raises(ValueError):
        await run(
            record_fx_rate,
            base_instrument_id=usd.id,
            quote_instrument_id=gbp.id,
            rate=Decimal("0"),
            as_of_date=DAY,
        )


NON_FINITE = ["NaN", "Infinity", "-Infinity", "sNaN"]


@pytest.mark.parametrize("bad", NON_FINITE)
async def test_price_must_be_finite(run, btc, usd, bad) -> None:
    with pytest.raises(ValueError, match="finite"):
        await run(
            record_price,
            instrument_id=btc.id,
            quote_instrument_id=usd.id,
            price=Decimal(bad),
            as_of_date=DAY,
        )


@pytest.mark.parametrize("bad", NON_FINITE)
async def test_fx_rate_must_be_finite(run, usd, gbp, bad) -> None:
    with pytest.raises(ValueError, match="finite"):
        await run(
            record_fx_rate,
            base_instrument_id=usd.id,
            quote_instrument_id=gbp.id,
            rate=Decimal(bad),
            as_of_date=DAY,
        )


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize(
    ("table", "columns"),
    [
        ("finance.prices", "instrument_id, quote_instrument_id, price"),
        ("finance.fx_rates", "base_instrument_id, quote_instrument_id, rate"),
    ],
)
async def test_database_rejects_non_finite_prices_and_rates(
    session, btc, usd, gbp, table, columns, bad
) -> None:
    first = btc.id if table == "finance.prices" else usd.id
    with pytest.raises(exc.IntegrityError, match="check constraint"):
        await session.execute(
            text(
                f"insert into {table} ({columns}, as_of_date, source)"
                " values (:first, :gbp, cast(:bad as numeric), :day, 'manual')"
            ),
            {"first": first, "gbp": gbp.id, "bad": bad, "day": DAY},
        )
