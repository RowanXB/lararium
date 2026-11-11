from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

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
