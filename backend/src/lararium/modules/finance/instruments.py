from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

from psycopg.errors import UniqueViolation
from sqlalchemy import ColumnElement, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext
from lararium.core.entities import get_self_entity_id
from lararium.core.json_types import JSONValue
from lararium.modules.finance import models
from lararium.modules.finance.errors import (
    DuplicateIdentifierError,
    FinanceValidationError,
    InstrumentNotFoundError,
    UnknownCurrencyError,
)

REPORTING_CURRENCY_KEY = "finance.reporting_currency"

InstrumentKind = Literal[
    "fiat", "equity", "etf", "fund", "bond", "crypto", "option", "cfd", "other"
]
IdentifierScheme = Literal[
    "iso4217",
    "isin",
    "figi",
    "ticker_mic",
    "ibkr_conid",
    "etoro_id",
    "trading212_ticker",
    "chain_contract",
    "coingecko",
]

_UPPERCASE_SCHEMES = frozenset({"iso4217", "isin", "figi"})
_LOWERCASE_SCHEMES = frozenset({"coingecko", "chain_contract"})


@dataclass(frozen=True)
class InstrumentIdentifier:
    scheme: IdentifierScheme
    value: str


@dataclass(frozen=True)
class InstrumentRecord:
    id: UUID
    kind: InstrumentKind
    symbol: str
    name: str
    decimals: int
    quote_instrument_id: UUID | None
    hidden: bool
    identifiers: tuple[InstrumentIdentifier, ...]  # sorted by scheme


def normalize_identifier(scheme: IdentifierScheme, value: str) -> str:
    value = value.strip()
    if scheme in _UPPERCASE_SCHEMES:
        return value.upper()
    if scheme in _LOWERCASE_SCHEMES:
        return value.lower()
    return value


async def _load(session: AsyncSession, *conditions: ColumnElement[bool]) -> list[InstrumentRecord]:
    """Instruments matching all conditions, sorted by symbol (then id), with identifiers."""
    # Select columns, not ORM objects, so a stale identity map can't hide an update.
    rows = (
        await session.execute(
            select(
                models.Instrument.id,
                models.Instrument.kind,
                models.Instrument.symbol,
                models.Instrument.name,
                models.Instrument.decimals,
                models.Instrument.quote_instrument_id,
                models.Instrument.hidden,
            )
            .where(*conditions)
            .order_by(models.Instrument.symbol, models.Instrument.id)
        )
    ).all()
    identifiers: defaultdict[UUID, list[InstrumentIdentifier]] = defaultdict(list)
    if rows:
        identifier_rows = await session.execute(
            select(
                models.InstrumentIdentifier.instrument_id,
                models.InstrumentIdentifier.scheme,
                models.InstrumentIdentifier.value,
            )
            .where(models.InstrumentIdentifier.instrument_id.in_([row.id for row in rows]))
            .order_by(models.InstrumentIdentifier.scheme, models.InstrumentIdentifier.value)
        )
        for instrument_id, scheme, value in identifier_rows:
            identifiers[instrument_id].append(
                InstrumentIdentifier(cast(IdentifierScheme, scheme), value)
            )
    return [
        InstrumentRecord(
            id=row.id,
            kind=cast(InstrumentKind, row.kind),
            symbol=row.symbol,
            name=row.name,
            decimals=row.decimals,
            quote_instrument_id=row.quote_instrument_id,
            hidden=row.hidden,
            identifiers=tuple(identifiers[row.id]),
        )
        for row in rows
    ]


def _event_data(record: InstrumentRecord) -> dict[str, JSONValue]:
    return {
        "id": str(record.id),
        "kind": record.kind,
        "symbol": record.symbol,
        "name": record.name,
        "decimals": record.decimals,
        "quote_instrument_id": (
            None if record.quote_instrument_id is None else str(record.quote_instrument_id)
        ),
        "hidden": record.hidden,
        "identifiers": [{"scheme": i.scheme, "value": i.value} for i in record.identifiers],
    }


async def get_instrument(session: AsyncSession, instrument_id: UUID) -> InstrumentRecord:
    found = await _load(session, models.Instrument.id == instrument_id)
    if not found:
        raise InstrumentNotFoundError(str(instrument_id))
    return found[0]


async def find_by_identifier(
    session: AsyncSession, scheme: IdentifierScheme, value: str
) -> InstrumentRecord | None:
    owner = (
        select(models.InstrumentIdentifier.instrument_id)
        .where(models.InstrumentIdentifier.scheme == scheme)
        .where(models.InstrumentIdentifier.value == normalize_identifier(scheme, value))
        .scalar_subquery()
    )
    found = await _load(session, models.Instrument.id == owner)
    return found[0] if found else None


async def get_currency(session: AsyncSession, iso_code: str) -> InstrumentRecord:
    found = await find_by_identifier(session, "iso4217", iso_code)
    if found is None:
        raise UnknownCurrencyError(iso_code)
    return found


async def list_instruments(
    session: AsyncSession, *, include_hidden: bool
) -> list[InstrumentRecord]:
    if include_hidden:
        return await _load(session)
    return await _load(session, models.Instrument.hidden.is_(False))


def _require_iso4217_code_iff_fiat(
    kind: InstrumentKind, identifiers: Sequence[InstrumentIdentifier]
) -> None:
    # Currencies are looked up by ISO 4217 code (get_currency), so the code is what makes an
    # instrument a currency: a fiat without one could never be priced or converted, and
    # anything else with one would be taken for a currency. Instruments are only created
    # here, and neither kind nor identifiers change afterwards, so this holds for all rows.
    codes = [i for i in identifiers if i.scheme == "iso4217"]
    if kind == "fiat" and len(codes) != 1:
        raise FinanceValidationError("a fiat currency needs exactly one ISO 4217 code")
    if kind != "fiat" and codes:
        raise FinanceValidationError("only a fiat currency can have an ISO 4217 code")


async def create_instrument(
    ctx: CommandContext,
    *,
    kind: InstrumentKind,
    symbol: str,
    name: str,
    decimals: int,
    quote_instrument_id: UUID | None,
    identifiers: Sequence[InstrumentIdentifier],
) -> InstrumentRecord:
    _require_iso4217_code_iff_fiat(kind, identifiers)
    session = ctx.session
    instrument = models.Instrument(
        kind=kind,
        symbol=symbol,
        name=name,
        decimals=decimals,
        quote_instrument_id=quote_instrument_id,
        owner_entity_id=await get_self_entity_id(session),
    )
    session.add(instrument)
    await session.flush()
    if identifiers:
        # A Core insert bypasses the identity map, so a conflict always reaches the database.
        statement = insert(models.InstrumentIdentifier).values(
            [
                {
                    "instrument_id": instrument.id,
                    "scheme": identifier.scheme,
                    "value": normalize_identifier(identifier.scheme, identifier.value),
                }
                for identifier in identifiers
            ]
        )
        try:
            await session.execute(statement)
        except IntegrityError as error:
            if isinstance(error.orig, UniqueViolation):
                raise DuplicateIdentifierError(
                    ", ".join(f"{i.scheme}:{i.value}" for i in identifiers)
                ) from error
            raise
    record = await get_instrument(session, instrument.id)
    data = _event_data(record)
    ctx.emit("finance.instrument.created", record.id, data)
    ctx.record_change(f"instrument:{record.id}", data)
    return record


async def add_identifiers(
    ctx: CommandContext, instrument_id: UUID, identifiers: Sequence[InstrumentIdentifier]
) -> InstrumentRecord:
    """Add the identifiers of schemes the instrument has none of yet.

    For a source that learns more about an instrument it already knows (an ISIN-matched
    listing gets the source's own id). An identifier of a scheme the instrument has stays as
    it is, and one already on another instrument is skipped, so the call never fails over a
    value the registry holds elsewhere. Currencies keep their ISO 4217 code and no other
    instrument gets one, so that scheme is refused.
    """
    if any(i.scheme == "iso4217" for i in identifiers):
        raise FinanceValidationError("an ISO 4217 code can't be added to an instrument")
    current = await get_instrument(ctx.session, instrument_id)
    have = {i.scheme for i in current.identifiers}
    added = False
    for identifier in identifiers:
        if identifier.scheme in have:
            continue
        value = normalize_identifier(identifier.scheme, identifier.value)
        # A conditional insert, so a value another instrument holds is skipped, not an error.
        i = models.InstrumentIdentifier
        inserted = await ctx.session.scalar(
            pg_insert(i)
            .values(instrument_id=instrument_id, scheme=identifier.scheme, value=value)
            .on_conflict_do_nothing()
            .returning(i.value)
        )
        if inserted is not None:
            have.add(identifier.scheme)
            added = True
    if not added:
        return current
    record = await get_instrument(ctx.session, instrument_id)
    data = _event_data(record)
    ctx.emit("finance.instrument.updated", record.id, data)
    ctx.record_change(f"instrument:{record.id}", data)
    return record


async def replace_identifier(
    ctx: CommandContext, instrument_id: UUID, identifier: InstrumentIdentifier
) -> InstrumentRecord:
    """Give the instrument's identifier of the scheme a new value: a source that renamed a
    listing it knows (Trading 212 renames tickers). One of a scheme the instrument has none of
    is added instead. The value must not be another instrument's: the caller looked it up.
    Currencies keep their ISO 4217 code, so that scheme is refused."""
    if identifier.scheme == "iso4217":
        raise FinanceValidationError("an ISO 4217 code can't be replaced")
    current = await get_instrument(ctx.session, instrument_id)
    if identifier.scheme not in {i.scheme for i in current.identifiers}:
        return await add_identifiers(ctx, instrument_id, [identifier])
    i = models.InstrumentIdentifier
    value = normalize_identifier(identifier.scheme, identifier.value)
    replaced = await ctx.session.scalar(
        update(i)
        .where(i.instrument_id == instrument_id, i.scheme == identifier.scheme, i.value != value)
        .values(value=value)
        .returning(i.value)
    )
    if replaced is None:
        return current
    record = await get_instrument(ctx.session, instrument_id)
    data = _event_data(record)
    ctx.emit("finance.instrument.updated", record.id, data)
    ctx.record_change(f"instrument:{record.id}", data)
    return record


async def set_kind(
    ctx: CommandContext, instrument_id: UUID, kind: InstrumentKind
) -> InstrumentRecord:
    """Reclassify a security a source first could not type (Trading 212's, before its
    metadata arrives). A currency stays one and nothing becomes one: the ISO 4217 code is
    what makes an instrument a currency."""
    current = await get_instrument(ctx.session, instrument_id)
    if kind == "fiat" or current.kind == "fiat":
        raise FinanceValidationError("an instrument can't become or stop being a currency")
    if current.kind == kind:
        return current
    await ctx.session.execute(
        update(models.Instrument).where(models.Instrument.id == instrument_id).values(kind=kind)
    )
    record = await get_instrument(ctx.session, instrument_id)
    data = _event_data(record)
    ctx.emit("finance.instrument.updated", record.id, data)
    ctx.record_change(f"instrument:{record.id}", data)
    return record


async def set_hidden(ctx: CommandContext, instrument_id: UUID, hidden: bool) -> InstrumentRecord:
    updated = await ctx.session.scalar(
        update(models.Instrument)
        .where(models.Instrument.id == instrument_id)
        .values(hidden=hidden)
        .returning(models.Instrument.id)
    )
    if updated is None:
        raise InstrumentNotFoundError(str(instrument_id))
    record = await get_instrument(ctx.session, instrument_id)
    data: dict[str, JSONValue] = {"id": str(record.id), "hidden": record.hidden}
    ctx.emit("finance.instrument.updated", record.id, data)
    ctx.record_change(f"instrument:{record.id}", data)
    return record
