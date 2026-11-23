"""Finding or creating the instrument of a Trading 212 row (design §5.6).

An instrument is a listing: a security in one currency. One is found by Trading 212's ticker,
then by ISIN in the same currency (a listing IBKR or eToro holds, or the same one under a
ticker Trading 212 has since renamed), and created when neither finds it. A hit lacking the
ticker gets it.

Trading 212 renames tickers, and its history may keep returning the old one (design §6). A
position's ticker is the current one: an instrument found by ISIN for a position takes it in
place of the ticker it holds. A history row's ticker never replaces one, so an old ticker in
the history cannot take the name back.

An instrument Trading 212's metadata does not list (yet, or any more) is made with kind
`other`, and flagged while it stays so (`unknown_types`); the metadata's type reclassifies it
when it arrives (`reclassify`).
"""

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import exists, select, text
from sqlalchemy.ext.asyncio import AsyncSession

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
    set_kind,
)

# Trading 212's instrument types the ledger holds (design §5.6); any other is not posted.
KINDS: Mapping[str, InstrumentKind] = {"STOCK": "equity", "ETF": "etf"}
# Trading 212's precision of fractional shares (design §5.6).
DECIMALS = 8
SCHEME = "trading212_ticker"


def instrument_kind(trading212_type: str | None) -> InstrumentKind | None:
    """The kind of instrument a metadata type is; None for a type the ledger does not hold.
    Without metadata the type is unknown: `other`."""
    if trading212_type is None:
        return "other"
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
    if found.kind == "other" and kind != "other":
        await set_kind(ctx, found.id, kind)
    return found.id


async def reclassify(ctx: CommandContext, connection_id: UUID) -> None:
    """Give each instrument made without a type the kind the connection's metadata now says
    its ticker is."""
    rows = await ctx.session.execute(
        text(
            "select i.id, m.type from finance.instruments i"
            " join finance.instrument_identifiers ii"
            " on ii.instrument_id = i.id and ii.scheme = 'trading212_ticker'"
            " join finance.trading212_instruments m"
            " on m.connection_id = :c and m.ticker = ii.value"
            " where i.kind = 'other' and m.type = any(cast(:t as text[]))"
        ),
        {"c": connection_id, "t": sorted(KINDS)},
    )
    for row in rows.all():
        await set_kind(ctx, row.id, KINDS[row.type])


async def unknown_types(session: AsyncSession, account_id: UUID) -> list[str]:
    """The tickers of the account's instruments (posted or reported on it) still without a
    type."""
    i, ii = models.Instrument, models.InstrumentIdentifier
    p, s = models.Posting, models.BalanceSnapshot
    used = exists().where(p.instrument_id == i.id, p.account_id == account_id) | exists().where(
        s.instrument_id == i.id, s.account_id == account_id
    )
    tickers = await session.scalars(
        select(ii.value)
        .join(i, i.id == ii.instrument_id)
        .where(ii.scheme == SCHEME, i.kind == "other", used)
        .order_by(ii.value)
    )
    return list(tickers)
