"""What one normalize run of a Trading 212 connection shares: its records, account,
instruments and system accounts, and what it reports; and reading an item's fields.

Raw records keep every number as its exact decimal text (readonly_http.record_json). Errors
name record types, fields and Trading 212's fill ids, never a value.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
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
from lararium.modules.finance.report_sightings import Sighting
from lararium.modules.finance.trading212.fields import COLUMNS, ENDPOINTS
from lararium.modules.finance.trading212.instruments import listing

__all__ = ["LONDON", "Item", "flat", "object_at", "objects_at", "text_at", "value_at"]

SOURCE = "trading212"


class Trading212DataError(DisplayableError):
    """Trading 212 sent what cannot be posted; the message names the record type, the field
    or Trading 212's fill id, never a value."""


@dataclass
class NormalizeReport:
    """What a normalize run changed, and what it could not map (IBKR's fields)."""

    accounts_created: int = 0
    accounts_updated: int = 0
    accounts_unchanged: int = 0
    # Ledger transactions.
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    # Flags about the records the run normalized (fields, values, types it does not know).
    flagged: set[str] = field(default_factory=set[str])
    mismatches: list[str] = field(default_factory=list[str])
    # The cursor's reconciliation keys (connector_flags.RECONCILIATION_KEYS), as written.
    cursor: dict[str, JSONValue] = field(default_factory=dict[str, JSONValue])
    # The run replayed every stored record, so `flagged` is all there is to flag.
    replayed: bool = False
    # Flags every run finds in full (instruments whose type Trading 212 has not said).
    instrument_flags: set[str] = field(default_factory=set[str])


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


async def reports_of(session: AsyncSession, sightings: Sequence[Sighting]) -> list[Report]:
    """The sightings' records, as reports, in the order seen."""
    records = {r.id: r for r in await records_of(session, [s.raw_record_id for s in sightings])}
    found = [
        Report(records[s.raw_record_id], s.sync_id, s.seen_at)
        for s in sightings
        if s.raw_record_id in records
    ]
    return sorted(found, key=lambda r: r.order)


_ENDPOINT_OF = {spec.record_type: name for name, spec in ENDPOINTS.items()}


def as_record(raw: RawRecord) -> Record | None:
    """The raw record as an item of its endpoint; None for one that is not Trading 212's."""
    endpoint = _ENDPOINT_OF.get(raw.record_type)
    if endpoint is None or not isinstance(raw.content, dict):
        return None
    return Record(raw.id, endpoint, raw.content, raw.fetched_at, raw.sync_id)


async def records_of(session: AsyncSession, ids: Sequence[UUID]) -> list[Record]:
    return [r for r in map(as_record, await get_raw_records(session, ids)) if r is not None]


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


# Reading items ---------------------------------------------------------------------------


def number_at(record: Record, item: Item | None, path: str) -> Decimal:
    """The field as a number; 0 when absent."""
    found = connector_items.number_or_none(item, path, record.record_type, Trading212DataError)
    return Decimal(0) if found is None else found


def moment_at(record: Record, item: Item | None, path: str) -> datetime:
    return connector_items.moment_at(item, path, record.record_type, Trading212DataError)


def record_key(record: Record) -> str:
    """The item's key: the ledger's external id (Decision 9) for the history, else what the
    item describes."""
    content = record.content
    match record.endpoint:
        case "orders":
            fill_id = text_at(object_at(content, "fill"), "id")
            return f"order:{fill_id}" if fill_id else f"order-none:{text_at(content, 'order.id')}"
        case "dividends":
            return f"dividend:{text_at(content, 'reference')}"
        case "transactions":
            return f"transaction:{text_at(content, 'reference')}"
        case "instruments":
            return text_at(content, "ticker")
        case "exchanges" | "pending_orders" | "pies":
            return text_at(content, "id")
        case "positions":
            return text_at(content, "instrument.ticker")
        case "pie":
            return text_at(content, "settings.id")
        case _:
            return ""


# One run ---------------------------------------------------------------------------------


class Run(ConnectorRun):
    COLUMNS: ClassVar = COLUMNS
    RECORD_TYPES: ClassVar = {name: spec.record_type for name, spec in ENDPOINTS.items()}
    CODE: ClassVar = re.compile(r"[A-Z][A-Z0-9_]{0,39}")

    def __init__(
        self, ctx: SyncContext, connection: ConnectionRecord, report: NormalizeReport
    ) -> None:
        super().__init__(ctx, connection, report)
        self.report = report
        self.account_id: UUID | None = None
        # The account's Trading 212 id.
        self.trading212_id = ""
        self.account_map: dict[str, JSONValue] = {}
        # The sibling account's registry row (accounts.sibling), once looked up.
        self.sibling: UUID | None = None
        # Ticker → the metadata's (type, short name), or None when it lists none.
        self.metadata: dict[str, tuple[str, str] | None] = {}
        # (instrument, currency, day) → price.
        self.position_prices: dict[tuple[UUID, UUID, date], Decimal] = {}
        self.fill_prices: dict[tuple[UUID, UUID, date], Decimal] = {}
        self._instruments: dict[tuple[str, bool], UUID | None] = {}
        # The tickers of this sync's positions (instruments.listing keeps theirs).
        self.held: frozenset[str] = frozenset()
        # (event key, field) of each review item this run opened (merge), to tell.

    @property
    def account(self) -> UUID:
        assert self.account_id is not None  # set before any record is applied
        return self.account_id

    async def load_metadata(self, tickers: set[str]) -> None:
        """Read the metadata of these tickers at once (else `instrument` reads each)."""
        wanted = sorted(tickers - set(self.metadata))
        if not wanted:
            return
        rows = await self.session.execute(
            text(
                "select ticker, type, short_name from finance.trading212_instruments"
                " where connection_id = :c and ticker = any(cast(:t as text[]))"
            ),
            {"c": self.connection_id, "t": wanted},
        )
        self.metadata |= dict.fromkeys(wanted)
        self.metadata |= {row.ticker: (row.type, row.short_name or "") for row in rows}

    async def instrument(
        self, record: Record, obj: Item | None, *, current: bool = False
    ) -> UUID | None:
        """The instrument of a row's instrument object; None, flagged, for a type the
        ledger does not hold. `current`: the row is one of this sync's positions."""
        ticker = text_at(obj, "ticker")
        if not ticker:
            raise Trading212DataError(f"{record.record_type} has no instrument ticker")
        if (ticker, current) in self._instruments:
            return self._instruments[(ticker, current)]
        fields = {name: text_at(obj, name) for name in ("ticker", "name", "isin", "currency")}
        await self.load_metadata({ticker})
        if (metadata := self.metadata[ticker]) is not None:
            fields["type"], fields["shortName"] = metadata
        found = await listing(self.command, fields, current=current, held=self.held)
        if found is None:
            self.flag_value("instruments", "type", fields["type"])
        else:
            await self.session.execute(
                text(
                    "update finance.trading212_instruments set instrument_id = :i"
                    " where connection_id = :c and ticker = :t"
                    " and instrument_id is distinct from cast(:i as uuid)"
                ),
                {"i": found, "c": self.connection_id, "t": ticker},
            )
        self._instruments[(ticker, current)] = found
        return found

    async def stored_records(self, table: str, column: str) -> dict[str, UUID]:
        """The raw record each of the connection's rows was last written from."""
        rows = await self.session.execute(
            text(
                f"select {column}::text as key, raw_record_id from finance.{table}"
                " where connection_id = :c"
            ),
            {"c": self.connection_id},
        )
        return {row.key: row.raw_record_id for row in rows}

    async def contra(
        self, key: str, other_postings: int, identifier_id: UUID | None = None
    ) -> UUID:
        """Unassigned or In transit, as transfer_contra says. Trading 212 names no
        counterparty, and no deposit is a bank transfer (design §5.12): a card's is paired by
        the card payment."""
        return await self.contra_for(key, self.account, other_postings, identifier_id)
