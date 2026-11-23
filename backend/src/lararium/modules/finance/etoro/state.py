"""What one normalize run of an eToro connection shares: its records, accounts, instruments and
system accounts, and what it reports; and reading an item's fields.

Raw records keep every number as its exact decimal text (readonly_http.record_json). Errors
name record types, fields and eToro's position ids, never a value: a name, date of birth,
counterparty or bank detail (fields.PERSONAL) never reaches a message or a log.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import ClassVar
from uuid import UUID

from sqlalchemy import ColumnElement, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core import models as core_models
from lararium.core.connections import ConnectionRecord
from lararium.core.connectors import SyncContext
from lararium.core.errors import DisplayableError
from lararium.core.json_types import JSONValue
from lararium.core.raw_records import RawRecord, get_raw_records
from lararium.core.timeutil import local_date
from lararium.modules.finance import connector_items
from lararium.modules.finance.connector_items import (
    Item,
    flat,
    object_at,
    objects_at,
    text_at,
    value_at,
)
from lararium.modules.finance.connector_run import LONDON, ConnectorRun
from lararium.modules.finance.etoro.fields import COLUMNS, ENDPOINTS
from lararium.modules.finance.etoro.instruments import listing
from lararium.modules.finance.instruments import find_by_identifier
from lararium.modules.finance.report_sightings import Sighting

__all__ = ["LONDON", "Item", "flat", "object_at", "objects_at", "text_at", "value_at"]

SOURCE = "etoro"
_DIGITS = re.compile(r"[0-9]{1,19}")


class EtoroDataError(DisplayableError):
    """eToro sent what cannot be posted; the message names the record type, the field or
    eToro's position id, never a value."""


@dataclass
class NormalizeReport:
    """What a normalize run changed, and what it could not map."""

    accounts_created: int = 0
    accounts_updated: int = 0
    accounts_unchanged: int = 0
    # Ledger transactions.
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    # Flags about the records the run normalized (fields, values, types it does not know).
    flagged: set[str] = field(default_factory=set[str])
    # The run replayed every stored record, so `flagged` is all there is to flag.
    replayed: bool = False
    # Flags every run finds in full: instruments whose type eToro has not given, or gave as
    # one the ledger does not hold, or whose currency it has not given; positions whose
    # settlement no source states.
    flags_in_full: set[str] = field(default_factory=set[str])


@dataclass(frozen=True)
class Record:
    id: UUID
    endpoint: str
    content: Item
    fetched_at: datetime
    sync_id: UUID

    @property
    def record_type(self) -> str:
        return ENDPOINTS[self.endpoint].record_type


@dataclass(frozen=True)
class Report:
    """A report record as one sync saw it (report_sightings): it holds at `seen_at`."""

    record: Record
    sync_id: UUID
    seen_at: datetime

    @property
    def endpoint(self) -> str:
        return self.record.endpoint

    @property
    def content(self) -> Item:
        return self.record.content

    @property
    def day(self) -> date:
        return local_date(self.seen_at, LONDON)

    @property
    def order(self) -> tuple[datetime, UUID, UUID]:
        return self.seen_at, self.sync_id, self.record.id


_ENDPOINT_OF = {spec.record_type: name for name, spec in ENDPOINTS.items()}


def as_record(raw: RawRecord) -> Record | None:
    """The raw record as an item of its endpoint; None for one that is no endpoint's: a
    statement row (Task 11's), or a refused key's scopes (sync.REFUSED_KEY)."""
    endpoint = _ENDPOINT_OF.get(raw.record_type)
    if endpoint is None or not isinstance(raw.content, dict):
        return None
    return Record(raw.id, endpoint, raw.content, raw.fetched_at, raw.sync_id)


async def records_of(session: AsyncSession, ids: Sequence[UUID]) -> list[Record]:
    return [r for r in map(as_record, await get_raw_records(session, ids)) if r is not None]


async def reports_of(session: AsyncSession, sightings: Sequence[Sighting]) -> list[Report]:
    """The sightings' records, as reports, in the order seen."""
    records = {r.id: r for r in await records_of(session, [s.raw_record_id for s in sightings])}
    found = [
        Report(records[s.raw_record_id], s.sync_id, s.seen_at)
        for s in sightings
        if s.raw_record_id in records
    ]
    return sorted(found, key=lambda r: r.order)


async def stored(
    session: AsyncSession,
    connection_id: UUID,
    endpoints: Sequence[str],
    *where: ColumnElement[bool],
) -> list[Record]:
    """The connection's raw records of these endpoints (and `where`), oldest fetch first."""
    r = core_models.RawRecord
    ids = await session.scalars(
        select(r.id)
        .where(
            r.connection_id == connection_id,
            r.record_type.in_([ENDPOINTS[e].record_type for e in endpoints]),
            *where,
        )
        .order_by(r.fetched_at, r.id)
    )
    return await records_of(session, list(ids))


async def latest_sighted(
    session: AsyncSession, connection_id: UUID, endpoint: str
) -> Report | None:
    """The latest sighting of a report of the endpoint."""
    row = (
        await session.execute(
            text(
                "select s.raw_record_id, s.sync_id, s.seen_at from finance.report_sightings s"
                " join core.raw_records r on r.id = s.raw_record_id"
                " where s.connection_id = :c and r.record_type = :t"
                " order by s.seen_at desc, s.sync_id desc, s.raw_record_id desc limit 1"
            ),
            {"c": connection_id, "t": ENDPOINTS[endpoint].record_type},
        )
    ).one_or_none()
    if row is None:
        return None
    found = await reports_of(session, [Sighting(row.raw_record_id, row.sync_id, row.seen_at)])
    return found[0] if found else None


# Reading items ---------------------------------------------------------------------------


def number_or_none(record: Record, item: Item | None, path: str) -> Decimal | None:
    """The field as a number; None when absent."""
    return connector_items.number_or_none(item, path, record.record_type, EtoroDataError)


def number_at(record: Record, item: Item | None, path: str) -> Decimal:
    """The field as a number; 0 when absent."""
    found = number_or_none(record, item, path)
    return Decimal(0) if found is None else found


def id_at(record: Record, item: Item | None, path: str) -> str:
    """An id (a whole number) in its plain text, without leading zeros or a decimal part."""
    value = number_or_none(record, item, path)
    if value is None or value != value.to_integral_value() or value <= 0:
        raise EtoroDataError(f"{record.record_type} {path} is not an id")
    plain = str(int(value))
    if _DIGITS.fullmatch(plain) is None:
        raise EtoroDataError(f"{record.record_type} {path} is not an id")
    return plain


def moment_at(record: Record, item: Item | None, path: str) -> datetime:
    return connector_items.moment_at(item, path, record.record_type, EtoroDataError)


def decimal_text(record: Record, value: str, path: str) -> Decimal:
    """A number already read as text (text_at), as a Decimal."""
    try:
        return Decimal(value)
    except InvalidOperation:
        raise EtoroDataError(f"{record.record_type} {path} is not a number") from None


def ending(position_id: str) -> str:
    """A position as messages name it: its last four digits (design §5.13)."""
    return f"···{position_id[-4:]}"


def record_key(record: Record) -> str:
    """The item's key: the ledger's external id (Decision 9) for a close or an eToro Money
    transaction, else what the item describes; a report's is empty (it is dated instead)."""
    content = record.content
    match record.endpoint:
        case "trade_history":
            return f"close:{id_at(record, content, 'positionId')}"
        case "cash_transactions":
            return f"cash:{text_at(content, 'id')}"
        case "order_lookup":
            return id_at(record, content, "orderId")
        case "instruments":
            return id_at(record, content, "instrumentID")
        case "instrument_types":
            return id_at(record, content, "instrumentTypeID")
        case "exchanges":
            return id_at(record, content, "exchangeID")
        case "candles":
            return f"{id_at(record, content, 'instrumentId')}:{text_at(content, 'time')}"
        case "closed_events":
            return f"{text_at(content, 'closeYear')}:{text_at(content, 'assetType')}"
        case "stockback":
            # A payment has no required field: each record is its own.
            return str(record.id)
        case _:
            return ""


# One run ---------------------------------------------------------------------------------


class Run(ConnectorRun):
    COLUMNS: ClassVar = COLUMNS
    RECORD_TYPES: ClassVar = {name: spec.record_type for name, spec in ENDPOINTS.items()}
    # eToro's enum values are camelCase words, or numbers.
    CODE: ClassVar = re.compile(r"[A-Za-z0-9][A-Za-z0-9_]{0,39}")

    def __init__(
        self, ctx: SyncContext, connection: ConnectionRecord, report: NormalizeReport
    ) -> None:
        super().__init__(ctx, connection, report)
        self.report = report
        # eToro account id → finance account id, as the connection's config keeps it.
        self.account_map: dict[str, JSONValue] = {}
        # The accounts the balances list, by eToro account id, and their types.
        self.accounts: dict[str, UUID] = {}
        self.account_types: dict[str, str] = {}
        self.account_currencies: dict[str, str] = {}
        self.trading_id: str | None = None
        # The Trading account's currency (accountCurrency, design §3.1).
        self.account_currency = ""
        # eToro instrument id → its asset currency (the aggregate's or the breakdown's), and
        # a symbol a position or order gave.
        self.asset_currencies: dict[str, str] = {}
        self.symbols: dict[str, str] = {}
        # eToro instrument id → the metadata's (type id, symbol, display name), None unlisted.
        self.metadata: dict[str, tuple[str, str, str] | None] = {}
        # (instrument, currency, day) → price: current rates and daily candles.
        self.current_prices: dict[tuple[UUID, UUID, date], Decimal] = {}
        self.candle_prices: dict[tuple[UUID, UUID, date], Decimal] = {}
        # eToro instrument id → the ISIN a statement's Closed Positions gave a position of it.
        self.isins: dict[str, str] = {}
        # (event key, field) of each review item this run opened (merge.review), to tell.
        self.reviews: list[tuple[str, str]] = []
        self._instruments: dict[str, UUID | None] = {}

    @property
    def trading(self) -> UUID:
        if self.trading_id is None:
            raise EtoroDataError("eToro's balances list no Trading account")
        return self.accounts[self.trading_id]

    async def load_metadata(self, ids: set[str]) -> None:
        """The metadata of these instruments, read at once (else `instrument` reads each)."""
        wanted = sorted(ids - set(self.metadata))
        if not wanted:
            return
        rows = await self.session.execute(
            text(
                "select instrument_id::text as id, instrument_type_id::text as type,"
                " symbol_full, instrument_display_name from finance.etoro_instruments"
                " where connection_id = :c and instrument_id = any(cast(:i as numeric[]))"
            ),
            {"c": self.connection_id, "i": wanted},
        )
        self.metadata |= dict.fromkeys(wanted)
        self.metadata |= {
            row.id: (row.type or "", row.symbol_full or "", row.instrument_display_name or "")
            for row in rows
        }

    async def instrument(self, etoro_id: str) -> UUID | None:
        """The instrument eToro's id names, found or created; None for a type the ledger does
        not hold (flagged), or for one new to the ledger whose asset currency neither the
        aggregate nor the breakdown has given (flagged in full: instruments.currency_flags),
        which is created, and its rows posted, once one does."""
        if etoro_id in self._instruments:
            return self._instruments[etoro_id]
        await self.load_metadata({etoro_id})
        type_id, symbol, name = self.metadata[etoro_id] or ("", "", "")
        symbol = symbol or self.symbols.get(etoro_id, "") or f"eToro {etoro_id}"
        currency = self.asset_currencies.get(etoro_id, "")
        found: UUID | None
        if not currency and await find_by_identifier(self.session, "etoro_id", etoro_id) is None:
            found = None
        else:
            found = await listing(
                self.command, int(etoro_id), symbol, name, currency, self.isins.get(etoro_id)
            )
            if found is None:
                self.flag_value("instruments", "instrumentTypeID", type_id)
        self._instruments[etoro_id] = found
        return found

    def forget_instruments(self) -> None:
        """Drop what `instrument` found: a merge has made two of them one."""
        self._instruments.clear()
        self._symbols.clear()

    async def contra(self, key: str, account_id: UUID, identifier_id: UUID | None) -> UUID:
        """Unassigned or In transit for a movement of two postings, as transfer_contra says."""
        return await self.contra_for(key, account_id, 0, identifier_id)
