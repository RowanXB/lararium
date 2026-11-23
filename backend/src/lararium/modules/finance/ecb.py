"""ECB reference rates (through Frankfurter) as a gap-filling FX source, source `ecb`.

An ECB rate is written for a currency pair and day only when no other source has a rate
between the two currencies on that day, in either direction, and it is removed when one
arrives. Valuation takes the latest rate of any source and breaks a same-day tie by write
order, so an unconditional ECB rate could otherwise replace IBKR's.
"""

import json
from collections.abc import Mapping
from datetime import date, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID

import httpx
from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from lararium.core.actors import Actor
from lararium.core.commands import CommandContext, command_context
from lararium.core.json_types import JSONValue
from lararium.modules.finance import models
from lararium.modules.finance.pricing import record_fx_rate

SOURCE = "ecb"
ECB_ACTOR = Actor("system", "ecb")
BASE_CURRENCY = "EUR"
# The one place the host and path are written down; the path is `<from>..<to>?base=EUR`.
FRANKFURTER_URL = "https://api.frankfurter.dev/v1"
# Frankfurter thins a long time series to weekly points, so a long range is asked for in
# windows short enough to stay daily.
CHUNK_DAYS = 90


async def fetch_rates(
    http: httpx.AsyncClient, start: date, end: date
) -> dict[date, dict[str, Decimal]]:
    """The ECB's EUR rates for every working day from start to end, by day and currency."""
    response = await http.get(
        f"{FRANKFURTER_URL}/{start.isoformat()}..{end.isoformat()}", params={"base": BASE_CURRENCY}
    )
    response.raise_for_status()
    body: object = json.loads(response.text, parse_float=Decimal)
    rates = cast(dict[str, object], body).get("rates") if isinstance(body, dict) else None
    if not isinstance(rates, dict):
        raise ValueError("the ECB response has no rates object")
    parsed: dict[date, dict[str, Decimal]] = {}
    for day, by_currency in cast(dict[str, object], rates).items():
        if not isinstance(by_currency, dict):
            raise ValueError("the ECB response has a malformed day")
        parsed[date.fromisoformat(day)] = {
            code: _rate(value) for code, value in cast(dict[str, object], by_currency).items()
        }
    return parsed


def _rate(value: object) -> Decimal:
    if isinstance(value, Decimal) or (isinstance(value, int) and not isinstance(value, bool)):
        return Decimal(value)
    raise ValueError("the ECB response has a rate that is not a number")


async def _fiat_ids(session: AsyncSession) -> dict[str, UUID]:
    rows = await session.execute(
        select(models.InstrumentIdentifier.value, models.InstrumentIdentifier.instrument_id)
        .join(models.Instrument, models.Instrument.id == models.InstrumentIdentifier.instrument_id)
        .where(models.InstrumentIdentifier.scheme == "iso4217", models.Instrument.kind == "fiat")
    )
    return {code: instrument_id for code, instrument_id in rows}


async def store_rates(ctx: CommandContext, rates: Mapping[date, Mapping[str, Decimal]]) -> int:
    """Record the ECB's rates for the currencies the ledger has, except where another source
    already has a rate for the pair and day; return how many rows were written (a rate that
    is already stored unchanged writes nothing)."""
    session = ctx.session
    ids = await _fiat_ids(session)
    eur = ids.get(BASE_CURRENCY)
    if eur is None or not rates:
        return 0
    r = models.FxRate
    days = list(rates)
    others = {
        (row.base_instrument_id, row.quote_instrument_id, row.as_of_date)
        for row in await session.execute(
            select(r.base_instrument_id, r.quote_instrument_id, r.as_of_date).where(
                r.source != SOURCE,
                r.as_of_date.in_(days),
                or_(r.base_instrument_id == eur, r.quote_instrument_id == eur),
            )
        )
    }
    stored = {
        (row.quote_instrument_id, row.as_of_date): row.rate
        for row in await session.execute(
            select(r.quote_instrument_id, r.as_of_date, r.rate).where(
                r.source == SOURCE, r.base_instrument_id == eur, r.as_of_date.in_(days)
            )
        )
    }
    written = 0
    for day, by_currency in sorted(rates.items()):
        for code, rate in sorted(by_currency.items()):
            quote = ids.get(code)
            if quote is None or quote == eur or rate <= 0:
                continue
            if (eur, quote, day) in others or (quote, eur, day) in others:
                continue
            if stored.get((quote, day)) == rate:
                continue
            await record_fx_rate(
                ctx,
                base_instrument_id=eur,
                quote_instrument_id=quote,
                rate=rate,
                as_of_date=day,
                source=SOURCE,
            )
            written += 1
    return written


async def drop_superseded(ctx: CommandContext) -> int:
    """Delete the `ecb` rates for a pair and day that another source has since given a rate
    for, in either direction; return how many."""
    r = models.FxRate
    other = aliased(models.FxRate)
    removed = (
        await ctx.session.execute(
            delete(r)
            .where(
                r.source == SOURCE,
                exists().where(
                    other.source != SOURCE,
                    other.as_of_date == r.as_of_date,
                    or_(
                        and_(
                            other.base_instrument_id == r.base_instrument_id,
                            other.quote_instrument_id == r.quote_instrument_id,
                        ),
                        and_(
                            other.base_instrument_id == r.quote_instrument_id,
                            other.quote_instrument_id == r.base_instrument_id,
                        ),
                    ),
                ),
            )
            .returning(r.id, r.base_instrument_id, r.quote_instrument_id, r.as_of_date)
        )
    ).all()
    for row in removed:
        data: dict[str, JSONValue] = {
            "id": str(row.id),
            "base_instrument_id": str(row.base_instrument_id),
            "quote_instrument_id": str(row.quote_instrument_id),
            "as_of_date": row.as_of_date.isoformat(),
            "source": SOURCE,
        }
        ctx.emit("finance.fx_rate.removed", row.id, data)
        ctx.record_change(f"fx_rate_removed:{row.id}", data)
    return len(removed)


async def _first_needed_date(session: AsyncSession) -> date | None:
    """The day after the latest `ecb` rate, else the earliest booking date of any posting."""
    r = models.FxRate
    latest = await session.scalar(select(func.max(r.as_of_date)).where(r.source == SOURCE))
    if latest is not None:
        return latest + timedelta(days=1)
    return await session.scalar(
        select(func.min(models.Transaction.booking_date)).where(
            exists().where(models.Posting.transaction_id == models.Transaction.id)
        )
    )


async def run_fetch_ecb_rates(
    session_factory: async_sessionmaker[AsyncSession], http: httpx.AsyncClient, today: date
) -> int:
    """Fetch the ECB's rates from the first day a ledger needs them to `today`, store the
    ones that fill a gap and drop those another source has superseded; return how many rates
    were written. A window that is stored stays so if a later one fails: the next run carries
    on after it."""
    async with session_factory() as session:
        start = await _first_needed_date(session)
    if start is None:
        return 0
    written = 0
    while start <= today:
        end = min(start + timedelta(days=CHUNK_DAYS - 1), today)
        rates = await fetch_rates(http, start, end)
        async with (
            session_factory() as session,
            command_context(
                session,
                ECB_ACTOR,
                "finance.fetch_ecb_rates",
                {"start": start.isoformat(), "end": end.isoformat()},
            ) as ctx,
        ):
            written += await store_rates(ctx, rates)
            await drop_superseded(ctx)
        start = end + timedelta(days=1)
    return written
