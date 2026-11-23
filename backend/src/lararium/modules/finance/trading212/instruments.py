"""Finding or creating the instrument of a Trading 212 row (design §5.6).

An instrument is a listing: a security in one currency. One is found by Trading 212's ticker,
then by ISIN in the same currency (a listing IBKR or eToro holds, or the same one under a
ticker Trading 212 has since renamed), and created when neither finds it. A hit lacking the
ticker gets it.

Trading 212 renames tickers, and its history may keep returning the old one (design §6). A
position's ticker is the current one: an instrument found by ISIN for a position takes it in
place of the ticker it holds. A history row's ticker never replaces one, so an old ticker in
the history cannot take the name back.
"""

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import select

from lararium.core.commands import CommandContext
from lararium.modules.finance import models
from lararium.modules.finance.instruments import (
    InstrumentIdentifier,
    InstrumentKind,
    InstrumentRecord,
    add_identifiers,
    create_instrument,
    find_by_identifier,
    get_currency,
    get_instrument,
    normalize_identifier,
    replace_identifier,
)

# Trading 212's instrument types the ledger holds (design §5.6); any other is not posted.
KINDS: Mapping[str, InstrumentKind] = {"STOCK": "equity", "ETF": "etf"}
# Trading 212's precision of fractional shares (design §5.6).
DECIMALS = 8
SCHEME = "trading212_ticker"


def instrument_kind(trading212_type: str | None) -> InstrumentKind | None:
    """The kind of instrument a metadata type is; None for a type the ledger does not hold.
    Without metadata (an instrument Trading 212 no longer lists) a share is assumed."""
    if trading212_type is None:
        return "equity"
    return KINDS.get(trading212_type)


def symbol(instrument: Mapping[str, str]) -> str:
    """The metadata's short name, else the ticker before its exchange suffix."""
    short = instrument.get("shortName", "").strip()
    return short or instrument["ticker"].split("_", 1)[0]


async def _same_listing(
    ctx: CommandContext, isin: str, quote_instrument_id: UUID
) -> InstrumentRecord | None:
    """An instrument with the ISIN, quoted in the currency."""
    i, instrument = models.InstrumentIdentifier, models.Instrument
    found = await ctx.session.scalar(
        select(instrument.id)
        .join(i, i.instrument_id == instrument.id)
        .where(
            i.scheme == "isin",
            i.value == normalize_identifier("isin", isin),
            instrument.quote_instrument_id == quote_instrument_id,
        )
        .order_by(instrument.id)
        .limit(1)
    )
    return None if found is None else await get_instrument(ctx.session, found)


async def listing(
    ctx: CommandContext, instrument: Mapping[str, str], *, current: bool = False
) -> UUID | None:
    """The instrument a row names, created if new; None for a type the ledger does not hold.

    `instrument` is a row's instrument object (ticker, name, isin, currency), with the
    metadata's `type` and `shortName` when Trading 212 lists the ticker. `current`: the row
    is a position, so its ticker is the one Trading 212 uses now."""
    kind = instrument_kind(instrument.get("type"))
    if kind is None:
        return None
    ticker = instrument["ticker"].strip()
    currency = await get_currency(ctx.session, instrument["currency"])
    isin = instrument.get("isin", "").strip()
    identifiers = [InstrumentIdentifier(SCHEME, ticker)]
    if isin:
        identifiers.append(InstrumentIdentifier("isin", isin))

    found = await find_by_identifier(ctx.session, SCHEME, ticker)
    if found is None and isin:
        found = await _same_listing(ctx, isin, currency.id)
        if found is not None and current:
            # Found by ISIN, so no instrument holds this ticker: it can take it.
            found = await replace_identifier(ctx, found.id, identifiers[0])
    if found is None:
        # The registry holds an identifier once: a second listing of one ISIN goes without it.
        free = [
            i
            for i in identifiers
            if await find_by_identifier(ctx.session, i.scheme, i.value) is None
        ]
        name = symbol(instrument)
        created = await create_instrument(
            ctx,
            kind=kind,
            symbol=name,
            name=instrument.get("name", "").strip() or name,
            decimals=DECIMALS,
            quote_instrument_id=currency.id,
            identifiers=free,
        )
        return created.id
    await add_identifiers(ctx, found.id, identifiers)
    return found.id
