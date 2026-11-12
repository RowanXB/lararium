from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext
from lararium.core.json_types import JSONValue
from lararium.modules.finance import models
from lararium.modules.finance.errors import FinanceValidationError, NotACurrencyError
from lararium.modules.finance.instruments import get_instrument
from lararium.modules.finance.money import require_finite, to_plain_str


async def _require_fiat(session: AsyncSession, instrument_id: UUID) -> None:
    instrument = await get_instrument(session, instrument_id)
    if instrument.kind != "fiat":
        raise NotACurrencyError(str(instrument_id))


def _publish(ctx: CommandContext, kind: str, row_id: UUID, data: dict[str, JSONValue]) -> None:
    ctx.emit(f"finance.{kind}.recorded", row_id, data)
    ctx.record_change(f"{kind}:{row_id}", data)


async def record_price(
    ctx: CommandContext,
    *,
    instrument_id: UUID,
    quote_instrument_id: UUID,
    price: Decimal,
    as_of_date: date,
    source: str = "manual",
) -> None:
    """Record an instrument's price in a fiat currency; a later entry for the same
    instrument, currency, date and source replaces the earlier one."""
    require_finite("price", price)
    session = ctx.session
    await get_instrument(session, instrument_id)  # raises InstrumentNotFoundError if missing
    await _require_fiat(session, quote_instrument_id)
    p = models.Price
    statement = insert(p).values(
        instrument_id=instrument_id,
        quote_instrument_id=quote_instrument_id,
        price=price,
        as_of_date=as_of_date,
        source=source,
    )
    row_id = (
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[p.instrument_id, p.quote_instrument_id, p.as_of_date, p.source],
                set_={"price": statement.excluded.price, "recorded_at": func.now()},
            ).returning(p.id)
        )
    ).scalar_one()
    _publish(
        ctx,
        "price",
        row_id,
        {
            "id": str(row_id),
            "instrument_id": str(instrument_id),
            "quote_instrument_id": str(quote_instrument_id),
            "price": to_plain_str(price),
            "as_of_date": as_of_date.isoformat(),
            "source": source,
        },
    )


async def record_fx_rate(
    ctx: CommandContext,
    *,
    base_instrument_id: UUID,
    quote_instrument_id: UUID,
    rate: Decimal,
    as_of_date: date,
    source: str = "manual",
) -> None:
    """Record how many units of the quote currency one unit of the base currency buys;
    a later entry for the same pair, date and source replaces the earlier one."""
    require_finite("rate", rate)
    if rate <= 0:
        raise FinanceValidationError("rate must be positive")
    session = ctx.session
    await _require_fiat(session, base_instrument_id)
    await _require_fiat(session, quote_instrument_id)
    r = models.FxRate
    statement = insert(r).values(
        base_instrument_id=base_instrument_id,
        quote_instrument_id=quote_instrument_id,
        rate=rate,
        as_of_date=as_of_date,
        source=source,
    )
    row_id = (
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    r.base_instrument_id,
                    r.quote_instrument_id,
                    r.as_of_date,
                    r.source,
                ],
                set_={"rate": statement.excluded.rate, "recorded_at": func.now()},
            ).returning(r.id)
        )
    ).scalar_one()
    _publish(
        ctx,
        "fx_rate",
        row_id,
        {
            "id": str(row_id),
            "base_instrument_id": str(base_instrument_id),
            "quote_instrument_id": str(quote_instrument_id),
            "rate": to_plain_str(rate),
            "as_of_date": as_of_date.isoformat(),
            "source": source,
        },
    )
