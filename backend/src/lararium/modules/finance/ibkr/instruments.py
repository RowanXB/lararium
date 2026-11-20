"""Finding or creating the instrument of an IBKR row (design 5.6).

An instrument is a listing: a security traded in one currency. A UCITS ETF listed in GBP and in
USD has one ISIN but two conids, so it is two instruments, and the registry holds the ISIN once.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import aliased

from lararium.core.commands import CommandContext
from lararium.modules.finance import models
from lararium.modules.finance.ibkr import tables
from lararium.modules.finance.ibkr.parser import FlexDataError, Row
from lararium.modules.finance.instruments import (
    IdentifierScheme,
    InstrumentIdentifier,
    InstrumentKind,
    InstrumentRecord,
    add_identifiers,
    create_instrument,
    find_by_identifier,
    get_currency,
    get_instrument,
    normalize_identifier,
)

# IBKR's precision of fractional shares.
DECIMALS = 4


def instrument_kind(attributes: dict[str, str]) -> InstrumentKind | None:
    """The kind of instrument an asset category is; None for one that is not supported."""
    category = attributes.get("assetCategory", "")
    if category == "STK":
        return "etf" if attributes.get("subCategory", "") == "ETF" else "equity"
    if category == "FUND":
        return "fund"
    return None


def _identifiers(attributes: dict[str, str]) -> list[InstrumentIdentifier]:
    """The identifiers a row carries, the conid first."""
    found: list[InstrumentIdentifier] = []
    if attributes.get("conid", "").strip():
        found.append(InstrumentIdentifier("ibkr_conid", attributes["conid"]))
    if attributes.get("isin", "").strip():
        found.append(InstrumentIdentifier("isin", attributes["isin"]))
    if attributes.get("figi", "").strip():
        found.append(InstrumentIdentifier("figi", attributes["figi"]))
    return found


async def _same_listing(
    ctx: CommandContext, scheme: IdentifierScheme, value: str, quote_instrument_id: UUID
) -> InstrumentRecord | None:
    """An instrument with the ISIN or FIGI, quoted in the currency and with no conid yet."""
    i, instrument = models.InstrumentIdentifier, models.Instrument
    has_conid = aliased(models.InstrumentIdentifier)
    conid_taken = select(has_conid.instrument_id).where(has_conid.scheme == "ibkr_conid")
    found = await ctx.session.scalar(
        select(instrument.id)
        .join(i, i.instrument_id == instrument.id)
        .where(
            i.scheme == scheme,
            i.value == normalize_identifier(scheme, value),
            instrument.quote_instrument_id == quote_instrument_id,
            instrument.id.not_in(conid_taken),
        )
        .order_by(instrument.id)
        .limit(1)
    )
    return None if found is None else await get_instrument(ctx.session, found)


async def listing(ctx: CommandContext, row: Row) -> UUID | None:
    """The instrument of the row's security, created if new; None for an unsupported category.

    Found by conid, else by ISIN or FIGI in the same currency (which then gets the conid).
    The row's instrument fields are stored in ibkr_instruments.
    """
    attributes = dict(row.attributes)
    kind = instrument_kind(attributes)
    if kind is None:
        return None
    conid = attributes.get("conid", "").strip()
    if not conid:
        raise FlexDataError(f"{row.section}.conid is missing")
    currency = await get_currency(ctx.session, attributes["currency"])
    identifiers = _identifiers(attributes)

    found = await find_by_identifier(ctx.session, "ibkr_conid", conid)
    if found is None:
        for identifier in identifiers[1:]:
            found = await _same_listing(ctx, identifier.scheme, identifier.value, currency.id)
            if found is not None:
                break
    if found is None:
        symbol = attributes.get("symbol", "").strip()
        if not symbol:
            raise FlexDataError(f"{row.section}.symbol is missing")
        # The registry holds an identifier once: a second listing of one ISIN goes without it.
        free = [
            i
            for i in identifiers
            if await find_by_identifier(ctx.session, i.scheme, i.value) is None
        ]
        found = await create_instrument(
            ctx,
            kind=kind,
            symbol=symbol,
            name=attributes.get("description", "").strip() or symbol,
            decimals=DECIMALS,
            quote_instrument_id=currency.id,
            identifiers=free,
        )
    else:
        await add_identifiers(ctx, found.id, identifiers)
    values = await tables.typed_values(ctx.session, "ibkr_instruments", row.section, attributes)
    await tables.upsert_instruments(ctx, {"instrument_id": found.id}, values)
    return found.id
