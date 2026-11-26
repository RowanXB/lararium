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
`other`, and flagged while it stays so (`type_flags`); the metadata's type reclassifies it
when it arrives (`reclassify`). Whether a row posts follows the metadata's type now, live
and on replay alike: one the ledger does not hold posts nothing, and an instrument found to be
one has what it posted voided.
"""

from collections.abc import Collection, Mapping
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext
from lararium.modules.finance.connector_run import listing_currency
from lararium.modules.finance.instruments import (
    InstrumentIdentifier,
    InstrumentKind,
    InstrumentRecord,
    add_identifiers,
    create_instrument,
    find_by_identifier,
    find_same_listing,
    get_currency,
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


def _ticker(record: InstrumentRecord) -> str | None:
    return next((i.value for i in record.identifiers if i.scheme == SCHEME), None)


async def listing(
    ctx: CommandContext,
    instrument: Mapping[str, str],
    *,
    current: bool = False,
    held: Collection[str] = (),
) -> UUID | None:
    """The instrument a row names, created if new; None for a type the ledger does not hold.

    `instrument` is a row's instrument object (ticker, name, isin, currency), with the
    metadata's `type` and `shortName` when Trading 212 lists the ticker. `current`: the row
    is a position, so its ticker is the one Trading 212 uses now, and the instrument takes it
    unless the ticker it holds is one of `held` (this sync's positions' tickers): two held
    tickers of one listing would otherwise take it from each other every sync."""
    kind = instrument_kind(instrument.get("type"))
    if kind is None:
        return None
    ticker = instrument["ticker"].strip()
    currency = await get_currency(ctx.session, listing_currency(instrument["currency"])[0])
    isin = instrument.get("isin", "").strip()
    identifiers = [InstrumentIdentifier(SCHEME, ticker)]
    if isin:
        identifiers.append(InstrumentIdentifier("isin", isin))

    found = await find_by_identifier(ctx.session, SCHEME, ticker)
    if found is None and isin:
        found = await find_same_listing(ctx.session, isin, currency.id)
        if found is not None and current and _ticker(found) not in held:
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


async def reclassify(ctx: CommandContext, connection_id: UUID) -> list[UUID]:
    """Give each instrument made without a type the kind the connection's metadata now says
    its ticker is; returns those the metadata says the ledger does not hold (their postings
    are not postable: records.void_unsupported)."""
    rows = await ctx.session.execute(
        text(
            "select i.id, m.type from finance.instruments i"
            " join finance.instrument_identifiers ii"
            " on ii.instrument_id = i.id and ii.scheme = 'trading212_ticker'"
            " join finance.trading212_instruments m"
            " on m.connection_id = :c and m.ticker = ii.value"
            " where i.kind = 'other'"
        ),
        {"c": connection_id},
    )
    unsupported: list[UUID] = []
    for row in rows.all():
        kind = KINDS.get(row.type)
        if kind is None:
            unsupported.append(row.id)
        else:
            await set_kind(ctx, row.id, kind)
    return sorted(unsupported)


UNKNOWN = "instrument type unknown"
UNSUPPORTED = "instrument type unsupported"


async def type_flags(session: AsyncSession, connection_id: UUID, account_id: UUID) -> set[str]:
    """The account's instruments (posted or reported on it) made without a type: those the
    metadata has not typed yet, and those it typed as one the ledger does not hold."""
    rows = await session.execute(
        text(
            "select ii.value as ticker, m.type from finance.instruments i"
            " join finance.instrument_identifiers ii"
            " on ii.instrument_id = i.id and ii.scheme = 'trading212_ticker'"
            " left join finance.trading212_instruments m"
            " on m.connection_id = :c and m.ticker = ii.value"
            " where i.kind = 'other' and ("
            " exists (select 1 from finance.postings p"
            " where p.instrument_id = i.id and p.account_id = :a)"
            " or exists (select 1 from finance.balance_snapshots s"
            " where s.instrument_id = i.id and s.account_id = :a))"
        ),
        {"c": connection_id, "a": account_id},
    )
    flags: set[str] = set()
    for row in rows:
        if row.type is None:
            flags.add(f"{UNKNOWN} {row.ticker}")
        elif row.type not in KINDS:
            flags.add(f"{UNSUPPORTED} {row.ticker}")
    return flags
