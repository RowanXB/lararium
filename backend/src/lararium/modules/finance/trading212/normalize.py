"""Trading 212 raw records into the ledger (design §5.5-5.11).

Deterministic from its inputs and versioned (NORMALIZER_VERSION, stored in the connection's
config, Decision 8): when the stored version differs, the run replays every raw record of the
history and the metadata the connection holds (the latest copy of each item), so what a new
version stores is filled in for what an older one saw. What Trading 212 reported at a sync
(the summary, positions, pending orders and pies) holds for that sync's time: only the run's
own copies are applied, dated by the run.

In order: the account (one per connection, named and wrapped by the connection's account
kind, registered under its Trading 212 account id); instrument and exchange metadata; order
fills, dividends and cash movements as connector transactions, each typed in its table; the
run's summary, positions, pending orders and pies, typed, the summary's cash and each position
a `reported` snapshot; prices; reconciliation (reconcile.py).

Posting rules (design §5.7), until the owner's first trade shows Trading 212's signs: the side
decides a fill's quantity sign; its cash is `walletImpact.netValue`, out for a buy and in for a
sell when it is not negative, as given when it is; each `taxes[]` entry is a fee of
-|quantity| in its currency, on the account against Unassigned; the cash a fill moves besides
its fees weighs the shares, at |cash| / quantity per share. A fill whose postings still do not
balance within rounding fails the run, naming the fill. A cash movement's sign comes from its
type, but a TRANSFER's is taken as given and flagged.

Flags (unknown fields with a value, unknown values of the enums that decide postings, types of
instrument the ledger does not hold, a TRANSFER) are reported, for the sync to publish;
errors name record types, fields and Trading 212's fill ids, never a value.
"""

import re
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import MAX_PREC, ROUND_HALF_EVEN, Decimal, localcontext
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from lararium.core import models as core_models
from lararium.core.commands import CommandContext, command_context
from lararium.core.connections import ConnectionRecord, get_connection, update_config
from lararium.core.connectors import SyncContext
from lararium.core.entities import get_or_create_organization
from lararium.core.errors import DisplayableError
from lararium.core.json_types import JSONValue
from lararium.core.raw_records import RawRecord, get_raw_records
from lararium.core.timeutil import local_date
from lararium.modules.finance import models
from lararium.modules.finance.accounts import (
    SourceDetails,
    TaxWrapper,
    create_account,
    get_account,
    set_source_details,
)
from lararium.modules.finance.errors import AccountNotFoundError
from lararium.modules.finance.identifiers import (
    ensure_identifier,
    get_or_create_identifier,
    normalize_identifier,
)
from lararium.modules.finance.instruments import InstrumentRecord, get_currency, get_instrument
from lararium.modules.finance.ledger import (
    CounterpartyFields,
    PostingInput,
    TransactionInput,
    compute_residuals,
    upsert_connector_transaction,
    void_transaction,
)
from lararium.modules.finance.money import to_plain_str, tolerance
from lararium.modules.finance.pricing import record_price
from lararium.modules.finance.system_accounts import (
    SystemAccountKey,
    get_or_create_system_account,
)
from lararium.modules.finance.trading212 import reconcile
from lararium.modules.finance.trading212.fields import COLUMNS, ENDPOINTS, MAP_ENTRIES
from lararium.modules.finance.trading212.instruments import instrument_kind, listing
from lararium.modules.finance.transfers import transfer_contra
from lararium.modules.finance.typed_tables import (
    Fields,
    Value,
    parse_decimal,
    parse_moment,
    typed_values,
    upsert_typed,
)

NORMALIZER_VERSION = "trading212-norm-1"
# The version that last normalized the connection, in its config (Decision 8).
NORMALIZER_VERSION_KEY = "normalizer_version"
ACCOUNT_MAP_KEY = "account_map"
INSTITUTION = "Trading 212"
SOURCE = "trading212"
# The owner's reporting zone: a record's booking date and a sync's day (Decision 11).
LONDON = ZoneInfo("Europe/London")

# The connection's account kind: the account's name and tax wrapper (design §5.5).
_ACCOUNTS: Mapping[str, tuple[str, TaxWrapper]] = {
    "invest": ("Trading 212 Invest", "gia"),
    "isa": ("Trading 212 ISA", "isa"),
}
_ENDPOINT_OF = {spec.record_type: name for name, spec in ENDPOINTS.items()}
# The items a key names once per connection, the ledger's (Decision 9) and the metadata's:
# replayed in full when the version changes. The others are what a sync reported.
_LEDGER = ("orders", "dividends", "transactions")
_REPLAYED = (*_LEDGER, "instruments", "exchanges")
# What each ledger key is, as an error names it.
_KEY_KIND = {"orders": "fill or order id", "dividends": "reference", "transactions": "reference"}

SIDES = frozenset({"BUY", "SELL"})
FILL_TYPES = frozenset(
    """TRADE STOCK_SPLIT STOCK_DISTRIBUTION FOP FOP_CORRECTION CUSTOM_STOCK_DISTRIBUTION
    EQUITY_RIGHTS SCRIP_STOCK_DIVIDENDS STOCK_DIVIDENDS STOCK_ACQUISITION
    CASH_AND_STOCK_ACQUISITION SPIN_OFF""".split()
)
# Shares moved in free of payment: a transfer's leg (design §5.7).
_TRANSFER_FILLS = frozenset({"FOP", "FOP_CORRECTION"})
# A cash movement's type: the sign of the account's leg (None: as given) and its description.
_TRANSACTIONS: Mapping[str, tuple[int | None, str]] = {
    "DEPOSIT": (1, "Deposit"),
    "WITHDRAW": (-1, "Withdrawal"),
    "FEE": (-1, "Fee"),
    "INTEREST_ON_FREE_CASH": (1, "Interest on cash"),
    "LENDING_INTEREST": (1, "Share lending interest"),
    "TRANSFER": (None, "Transfer"),
}
# Movements with another account of the owner's, or the owner's card (design §5.12).
_THROUGH_CONTRA = frozenset({"DEPOSIT", "WITHDRAW", "TRANSFER"})
# The owner checks the first real TRANSFER's sign (design §10).
TRANSFER_FLAG = "TRANSFER"
# An enum value fit to name in a flag.
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,39}")
# A fill's price per share is its cash over its quantity, to this place.
_PER_UNIT = Decimal(1).scaleb(-10)

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
    flagged: set[str] = field(default_factory=set[str])
    mismatches: list[str] = field(default_factory=list[str])
    # The run replayed every stored record, so `flagged` is all there is to flag.
    replayed: bool = False


@dataclass(frozen=True)
class _Record:
    id: UUID
    endpoint: str
    content: Item
    fetched_at: datetime

    @property
    def record_type(self) -> str:
        return ENDPOINTS[self.endpoint].record_type


# Reading items ---------------------------------------------------------------------------


def _get(item: Item | None, path: str) -> JSONValue:
    value: JSONValue = item
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _text(item: Item | None, path: str) -> str:
    """The field as text; a number is its text (raw records keep numbers so)."""
    value = _get(item, path)
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return value.strip() if isinstance(value, str) else ""


def _object(item: Item | None, path: str) -> Item | None:
    value = _get(item, path)
    return value if isinstance(value, dict) else None


def _objects(item: Item | None, path: str) -> list[Item]:
    value = _get(item, path)
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _number(record: _Record, item: Item | None, path: str) -> Decimal:
    """The field as a number (raw records keep numbers as their exact text); 0 when absent."""
    value = _get(item, path)
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if value is None or value == "":
        return Decimal(0)
    if not isinstance(value, str):
        raise Trading212DataError(f"{record.record_type} {path} is not a number")
    return parse_decimal(value, f"{record.record_type} {path}")


def _moment(record: _Record, item: Item | None, path: str) -> datetime:
    value = _text(item, path)
    if not value:
        raise Trading212DataError(f"{record.record_type} lacks {path}")
    moment = parse_moment(value, f"{record.record_type} {path}")
    if moment.tzinfo is None:
        raise Trading212DataError(f"{record.record_type} {path} has no time zone")
    return moment


def _flat(item: Item, prefix: str = "") -> dict[str, Value]:
    """The item's values by dotted path, arrays and maps left out: they are child rows."""
    found: dict[str, Value] = {}
    for key, value in item.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            found |= _flat(value, f"{path}.")
        elif isinstance(value, bool | str) or value is None:
            found[path] = value
        elif not isinstance(value, list):
            found[path] = str(value)
    return found


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _leaves(value: JSONValue, path: str) -> Iterator[str]:
    """The paths of the item's values (fields.py's notation); null and empty ones have none."""
    if isinstance(value, dict):
        entries = MAP_ENTRIES.get(f"{path}{{}}")
        if entries is not None:
            key_field, value_field = entries
            for entry in value.values():
                yield f"{path}{{}}.{key_field}"
                yield from _leaves(entry, f"{path}{{}}.{value_field}")
            return
        for key, entry in value.items():
            yield from _leaves(entry, _join(path, key))
    elif isinstance(value, list):
        for entry in value:
            yield from _leaves(entry, f"{path}[]")
    elif value is not None:
        yield path


def _lacks(container: JSONValue, parts: Sequence[str], path: str = "") -> bool:
    """Whether a required path (split at its dots) is missing below `container`: one under an
    array or a map only where that array or map is there."""
    head, rest = parts[0], parts[1:]
    name = head.removesuffix("[]").removesuffix("{}")
    here = _join(path, head)
    value = container.get(name) if isinstance(container, dict) else None
    if head.endswith("[]"):
        if value is None:
            return False
        if not isinstance(value, list):
            return True
        return any(e is None if not rest else _lacks(e, rest, here) for e in value)
    if head.endswith("{}"):
        if value is None:
            return False
        key_field, value_field = MAP_ENTRIES[here]
        if not isinstance(value, dict) or not rest or rest[0] not in (key_field, value_field):
            return True
        if rest[0] == key_field:
            return False
        below = rest[1:]
        return any(v is None if not below else _lacks(v, below, here) for v in value.values())
    if not rest:
        return value is None
    if value is None:
        # A parent missing: the path is missing, unless it lies in an array or map.
        return not any(p.endswith(("[]", "{}")) for p in rest)
    return _lacks(value, rest, here)


def _key(record: _Record) -> str:
    """The item's key: the ledger's external id (Decision 9) for the history, else what the
    item describes."""
    content = record.content
    match record.endpoint:
        case "orders":
            fill_id = _text(_object(content, "fill"), "id")
            return f"order:{fill_id}" if fill_id else f"order-none:{_text(content, 'order.id')}"
        case "dividends":
            return f"dividend:{_text(content, 'reference')}"
        case "transactions":
            return f"transaction:{_text(content, 'reference')}"
        case "instruments" | "pending_orders" | "pies":
            return _text(content, "ticker" if record.endpoint == "instruments" else "id")
        case "exchanges":
            return _text(content, "id")
        case "positions":
            return _text(content, "instrument.ticker")
        case "pie":
            return _text(content, "settings.id")
        case _:
            return ""


def _record(raw: RawRecord) -> _Record | None:
    endpoint = _ENDPOINT_OF.get(raw.record_type)
    if endpoint is None or not isinstance(raw.content, dict):
        return None
    return _Record(raw.id, endpoint, raw.content, raw.fetched_at)


async def _records(
    ctx: SyncContext, raw_record_ids: Sequence[UUID], replay: bool
) -> tuple[list[_Record], set[UUID]]:
    """This run's items, one per key, and the ids of those chosen; with `replay`, also the
    latest copy of every history and metadata item the connection holds.

    One ledger key with two contents in one run fails it, naming the record type and the
    key's kind (Decision 9): an item repeated unchanged is one raw record."""
    session = ctx.command.session
    raw = await get_raw_records(session, raw_record_ids)
    given = [record for record in map(_record, raw) if record is not None]
    chosen: dict[tuple[str, str], _Record] = {}
    for record in sorted(given, key=lambda r: (r.fetched_at, r.id)):
        key = (record.endpoint, _key(record))
        held = chosen.get(key)
        if held is not None and record.endpoint in _LEDGER and held.content != record.content:
            raise Trading212DataError(
                f"{record.record_type} has two different items with one"
                f" {_KEY_KIND[record.endpoint]}"
            )
        chosen[key] = record
    current = {record.id for record in chosen.values()}
    if replay:
        r = core_models.RawRecord
        ids = await session.scalars(
            select(r.id)
            .where(
                r.connection_id == ctx.connection.id,
                r.record_type.in_([ENDPOINTS[e].record_type for e in _REPLAYED]),
            )
            .order_by(r.fetched_at, r.id)
        )
        given_keys = set(chosen)
        for stored in await get_raw_records(session, list(ids)):
            record = _record(stored)
            if record is not None and (key := (record.endpoint, _key(record))) not in given_keys:
                chosen[key] = record  # oldest first, so the latest copy stays
    return list(chosen.values()), current


# One run ---------------------------------------------------------------------------------


class _Run:
    def __init__(
        self,
        ctx: SyncContext,
        connection: ConnectionRecord,
        now: datetime,
        report: NormalizeReport,
    ) -> None:
        self.ctx = ctx
        self.command = ctx.command
        self.session = ctx.command.session
        self.connection_id = connection.id
        self.config = connection.config
        self.now = now
        # The sync's day: what it reports is dated by it.
        self.day = local_date(now, LONDON)
        self.report = report
        self.account_id: UUID | None = None
        self.account_map: dict[str, JSONValue] = {}
        # Ticker → the metadata's (type, short name).
        self.metadata: dict[str, tuple[str, str]] = {}
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

    async def symbol(self, instrument_id: UUID) -> str:
        if instrument_id not in self._symbols:
            self._symbols[instrument_id] = (
                await get_instrument(self.session, instrument_id)
            ).symbol
        return self._symbols[instrument_id]

    async def load_metadata(self, tickers: set[str]) -> None:
        if not tickers:
            return
        rows = await self.session.execute(
            text(
                "select ticker, type, short_name from finance.trading212_instruments"
                " where connection_id = :c and ticker = any(cast(:t as text[]))"
            ),
            {"c": self.connection_id, "t": sorted(tickers)},
        )
        self.metadata = {row.ticker: (row.type, row.short_name or "") for row in rows}

    async def instrument(
        self, record: _Record, obj: Item | None, *, current: bool = False
    ) -> UUID | None:
        """The instrument of a row's instrument object; None, flagged, for a type the
        ledger does not hold."""
        ticker = _text(obj, "ticker")
        if not ticker:
            raise Trading212DataError(f"{record.record_type} has no instrument ticker")
        if (ticker, current) in self._instruments:
            return self._instruments[(ticker, current)]
        fields = {name: _text(obj, name) for name in ("ticker", "name", "isin", "currency")}
        if ticker in self.metadata:
            fields["type"], fields["shortName"] = self.metadata[ticker]
        found: UUID | None = None
        if instrument_kind(fields.get("type")) is None:
            self.flag_value("instruments", "type", fields["type"])
        else:
            found = await listing(self.command, fields, current=current)
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
                # A card deposit or withdrawal names no counterparty and is no bank transfer
                # (design §5.12).
                counterparty=CounterpartyFields(),
            ),
        )
        self.count(outcome)
        return record.id

    async def contra(self, key: str, other_postings: int) -> UUID:
        """Unassigned or In transit, as transfer_contra says for a leg with no counterparty."""
        await self.system_account("unassigned")
        await self.system_account("in_transit")
        return await transfer_contra(
            self.session,
            source=("connection", f"{self.connection_id}:{key}"),
            own_account_id=self.account,
            counterparty_identifier_id=None,
            other_postings=other_postings,
        )


# The account -----------------------------------------------------------------------------


async def _account(run: _Run, summary: _Record | None) -> None:
    """The connection's finance account, created on first sight (design §5.5)."""
    stored = run.config.get(ACCOUNT_MAP_KEY)
    run.account_map = dict(stored) if isinstance(stored, dict) else {}
    if summary is not None:
        trading212_id = normalize_identifier("trading212_account", _text(summary.content, "id"))
    elif len(run.account_map) == 1:
        [trading212_id] = run.account_map
    else:
        raise Trading212DataError("Trading 212's account summary is missing")
    kind = run.config.get("account_kind")
    if not isinstance(kind, str) or kind not in _ACCOUNTS:
        raise Trading212DataError("the connection's account kind is neither invest nor isa")
    name, wrapper = _ACCOUNTS[kind]

    account_id: UUID | None = None
    mapped = run.account_map.get(trading212_id)
    if isinstance(mapped, str):
        try:
            account_id = (await get_account(run.session, UUID(mapped))).id
        except AccountNotFoundError:
            account_id = None
    created_now = account_id is None
    if account_id is None:
        institution = await run.shared(
            ("organization", INSTITUTION),
            lambda cmd: get_or_create_organization(cmd, INSTITUTION),
        )
        created = await create_account(
            run.command,
            name=f"{name} ···{trading212_id[-4:]}",
            ledger_type="asset",
            subtype="broker",
            balance_mode="transactions",
            institution_entity_id=institution,
            tax_wrapper=wrapper,
            source_connection_id=run.connection_id,
        )
        account_id = created.id
        run.account_map[trading212_id] = str(account_id)
    run.account_id = account_id
    changed = await set_source_details(run.command, account_id, SourceDetails(tax_wrapper=wrapper))
    # The row exists before ensure_identifier runs, so it takes no lock in this transaction.
    await run.shared(
        ("identifier", "trading212_account", trading212_id),
        lambda cmd: get_or_create_identifier(cmd, "trading212_account", trading212_id),
    )
    changed |= await ensure_identifier(run.command, account_id, "trading212_account", trading212_id)
    if created_now:
        run.report.accounts_created += 1
    elif changed:
        run.report.accounts_updated += 1
    else:
        run.report.accounts_unchanged += 1


# Metadata --------------------------------------------------------------------------------


async def _instruments(run: _Run, records: Sequence[_Record], replay: bool) -> None:
    """Every tradable instrument, typed. One unchanged since the row was written from it is
    skipped (the list holds thousands), except when a new version replays."""
    stored = {} if replay else await run.stored_records("trading212_instruments", "ticker")
    for record in records:
        ticker = _text(record.content, "ticker")
        if stored.get(ticker) == record.id:
            continue
        values = await run.typed("instruments", "trading212_instruments", _flat(record.content))
        values.pop("ticker", None)
        await run.upsert(
            "trading212_instruments",
            {"connection_id": run.connection_id, "ticker": ticker},
            values | {"raw_record_id": record.id},
        )


async def _exchanges(run: _Run, records: Sequence[_Record], replay: bool) -> None:
    stored = {} if replay else await run.stored_records("trading212_exchanges", "id")
    endpoint = "exchanges"
    for record in records:
        content = record.content
        values = await run.typed(endpoint, "trading212_exchanges", _flat(content))
        exchange_id = values.pop("id")
        if stored.get(str(exchange_id)) == record.id:
            continue
        parent = {"connection_id": run.connection_id, "exchange_id": exchange_id}
        await run.upsert(
            "trading212_exchanges",
            {"connection_id": run.connection_id, "id": exchange_id},
            values | {"raw_record_id": record.id},
        )
        schedules = _objects(content, "workingSchedules")
        for n, schedule in enumerate(schedules):
            values = await run.typed(
                endpoint, "trading212_exchange_schedules", _flat(schedule), "workingSchedules[]."
            )
            await run.upsert(
                "trading212_exchange_schedules",
                parent | {"item_index": n},
                values | {"raw_record_id": record.id},
            )
            events = _objects(schedule, "timeEvents")
            for m, event in enumerate(events):
                values = await run.typed(
                    endpoint,
                    "trading212_exchange_time_events",
                    _flat(event),
                    "workingSchedules[].timeEvents[].",
                )
                await run.upsert(
                    "trading212_exchange_time_events",
                    parent | {"schedule_index": n, "item_index": m},
                    values | {"raw_record_id": record.id},
                )
            await run.prune(
                "trading212_exchange_time_events", parent | {"schedule_index": n}, len(events)
            )
        await run.prune("trading212_exchange_schedules", parent, len(schedules))
    if records:
        # Each instrument's exchange: the one whose schedule its workingScheduleId is.
        await run.session.execute(
            text(
                "update finance.trading212_instruments i set exchange_id = s.exchange_id"
                " from finance.trading212_exchange_schedules s"
                " where i.connection_id = :c and s.connection_id = :c"
                " and s.id = i.working_schedule_id"
                " and i.exchange_id is distinct from s.exchange_id"
            ),
            {"c": run.connection_id},
        )


# Order fills -----------------------------------------------------------------------------


def _fees(
    account_id: UUID, unassigned: UUID, fees: Sequence[tuple[UUID, Decimal, str]]
) -> list[PostingInput]:
    """Each fee on the account, then Unassigned's counterpart per currency."""
    postings: list[PostingInput] = []
    totals: dict[UUID, Decimal] = {}
    for instrument_id, amount, memo in fees:
        postings.append(PostingInput(account_id, instrument_id, amount, is_fee=True, memo=memo))
        totals[instrument_id] = totals.get(instrument_id, Decimal(0)) + amount
    postings += [
        PostingInput(unassigned, instrument_id, -total)
        for instrument_id, total in totals.items()
        if total != 0
    ]
    return postings


def _per_unit(total: Decimal, quantity: Decimal) -> Decimal:
    """total / quantity to _PER_UNIT's place, without trailing zeros."""
    with localcontext(prec=80):
        value = (total / quantity).quantize(_PER_UNIT, rounding=ROUND_HALF_EVEN)
    trimmed = value.normalize()
    exponent = trimmed.as_tuple().exponent
    assert isinstance(exponent, int)  # finite
    return trimmed if exponent <= 0 else trimmed.quantize(Decimal(1))


def _plain(quantity: Decimal) -> str:
    return to_plain_str(quantity.copy_abs().normalize())


async def _rounding(
    run: _Run,
    fill_id: str,
    postings: list[PostingInput],
    unassigned: UUID,
    currency: InstrumentRecord,
    quantity: Decimal,
) -> None:
    """A residual within the ledger's tolerance stays; one within what the price's rounding
    explains (|quantity| times half its last place) is posted to Unassigned (memo
    "rounding", design §5.7); more fails the run, naming the fill."""
    allowed = tolerance(currency.decimals) + quantity.copy_abs() * _PER_UNIT / 2
    for instrument_id, residual in compute_residuals(postings).items():
        decimals = (await get_instrument(run.session, instrument_id)).decimals
        if residual.copy_abs() <= tolerance(decimals):
            continue
        if instrument_id != currency.id or residual.copy_abs() > allowed:
            raise Trading212DataError(
                f"Trading 212 order fill {fill_id}: its cash differs from its quantity times"
                " price by more than rounding"
            )
        postings.append(PostingInput(unassigned, instrument_id, -residual, memo="rounding"))


async def _post_fill(run: _Run, record: _Record, key: str, order: Item, fill: Item) -> UUID | None:
    """The fill's transaction; None when it posts nothing (flagged when that is a value
    the mapping does not know)."""
    side, fill_type = _text(order, "side"), _text(fill, "type")
    if side not in SIDES:
        run.flag_value("orders", "order.side", side)
        return None
    if fill_type not in FILL_TYPES:
        run.flag_value("orders", "fill.type", fill_type)
        return None
    instrument_id = await run.instrument(record, _object(order, "instrument"))
    if instrument_id is None:
        return None
    fill_id = _text(fill, "id")
    buy = side == "BUY"
    quantity = _number(record, fill, "quantity").copy_abs()
    quantity = quantity if buy else -quantity

    unassigned = await run.system_account("unassigned")
    fees: list[tuple[UUID, Decimal, str]] = []
    for tax in _objects(fill, "walletImpact.taxes"):
        amount = _number(record, tax, "quantity").copy_abs()
        if amount != 0:
            fees.append(
                ((await run.currency(_text(tax, "currency"))).id, -amount, _text(tax, "name"))
            )
    net = _number(record, fill, "walletImpact.netValue")
    cash = (-net if buy else net) if net >= 0 else net
    currency: InstrumentRecord | None = None
    code = _text(fill, "walletImpact.currency")
    if code:
        currency = await run.currency(code)
    elif cash != 0:
        raise Trading212DataError(f"{record.record_type} lacks fill.walletImpact.currency")
    # The cash besides the fees in its currency: what the shares weigh.
    in_cash = [amount for i, amount, _ in fees if currency is not None and i == currency.id]
    gross = cash - sum(in_cash, Decimal(0))
    occurred_at = (
        _moment(record, fill, "filledAt")
        if _text(fill, "filledAt")
        else _moment(record, order, "createdAt")
    )

    postings: list[PostingInput] = []
    if fill_type == "TRADE":
        if quantity == 0:
            raise Trading212DataError(f"Trading 212 order fill {fill_id} has no quantity")
        price = _per_unit(gross.copy_abs(), quantity.copy_abs())
        if currency is None or price == 0:
            # Nothing to weigh the shares by: they come from (or go to) Unassigned.
            postings += [
                PostingInput(run.account, instrument_id, quantity),
                PostingInput(unassigned, instrument_id, -quantity),
            ]
        elif buy:
            postings.append(
                PostingInput(
                    run.account,
                    instrument_id,
                    quantity,
                    cost_per_unit=price,
                    cost_instrument_id=currency.id,
                )
            )
        else:
            postings.append(
                PostingInput(
                    run.account,
                    instrument_id,
                    quantity,
                    price_per_unit=price,
                    price_instrument_id=currency.id,
                )
            )
        if currency is not None and gross != 0:
            postings.append(PostingInput(run.account, currency.id, gross))
        description = f"{side.capitalize()} {_plain(quantity)} {await run.symbol(instrument_id)}"
    else:
        # A corporate action moves shares (and cash, if any) from or to Unassigned; shares
        # moved in free of payment are a transfer's leg (design §5.7).
        cash_postings = 2 if currency is not None and gross != 0 else 0
        others = cash_postings + len(fees) + len({i for i, _, _ in fees})
        if quantity != 0:
            contra = await run.contra(key, others) if fill_type in _TRANSFER_FILLS else unassigned
            postings += [
                PostingInput(run.account, instrument_id, quantity),
                PostingInput(contra, instrument_id, -quantity),
            ]
        if currency is not None and gross != 0:
            postings += [
                PostingInput(run.account, currency.id, gross),
                PostingInput(unassigned, currency.id, -gross),
            ]
        words = fill_type.replace("_", " ").capitalize()
        description = f"{words} {await run.symbol(instrument_id)}"
    postings += _fees(run.account, unassigned, fees)
    if not postings:
        return None
    if fill_type == "TRADE" and currency is not None:
        await _rounding(run, fill_id, postings, unassigned, currency, quantity)
        price = _number(record, fill, "price")
        if price > 0:
            quote = await run.currency(_text(order, "instrument.currency"))
            run.fill_prices[(instrument_id, quote.id, local_date(occurred_at, LONDON))] = price
    return await run.post(key, occurred_at, description, postings)


async def _order(run: _Run, record: _Record) -> None:
    content = record.content
    key = _key(record)
    order = _object(content, "order") or {}
    fill = _object(content, "fill")
    transaction_id = None
    if fill is not None:
        transaction_id = await _post_fill(run, record, key, order, fill)
    if transaction_id is None:
        await run.void(key)
    values = await run.typed("orders", "trading212_orders", _flat(content))
    parent = {"connection_id": run.connection_id, "external_id": key}
    await run.upsert(
        "trading212_orders",
        parent,
        values | {"transaction_id": transaction_id, "raw_record_id": record.id},
    )
    taxes = _objects(fill, "walletImpact.taxes")
    for n, tax in enumerate(taxes):
        values = await run.typed(
            "orders", "trading212_order_taxes", _flat(tax), "fill.walletImpact.taxes[]."
        )
        await run.upsert(
            "trading212_order_taxes",
            parent | {"item_index": n},
            values | {"raw_record_id": record.id},
        )
    await run.prune("trading212_order_taxes", parent, len(taxes))


def _order_time(record: _Record) -> tuple[str, str]:
    content = record.content
    return _text(content, "fill.filledAt") or _text(content, "order.createdAt"), _key(record)


# Dividends and cash movements ------------------------------------------------------------


async def _dividend(run: _Run, record: _Record) -> None:
    content = record.content
    key = _key(record)
    transaction_id = None
    instrument_id = await run.instrument(record, _object(content, "instrument"))
    amount = _number(record, content, "amount")
    if instrument_id is not None and amount != 0:
        currency = await run.currency(_text(content, "currency"))
        unassigned = await run.system_account("unassigned")
        transaction_id = await run.post(
            key,
            _moment(record, content, "paidOn"),
            f"Dividend {await run.symbol(instrument_id)}",
            [
                PostingInput(run.account, currency.id, amount),
                PostingInput(unassigned, currency.id, -amount),
            ],
        )
    if transaction_id is None:
        await run.void(key)
    values = await run.typed("dividends", "trading212_dividends", _flat(content))
    reference = values.pop("reference")
    await run.upsert(
        "trading212_dividends",
        {"connection_id": run.connection_id, "reference": reference},
        values | {"transaction_id": transaction_id, "raw_record_id": record.id},
    )


async def _cash_movement(run: _Run, record: _Record) -> None:
    content = record.content
    key = _key(record)
    kind = _text(content, "type")
    transaction_id = None
    if kind not in _TRANSACTIONS:
        run.flag_value("transactions", "type", kind)
    else:
        sign, description = _TRANSACTIONS[kind]
        amount = _number(record, content, "amount")
        if sign is None:
            run.flag(TRANSFER_FLAG)
        else:
            amount = amount.copy_abs() * sign
        if amount != 0:
            currency = await run.currency(_text(content, "currency"))
            contra = (
                await run.contra(key, 0)
                if kind in _THROUGH_CONTRA
                else await run.system_account("unassigned")
            )
            # A card deposit's contra is Unassigned until a card payment pairs with it.
            transaction_id = await run.post(
                key,
                _moment(record, content, "dateTime"),
                description,
                [
                    PostingInput(run.account, currency.id, amount),
                    PostingInput(contra, currency.id, -amount),
                ],
            )
    if transaction_id is None:
        await run.void(key)
    values = await run.typed("transactions", "trading212_transactions", _flat(content))
    reference = values.pop("reference")
    await run.upsert(
        "trading212_transactions",
        {"connection_id": run.connection_id, "reference": reference},
        values | {"transaction_id": transaction_id, "raw_record_id": record.id},
    )


# What the sync reported ------------------------------------------------------------------


async def _summary(run: _Run, record: _Record) -> tuple[UUID, Decimal]:
    """The account's cash as a reported snapshot, and the summary typed; (currency, cash)."""
    content = record.content
    cash = sum(
        (
            _number(record, content, f"cash.{part}")
            for part in ("availableToTrade", "reservedForOrders", "inPies")
        ),
        Decimal(0),
    )
    currency = await run.currency(_text(content, "currency"))
    snapshot_id = await reconcile.reported_snapshot(
        run.command, run.account, currency.id, cash, run.now, run.day
    )
    values = await run.typed("summary", "trading212_accounts", _flat(content))
    await run.upsert(
        "trading212_accounts",
        {"connection_id": run.connection_id, "as_of_date": run.day},
        values
        | {
            "account_id": run.account,
            "account_kind": run.config.get("account_kind"),
            "snapshot_id": snapshot_id,
            "raw_record_id": record.id,
        },
    )
    return currency.id, cash


async def _positions(run: _Run, records: Sequence[_Record]) -> dict[UUID, Decimal]:
    """Each position as a reported snapshot and a price for the day, typed; the quantity
    reported of each instrument."""
    reported: defaultdict[UUID, Decimal] = defaultdict(Decimal)
    for record in records:
        content = record.content
        snapshot_id = None
        instrument_id = await run.instrument(record, _object(content, "instrument"), current=True)
        if instrument_id is not None:
            quantity = _number(record, content, "quantity")
            reported[instrument_id] += quantity
            snapshot_id = await reconcile.reported_snapshot(
                run.command, run.account, instrument_id, quantity, run.now, run.day
            )
            price = _number(record, content, "currentPrice")
            if price > 0:
                quote = await run.currency(_text(content, "instrument.currency"))
                run.position_prices[(instrument_id, quote.id, run.day)] = price
        values = await run.typed("positions", "trading212_positions", _flat(content))
        ticker = values.pop("instrument_ticker")
        await run.upsert(
            "trading212_positions",
            {
                "connection_id": run.connection_id,
                "as_of_date": run.day,
                "instrument_ticker": ticker,
            },
            values | {"snapshot_id": snapshot_id, "raw_record_id": record.id},
        )
    return dict(reported)


async def _pending_orders(run: _Run, records: Sequence[_Record]) -> None:
    for record in records:
        values = await run.typed(
            "pending_orders", "trading212_pending_orders", _flat(record.content)
        )
        order_id = values.pop("id")
        await run.upsert(
            "trading212_pending_orders",
            {"connection_id": run.connection_id, "as_of_date": run.day, "id": order_id},
            values | {"raw_record_id": record.id},
        )


async def _pies(run: _Run, pies: Sequence[_Record], details: Sequence[_Record]) -> None:
    """Each pie of the list, with its detail's settings, instruments and shares: typed only."""
    # The pie's id → (its key value, the list's record: the row's raw_record_id).
    listed: dict[str, tuple[object, UUID]] = {}
    for record in pies:
        values = await run.typed("pies", "trading212_pies", _flat(record.content))
        pie_id = values.pop("id")
        listed[_text(record.content, "id")] = (pie_id, record.id)
        await run.upsert(
            "trading212_pies",
            {"connection_id": run.connection_id, "id": pie_id, "as_of_date": run.day},
            values | {"raw_record_id": record.id},
        )
    endpoint = "pie"
    for record in details:
        content = record.content
        if (pie := listed.get(_text(content, "settings.id"))) is None:
            continue  # a pie the list no longer holds
        pie_id, list_record_id = pie
        values = await run.typed(endpoint, "trading212_pies", _flat(content))
        # The list's raw_record_id again: an insert's row is checked before its conflict.
        await run.upsert(
            "trading212_pies",
            {"connection_id": run.connection_id, "id": pie_id, "as_of_date": run.day},
            values | {"raw_record_id": list_record_id, "detail_raw_record_id": record.id},
        )
        parent = {"connection_id": run.connection_id, "pie_id": pie_id, "as_of_date": run.day}
        instruments = _objects(content, "instruments")
        for n, instrument in enumerate(instruments):
            values = await run.typed(
                endpoint, "trading212_pie_instruments", _flat(instrument), "instruments[]."
            )
            await run.upsert(
                "trading212_pie_instruments",
                parent | {"item_index": n},
                values | {"raw_record_id": record.id},
            )
            issues = _objects(instrument, "issues")
            for m, issue in enumerate(issues):
                values = await run.typed(
                    endpoint,
                    "trading212_pie_instrument_issues",
                    _flat(issue),
                    "instruments[].issues[].",
                )
                await run.upsert(
                    "trading212_pie_instrument_issues",
                    parent | {"instrument_index": n, "item_index": m},
                    values | {"raw_record_id": record.id},
                )
            await run.prune(
                "trading212_pie_instrument_issues", parent | {"instrument_index": n}, len(issues)
            )
        await run.prune("trading212_pie_instruments", parent, len(instruments))
        shares = _object(content, "settings.instrumentShares") or {}
        for ticker, share in shares.items():
            values = await run.typed(
                endpoint,
                "trading212_pie_instrument_shares",
                {"share": share if isinstance(share, str) else None},
                "settings.instrumentShares{}.",
            )
            await run.upsert(
                "trading212_pie_instrument_shares",
                parent | {"ticker": ticker},
                values | {"raw_record_id": record.id},
            )
        await run.session.execute(
            text(
                "delete from finance.trading212_pie_instrument_shares where connection_id = :c"
                " and pie_id = :p and as_of_date = :d and ticker <> all(cast(:t as text[]))"
            ),
            {"c": run.connection_id, "p": pie_id, "d": run.day, "t": sorted(shares)},
        )


async def _prices(run: _Run) -> None:
    """Each position's price for the sync's day; a fill's price on its day when no price of
    Trading 212's is there for it yet (a later write for a day replaces an earlier one)."""
    p = models.Price
    wanted = [(k, v, True) for k, v in run.position_prices.items()]
    wanted += [(k, v, False) for k, v in run.fill_prices.items() if k not in run.position_prices]
    for (instrument_id, quote_id, day), price, replaces in wanted:
        stored = await run.session.scalar(
            select(p.price).where(
                p.instrument_id == instrument_id,
                p.quote_instrument_id == quote_id,
                p.as_of_date == day,
                p.source == SOURCE,
            )
        )
        if stored == price or (stored is not None and not replaces):
            continue
        await record_price(
            run.command,
            instrument_id=instrument_id,
            quote_instrument_id=quote_id,
            price=price,
            as_of_date=day,
            source=SOURCE,
        )


# The run -------------------------------------------------------------------------------


def _check(run: _Run, record: _Record) -> None:
    """Required fields fail the run when missing; unknown ones with a value are flagged."""
    spec = ENDPOINTS[record.endpoint]
    for path in sorted(spec.required):
        if _lacks(record.content, path.split(".")):
            raise Trading212DataError(f"{spec.record_type} lacks {path}")
    for path in sorted(set(_leaves(record.content, "")) - spec.known):
        run.flag(f"{record.endpoint}.{path}")


def _tickers(records: Mapping[str, Sequence[_Record]]) -> set[str]:
    found = {_text(r.content, "order.instrument.ticker") for r in records["orders"]}
    for endpoint in ("dividends", "positions"):
        found |= {_text(r.content, "instrument.ticker") for r in records[endpoint]}
    return found - {""}


async def normalize(
    ctx: SyncContext, raw_record_ids: Sequence[UUID], *, now: datetime | None = None
) -> NormalizeReport:
    """Apply raw records to the ledger; `now` is the sync's time (default: now).

    Its arithmetic on amounts is exact, as the ledger's balance check is: the default context
    rounds to 28 digits, which could leave postings that do not balance."""
    with localcontext(prec=MAX_PREC):
        return await _normalize(ctx, list(dict.fromkeys(raw_record_ids)), now or datetime.now(UTC))


async def _normalize(
    ctx: SyncContext, raw_record_ids: list[UUID], now: datetime
) -> NormalizeReport:
    connection = await get_connection(ctx.command.session, ctx.connection.id)
    replay = connection.config.get(NORMALIZER_VERSION_KEY) != NORMALIZER_VERSION
    chosen, current = await _records(ctx, raw_record_ids, replay)
    report = NormalizeReport(replayed=replay)
    run = _Run(ctx, connection, now, report)
    records: defaultdict[str, list[_Record]] = defaultdict(list)
    for record in sorted(chosen, key=lambda r: (_key(r), r.fetched_at, r.id)):
        _check(run, record)
        if record.endpoint in _REPLAYED or record.id in current:
            records[record.endpoint].append(record)
    summaries = sorted(records["summary"], key=lambda r: (r.fetched_at, r.id))
    summary = summaries[-1] if summaries else None
    await _account(run, summary)

    await _instruments(run, records["instruments"], replay)
    await _exchanges(run, records["exchanges"], replay)
    await run.load_metadata(_tickers(records))
    for record in sorted(records["orders"], key=_order_time):
        await _order(run, record)
    for record in sorted(records["dividends"], key=lambda r: (_text(r.content, "paidOn"), _key(r))):
        await _dividend(run, record)
    for record in sorted(
        records["transactions"], key=lambda r: (_text(r.content, "dateTime"), _key(r))
    ):
        await _cash_movement(run, record)

    reported = await _positions(run, records["positions"])
    await _pending_orders(run, records["pending_orders"])
    await _pies(run, records["pies"], records["pie"])
    if summary is not None:
        currency_id, cash = await _summary(run, summary)
        reported[currency_id] = reported.get(currency_id, Decimal(0)) + cash
    await _prices(run)

    # Reconciliation (design §5.11), at the sync's time: only a run with the summary has
    # what Trading 212 reports now; another keeps the warnings the last check left.
    if summary is not None:
        account = await get_account(run.session, run.account)
        mismatches = await reconcile.check(
            run.session, run.account, account.name, run.day, reported
        )
        report.mismatches = [m.warning for m in mismatches]
        await reconcile.publish(ctx, mismatches)

    # Only this normaliser's keys: others (the account kind) stay as they are.
    values: dict[str, JSONValue] = {
        ACCOUNT_MAP_KEY: run.account_map,
        NORMALIZER_VERSION_KEY: NORMALIZER_VERSION,
    }
    if any(connection.config.get(key) != value for key, value in values.items()):
        await update_config(ctx.command, connection.id, values)
    return report
