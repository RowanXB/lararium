"""What one normalize run of an eToro connection shares: its records, accounts, instruments and
system accounts, and what it reports; and reading an item's fields.

Raw records keep every number as its exact decimal text (readonly_http.record_json). Errors
name record types, fields and eToro's position ids, never a value: a name, date of birth,
counterparty or bank detail (fields.PERSONAL) never reaches a message or a log.
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
from lararium.modules.finance.etoro.fields import COLUMNS, ENDPOINTS
from lararium.modules.finance.etoro.instruments import listing, listing_currency
from lararium.modules.finance.instruments import (
    InstrumentRecord,
    find_by_identifier,
    get_currency,
    get_instrument,
)
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
from lararium.modules.finance.transfers import transfer_contra
from lararium.modules.finance.typed_tables import (
    Fields,
    Value,
    parse_decimal,
    parse_moment,
    typed_values,
    upsert_typed,
)

SOURCE = "etoro"
# The owner's reporting zone: a record's booking date and a report's day (Decision 11).
LONDON = ZoneInfo("Europe/London")
# An enum value fit to name in a flag (eToro's are camelCase words, or numbers).
_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_]{0,39}")
_DIGITS = re.compile(r"[0-9]{1,19}")

type Item = dict[str, JSONValue]


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
    # Flags every run finds in full (instruments whose type eToro has not given, or gave as
    # one the ledger does not hold).
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


def number_or_none(record: Record, item: Item | None, path: str) -> Decimal | None:
    """The field as a number; None when absent."""
    value = value_at(item, path)
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise EtoroDataError(f"{record.record_type} {path} is not a number")
    return parse_decimal(value, f"{record.record_type} {path}")


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
    value = text_at(item, path)
    if not value:
        raise EtoroDataError(f"{record.record_type} lacks {path}")
    moment = parse_moment(value, f"{record.record_type} {path}")
    if moment.tzinfo is None:
        raise EtoroDataError(f"{record.record_type} {path} has no time zone")
    return moment


def flat(item: Item, prefix: str = "") -> dict[str, Value]:
    """The item's values by dotted path, arrays left out: they are child rows."""
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


class Run:
    def __init__(
        self, ctx: SyncContext, connection: ConnectionRecord, report: NormalizeReport
    ) -> None:
        self.ctx = ctx
        self.command = ctx.command
        self.session = ctx.command.session
        self.connection_id = connection.id
        self.config = connection.config
        self.sync_id = ctx.sync_id
        self.report = report
        # eToro account id → finance account id, as the connection's config keeps it.
        self.account_map: dict[str, JSONValue] = {}
        # The accounts the balances list, by eToro account id, and their types.
        self.accounts: dict[str, UUID] = {}
        self.account_types: dict[str, str] = {}
        self.trading_id: str | None = None
        # The Trading account's currency (accountCurrency, design §3.1).
        self.account_currency = ""
        # eToro instrument id → its asset currency, and a symbol a position or order gave.
        self.asset_currencies: dict[str, str] = {}
        self.symbols: dict[str, str] = {}
        # eToro instrument id → the metadata's (type id, symbol, display name), None unlisted.
        self.metadata: dict[str, tuple[str, str, str] | None] = {}
        # (instrument, currency, day) → price: current rates and daily candles.
        self.current_prices: dict[tuple[UUID, UUID, date], Decimal] = {}
        self.candle_prices: dict[tuple[UUID, UUID, date], Decimal] = {}
        self._instruments: dict[str, UUID | None] = {}
        self._symbols: dict[UUID, str] = {}
        self._currencies: dict[str, InstrumentRecord] = {}
        self._shared: dict[tuple[str, ...], UUID] = {}

    @property
    def trading(self) -> UUID:
        if self.trading_id is None:
            raise EtoroDataError("eToro's balances list no Trading account")
        return self.accounts[self.trading_id]

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
        """A price eToro gives in an asset currency, as finance.prices keeps it: (the
        listing's currency, the price in it); pence (GBX) are pounds / 100."""
        listed, exponent = listing_currency(code)
        return (await self.currency(listed)).id, price.scaleb(exponent)

    async def symbol(self, instrument_id: UUID) -> str:
        if instrument_id not in self._symbols:
            self._symbols[instrument_id] = (
                await get_instrument(self.session, instrument_id)
            ).symbol
        return self._symbols[instrument_id]

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
        """The instrument eToro's id names, found or created; None, flagged, for a type the
        ledger does not hold or one new to the ledger whose currency eToro has not given."""
        if etoro_id in self._instruments:
            return self._instruments[etoro_id]
        await self.load_metadata({etoro_id})
        type_id, symbol, name = self.metadata[etoro_id] or ("", "", "")
        symbol = symbol or self.symbols.get(etoro_id, "") or f"eToro {etoro_id}"
        currency = self.asset_currencies.get(etoro_id, "")
        found: UUID | None
        if not currency and await find_by_identifier(self.session, "etoro_id", etoro_id) is None:
            self.flag(f"instrument {etoro_id}: no currency")
            found = None
        else:
            found = await listing(self.command, int(etoro_id), symbol, name, currency, None)
            if found is None:
                self.flag_value("instruments", "instrumentTypeID", type_id)
        self._instruments[etoro_id] = found
        return found

    # Typed tables -----------------------------------------------------------------------

    async def typed(
        self, endpoint: str, table: str, values: Mapping[str, Value], under: str = ""
    ) -> dict[str, object]:
        """The values that are known fields of the table's rows, by column, typed. `under`:
        the path of the array the values are an item of."""
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

    # The ledger -------------------------------------------------------------------------

    async def live(self, key: str) -> UUID | None:
        t = models.Transaction
        return await self.session.scalar(
            select(t.id).where(
                t.connection_id == self.connection_id, t.external_id == key, t.status != "void"
            )
        )

    async def void(self, key: str) -> None:
        """Void the connection's live transaction under the key, if there is one: a record
        eToro changed to one that posts nothing leaves no entry."""
        posted = await self.live(key)
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
                counterparty=counterparty or CounterpartyFields(),
            ),
        )
        self.count(outcome)
        return record.id

    async def contra(
        self, key: str, account_id: UUID, other_postings: int, identifier_id: UUID | None
    ) -> UUID:
        """Unassigned or In transit, as transfer_contra says."""
        await self.system_account("unassigned")
        await self.system_account("in_transit")
        return await transfer_contra(
            self.session,
            source=("connection", f"{self.connection_id}:{key}"),
            own_account_id=account_id,
            counterparty_identifier_id=identifier_id,
            other_postings=other_postings,
        )
