"""What one normalize run of a Trading 212 connection shares: its records, account,
instruments and system accounts, and what it reports; and reading an item's fields.

Raw records keep every number as its exact decimal text (readonly_http.record_json). Errors
name record types, fields and Trading 212's fill ids, never a value.
"""

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core import models as core_models
from lararium.core.commands import CommandContext, command_context
from lararium.core.connections import ConnectionRecord
from lararium.core.connectors import SyncContext
from lararium.core.errors import DisplayableError
from lararium.core.json_types import JSONValue
from lararium.core.raw_records import RawRecord, get_raw_records
from lararium.core.timeutil import local_date
from lararium.modules.finance import models
from lararium.modules.finance.instruments import InstrumentRecord, get_currency, get_instrument
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    upsert_connector_transaction,
    void_transaction,
)
from lararium.modules.finance.report_sightings import Sighting
from lararium.modules.finance.system_accounts import (
    SystemAccountKey,
    get_or_create_system_account,
)
from lararium.modules.finance.trading212.fields import COLUMNS, ENDPOINTS
from lararium.modules.finance.trading212.instruments import listing, listing_currency
from lararium.modules.finance.transfers import transfer_contra
from lararium.modules.finance.typed_tables import (
    Fields,
    Value,
    parse_decimal,
    parse_moment,
    typed_values,
    upsert_typed,
)

SOURCE = "trading212"
# The owner's reporting zone: a record's booking date and a report's day (Decision 11).
LONDON = ZoneInfo("Europe/London")
# An enum value fit to name in a flag.
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,39}")

type Item = dict[str, JSONValue]


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


def value_at(item: Item | None, path: str) -> JSONValue:
    value: JSONValue = item
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def text_at(item: Item | None, path: str) -> str:
    """The field as text; a number is its text (raw records keep numbers so)."""
    value = value_at(item, path)
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return value.strip() if isinstance(value, str) else ""


def object_at(item: Item | None, path: str) -> Item | None:
    value = value_at(item, path)
    return value if isinstance(value, dict) else None


def objects_at(item: Item | None, path: str) -> list[Item]:
    value = value_at(item, path)
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def number_at(record: Record, item: Item | None, path: str) -> Decimal:
    """The field as a number; 0 when absent."""
    value = value_at(item, path)
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if value is None or value == "":
        return Decimal(0)
    if not isinstance(value, str):
        raise Trading212DataError(f"{record.record_type} {path} is not a number")
    return parse_decimal(value, f"{record.record_type} {path}")


def moment_at(record: Record, item: Item | None, path: str) -> datetime:
    value = text_at(item, path)
    if not value:
        raise Trading212DataError(f"{record.record_type} lacks {path}")
    moment = parse_moment(value, f"{record.record_type} {path}")
    if moment.tzinfo is None:
        raise Trading212DataError(f"{record.record_type} {path} has no time zone")
    return moment


def flat(item: Item, prefix: str = "") -> dict[str, Value]:
    """The item's values by dotted path, arrays and maps left out: they are child rows."""
    found: dict[str, Value] = {}
    for key, value in item.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            found |= flat(value, f"{path}.")
        elif isinstance(value, bool | str) or value is None:
            found[path] = value
        elif not isinstance(value, list):
            found[path] = str(value)
    return found


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


class Run:
    def __init__(
        self, ctx: SyncContext, connection: ConnectionRecord, report: NormalizeReport
    ) -> None:
        self.ctx = ctx
        self.command = ctx.command
        self.session = ctx.command.session
        self.connection_id = connection.id
        self.config = connection.config
        # This sync: its reports are its sightings (report_sightings).
        self.sync_id = ctx.sync_id
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
        self._symbols: dict[UUID, str] = {}
        self._currencies: dict[str, InstrumentRecord] = {}
        self._shared: dict[tuple[str, ...], UUID] = {}

    @property
    def account(self) -> UUID:
        assert self.account_id is not None  # set before any record is applied
        return self.account_id

    def flag(self, name: str) -> None:
        self.report.flagged.add(name)

    def flag_value(self, endpoint: str, path: str, value: str) -> None:
        """An enum value the mapping does not know, named when it looks like one."""
        named = _CODE.fullmatch(value) is not None
        self.flag(f"{endpoint}.{path}={value}" if named else f"{endpoint}.{path}")

    def count(self, outcome: Literal["created", "updated", "unchanged"]) -> None:
        setattr(self.report, outcome, getattr(self.report, outcome) + 1)

    async def shared(
        self, key: tuple[str, ...], work: Callable[[CommandContext], Awaitable[UUID]]
    ) -> UUID:
        """A row other connections share, found or created in a transaction of its own and
        committed at once: its lock must not be held until this sync commits."""
        if key not in self._shared:
            async with (
                self.ctx.session_factory() as session,
                command_context(session, self.command.actor, "finance.shared_row", {}) as cmd,
            ):
                self._shared[key] = await work(cmd)
        return self._shared[key]

    async def system_account(self, key: SystemAccountKey) -> UUID:
        return await self.shared(
            ("system_account", key), lambda cmd: get_or_create_system_account(cmd, key)
        )

    async def currency(self, code: str) -> InstrumentRecord:
        if code not in self._currencies:
            self._currencies[code] = await get_currency(self.session, code)
        return self._currencies[code]

    async def price(self, code: str, price: Decimal) -> tuple[UUID, Decimal]:
        """A price Trading 212 gives in an instrument's currency, as finance.prices keeps it:
        (the listing's currency, the price in it); pence (GBX) are pounds / 100."""
        listed, exponent = listing_currency(code)
        return (await self.currency(listed)).id, price.scaleb(exponent)

    async def symbol(self, instrument_id: UUID) -> str:
        if instrument_id not in self._symbols:
            self._symbols[instrument_id] = (
                await get_instrument(self.session, instrument_id)
            ).symbol
        return self._symbols[instrument_id]

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
        found = await listing(self.command, fields, current=current)
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

    # Typed tables -----------------------------------------------------------------------

    async def typed(
        self, endpoint: str, table: str, values: Mapping[str, Value], under: str = ""
    ) -> dict[str, object]:
        """The values that are known fields of the table's rows, by column, typed. `under`:
        the path of the array or map the values are an entry of."""
        columns = COLUMNS[endpoint][table]
        by_column: dict[str, Value] = {}
        labels: dict[str, str] = {}
        for path, value in values.items():
            column = columns.get(f"{under}{path}")
            if column is not None:
                by_column[column] = value
                labels[column] = f"{ENDPOINTS[endpoint].record_type} {under}{path}"
        return dict(await typed_values(self.session, table, by_column, labels))

    async def upsert(self, table: str, key: Fields, values: Fields) -> None:
        await upsert_typed(self.command, table, key, values)

    async def prune(self, table: str, parent: Mapping[str, object], keep: int) -> None:
        """Delete the parent's child rows from index `keep` on: an array that shrank."""
        where = " and ".join(f"{column} = :{column}" for column in parent)
        await self.session.execute(
            text(f"delete from finance.{table} where {where} and item_index >= :keep"),
            {**parent, "keep": keep},
        )

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

    # The ledger -------------------------------------------------------------------------

    async def void(self, key: str) -> None:
        """Void the connection's live transaction under the key, if there is one: a row
        Trading 212 changed to one that posts nothing leaves no entry."""
        t = models.Transaction
        posted = await self.session.scalar(
            select(t.id).where(
                t.connection_id == self.connection_id, t.external_id == key, t.status != "void"
            )
        )
        if posted is not None:
            await void_transaction(self.command, posted, voided_by="connector")

    async def post(
        self,
        key: str,
        occurred_at: datetime,
        description: str,
        postings: Sequence[PostingInput],
        counterparty: CounterpartyFields | None = None,
    ) -> UUID:
        record, outcome = await upsert_connector_transaction(
            self.command,
            TransactionInput(
                occurred_at=occurred_at,
                booking_date=local_date(occurred_at, LONDON),
                description=description,
                postings=postings,
                origin="connector",
                connection_id=self.connection_id,
                external_id=key,
                # Trading 212 names no counterparty, and no deposit is a bank transfer
                # (design §5.12): a card's is paired by the card payment.
                counterparty=counterparty or CounterpartyFields(),
            ),
        )
        self.count(outcome)
        return record.id

    async def contra(
        self, key: str, other_postings: int, identifier_id: UUID | None = None
    ) -> UUID:
        """Unassigned or In transit, as transfer_contra says."""
        await self.system_account("unassigned")
        await self.system_account("in_transit")
        return await transfer_contra(
            self.session,
            source=("connection", f"{self.connection_id}:{key}"),
            own_account_id=self.account,
            counterparty_identifier_id=identifier_id,
            other_postings=other_postings,
        )
