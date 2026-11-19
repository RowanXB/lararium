"""Monzo normalizer: the raw records of one sync → accounts, snapshots and ledger transactions.

Deterministic from its inputs and versioned (NORMALIZER_VERSION), so the ledger can later be
regenerated from raw records. Mapping rules (plan Decisions 5, 6 and 9):

- each Monzo account is a transactions-mode bank account; each pot a hybrid-mode child of it
  (ruling R14), valued by its moves before its first snapshot and by the pot balance fetched
  on each sync afterwards; a deleted pot is closed;
- each non-declined transaction is one ledger transaction keyed by its Monzo id, posted
  against the pot's account for a pot move, and otherwise against "In transit" when it is a
  transfer between the owner's accounts and "Unassigned" when not (transfer_contra; the
  heal after the sync does the matching); when it changes upstream it is replaced in place,
  and a pending one that comes back declined or zero is voided;
- what a transaction says about its counterparty is stored in typed fields, its bank
  account in the identifier registry; each Monzo account's own bank details are registered
  to it (M4 Decisions 10 and 11);
- after each full backfill of an account, and when an account is first seen outside one,
  the difference from Monzo's balance is posted as its opening balance adjustment, replaced
  in place, so older history fetched later corrects it instead of adding to it (ruling R18);
  any other difference is a warning, never corrected;
- what Monzo says about a transaction, its merchant, an account or a pot is stored typed, a
  fee as a posting of its own, and a declined transaction outside the ledger (the
  structured-fields plan, Decisions 5-11); every field is read through _Reader, and one in a
  shape not mapped yet stays in the raw records, with a warning on the connection, and never
  fails the sync, unless it is a core field the sync cannot do without (the reader-
  everywhere plan, Decision 4);
- the first sync under a new NORMALIZER_VERSION first replays the latest raw record of every
  transaction the connection fetched (structured-fields Decision 12), so what a new version
  stores is filled in for the history an older one saw too; the replay never posts a
  transaction the ledger does not already hold.
"""

import logging
import re
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, date, datetime, timedelta
from decimal import MAX_PREC, Decimal, localcontext
from functools import partial
from numbers import Real
from typing import Any, Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, and_, case, column, func, select, true
from sqlalchemy.dialects.postgresql import JSONB, distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext, command_context
from lararium.core.connections import get_connection, set_warnings, update_config
from lararium.core.connectors import SyncContext
from lararium.core.entities import PlaceDetails, get_or_create_organization, get_or_create_place
from lararium.core.json_types import JSONValue
from lararium.core.models import RawRecord
from lararium.core.raw_records import get_raw_records
from lararium.core.settings_store import get_timezone
from lararium.core.timeutil import local_date
from lararium.modules.finance import models
from lararium.modules.finance.accounts import (
    SourceDetails,
    TaxWrapper,
    close_account,
    create_account,
    get_account,
    set_source_details,
)
from lararium.modules.finance.declined import DeclinedInput, upsert_declined
from lararium.modules.finance.errors import FinanceValidationError, UnknownCurrencyError
from lararium.modules.finance.identifiers import (
    ensure_identifier,
    get_or_create_identifier,
    normalize_identifier,
)
from lararium.modules.finance.instruments import InstrumentRecord, get_currency
from lararium.modules.finance.ledger import (
    CategorySplit,
    CounterpartyFields,
    PostingInput,
    TransactionDetails,
    TransactionInput,
    TransactionStatus,
    set_counterparty_fields,
    upsert_connector_transaction,
    void_transaction,
)
from lararium.modules.finance.money import require_amount, to_plain_str, tolerance
from lararium.modules.finance.snapshots import record_snapshot
from lararium.modules.finance.system_accounts import (
    SystemAccountKey,
    get_or_create_system_account,
)
from lararium.modules.finance.transfers import transfer_contra

logger = logging.getLogger(__name__)

NORMALIZER_VERSION = "monzo-norm-3"
# The version that last normalized the connection, in its config. Bumping NORMALIZER_VERSION
# makes the next sync replay the history already posted (see _Normalizer.replay).
NORMALIZER_VERSION_KEY = "normalizer_version"

ACCOUNT_MAP_KEY = "account_map"
# Per Monzo account: where the history its opening accounts for begins (ISO time). Recorded
# when the opening is first computed, even when none is needed (ruling R18).
OPENING_BOUNDARIES_KEY = "opening_boundaries"
# The last mismatch the owner was told about, per Monzo account, so each value is sent once.
NOTIFIED_KEY = "reconciliation_notified"
OPENING_DESCRIPTION = "Opening balance adjustment"
# Set once the transactions posted before counterparty fields existed have been given them.
COUNTERPARTY_FIELDS_KEY = "counterparty_fields_v1"
# The payment schemes that move money between bank accounts.
_BANK_TRANSFER_SCHEMES = frozenset({"payport_faster_payments", "bacs", "monzo_to_monzo"})

# By Monzo's account type; uk_rewards is the account Monzo pays rewards into.
_ACCOUNT_NAMES = {
    "uk_retail": "Monzo",
    "uk_retail_joint": "Monzo Joint",
    "uk_rewards": "Monzo Rewards",
}

# A pot's isa_wrapper, lowercased, as a tax wrapper; "" means none.
_TAX_WRAPPERS: dict[str, TaxWrapper] = {"isa": "isa", "lisa": "lisa", "sipp": "sipp", "gia": "gia"}


class MonzoFieldError(ValueError):
    """A field the sync cannot do without is missing or unreadable: a transaction's id or
    amount, say (the reader-everywhere plan, Decision 4). The sync fails rather than post
    what Monzo did not say. The message names the field's path, never its value, so it can
    be shown as the sync's error."""

    def __init__(self, path: str) -> None:
        super().__init__(f"Monzo field {path} is missing or unreadable")
        self.path = path


@dataclass(frozen=True)
class FetchedRecord:
    """One response the sync stored: its raw record, the request that fetched it, and when.

    The request and time travel with the id because raw records are deduplicated by content:
    two accounts whose balances read the same share one record, stored with the first request,
    and a response seen again keeps the record's first fetch time.
    """

    record_id: UUID
    request: dict[str, JSONValue]
    fetched_at: datetime


@dataclass
class NormalizeReport:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    voided: int = 0
    skipped_declined: int = 0
    mismatches: list[str] = field(default_factory=list[str])


def to_quantity(minor_units: int, decimals: int) -> Decimal:
    """An amount in integer minor units as an exact decimal (-1234, 2 → -12.34), however many
    digits it has: the default context would round it to 28, and then a fee split could fail
    to balance, or an amount too long to store look storable."""
    with localcontext(prec=MAX_PREC):
        return Decimal(minor_units).scaleb(-decimals)


async def _notify(ctx: SyncContext, text: str) -> None:
    """Tell the owner; a failure to send is logged and never fails the sync."""
    try:
        await ctx.notifier.send(text)
    except Exception:
        logger.warning("sending a reconciliation notification failed", exc_info=True)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _mismatch_prefix(monzo_account_id: str) -> str:
    return f"Monzo account {monzo_account_id}: reconciliation mismatch"


def _unmapped_warning(name: str) -> str:
    return f"Monzo field {name} is not stored yet; it stays in the raw records"


def _fields(value: object) -> dict[str, Any]:
    """A JSON object's fields; none for anything else, such as a missing or null object."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


@dataclass(frozen=True)
class _Statement:
    """What one transaction says about its merchant, and when it said it."""

    at: datetime | None
    name: str | None
    details: PlaceDetails


# Sorts a statement with no time before any other.
_NO_TIME = datetime.min.replace(tzinfo=UTC)


def _fold(statements: Sequence[_Statement]) -> tuple[str | None, PlaceDetails]:
    """What a merchant's statements say together (structured-fields Decision 6): each value a
    newer statement states replaces an older one, and one no newer statement states is kept.
    By statement time alone (no time counts as the oldest; equal times keep the order given),
    so the order a sync meets them in does not decide."""
    name: str | None = None
    values: dict[str, object] = {}
    for statement in sorted(statements, key=lambda s: s.at or _NO_TIME):
        if statement.name is not None:
            name = statement.name
        for detail in fields(PlaceDetails):
            if (value := getattr(statement.details, detail.name)) is not None:
                values[detail.name] = value
    return name, replace(PlaceDetails(), **values)


def _storable(quantity: Decimal) -> bool:
    """Whether the ledger can store the amount (require_amount's rule, in one place)."""
    try:
        require_amount("amount", quantity)
    except FinanceValidationError:
        return False
    return True


def _minor_units(value: object) -> int | None:
    """A JSON integer amount; a JSON true or false is not one (bool is an int in Python)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _boolean(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _decimal(value: object) -> Decimal | None:
    """A JSON number as an exact decimal, made from its shortest form (the 51.5 the source
    wrote), never from the binary fraction the JSON parser turned it into."""
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    number = Decimal(str(value))
    return number if number.is_finite() else None


def _amounts(value: object) -> dict[str, int] | None:
    """A JSON object of name -> amount in minor units, such as Monzo's fees."""
    if not isinstance(value, dict):
        return None
    amounts = {
        key: amount
        for key, item in cast(dict[str, object], value).items()
        if (amount := _minor_units(item)) is not None
    }
    return amounts if len(amounts) == len(cast(dict[str, object], value)) else None


def _strings(value: object) -> tuple[str, ...] | None:
    """A JSON list of strings, such as Monzo's labels."""
    if isinstance(value, list) and all(isinstance(v, str) for v in cast(list[object], value)):
        return tuple(cast(list[str], value))
    return None


def _is_stated(value: object) -> bool:
    """Whether a JSON value says anything: absent, null and "" do not."""
    return value is not None and value != ""


# A time as Monzo writes it: ISO 8601 with its offset, in a year it can mean. The one rule
# for a readable time, in SQL (the replay's latest copy) and here alike: only the forms both
# PostgreSQL and Python read, as the same instant (hours to 23, minutes and seconds to 59, at
# most microseconds, offsets within 15:59), whose date exists. Anything else, such as
# "infinity", a date alone, a time without an offset, 24:00 or a leap second, reads as none.
_ISO_TIME = (
    r"(19|2[0-9])[0-9]{2}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])"
    r"T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\.[0-9]{1,6})?"
    r"(Z|[+-](0[0-9]|1[0-5]):?[0-5][0-9])"
)


def _readable_time(value: object) -> datetime | None:
    """A time in the _ISO_TIME form that is a real time, else None (never an error)."""
    if not isinstance(value, str) or not re.fullmatch(_ISO_TIME, value):
        return None
    try:
        return _parse(value)
    except ValueError:  # a date that does not exist, such as the 30th of February
        return None


class _Reader:
    """Reads the fields of one JSON object, its path in the source given (structured-fields
    Decision 10): the only code that looks inside Monzo's objects (the reader-everywhere
    plan). Absent, null and "" read as None. A value of another type reads as None too, and
    its path is noted in `unmapped`, so the sync goes on and the owner is warned;
    `unreadable` keeps this object's field names that could not be read. A core field, one
    the sync cannot do without, is read with a `required_` reader, which raises
    MonzoFieldError instead."""

    def __init__(self, value: object, path: str, unmapped: set[str]) -> None:
        self.path = path
        self.unmapped = unmapped
        self.unreadable: set[str] = set()
        self.fields: dict[str, Any] = _fields(value)
        if not isinstance(value, dict) and _is_stated(value):
            unmapped.add(path)

    def _stated(self, key: str) -> object:
        value = self.fields.get(key)
        return value if _is_stated(value) else None

    def states(self, key: str) -> bool:
        """Whether the field says anything, in whatever shape."""
        return self._stated(key) is not None

    def unreadable_field(self, key: str) -> None:
        self.unreadable.add(key)
        self.unmapped.add(self._path(key))

    def _path(self, key: str) -> str:
        return f"{self.path}.{key}" if self.path else key

    def _read[T](self, key: str, parse: Callable[[object], T | None]) -> T | None:
        value = self._stated(key)
        if value is None:
            return None
        parsed = parse(value)
        if parsed is None:
            self.unreadable_field(key)
        return parsed

    def _required[T](self, key: str, parse: Callable[[object], T | None]) -> T:
        value = self._stated(key)
        parsed = None if value is None else parse(value)
        if parsed is None:
            raise MonzoFieldError(self._path(key))
        return parsed

    def text(self, key: str, *, keep_empty: bool = False) -> str | None:
        """A string; with `keep_empty`, "" reads as itself, for a field where it means
        something (a pot's isa_wrapper, where it means none)."""
        if keep_empty and self.fields.get(key) == "":
            return ""
        return self._read(key, _string)

    def flag(self, key: str) -> bool | None:
        return self._read(key, _boolean)

    def minor_units(self, key: str) -> int | None:
        return self._read(key, _minor_units)

    def number(self, key: str) -> Decimal | None:
        return self._read(key, _decimal)

    def time(self, key: str) -> datetime | None:
        """A time by the one rule for a readable time (_readable_time); never an error."""
        return self._read(key, _readable_time)

    def amounts(self, key: str) -> dict[str, int]:
        """Amounts in minor units by name; none when not stated, or not in that shape."""
        return self._read(key, _amounts) or {}

    def texts(self, key: str) -> tuple[str, ...]:
        """A list of strings; empty when not stated, or not in that shape."""
        return self._read(key, _strings) or ()

    def warn_if_stated(self, key: str) -> None:
        """For a field no sample shows the shape of yet: any value it states is noted."""
        if self.states(key):
            self.unreadable_field(key)

    def required_text(self, key: str) -> str:
        return self._required(key, _string)

    def required_minor_units(self, key: str) -> int:
        return self._required(key, _minor_units)

    def required_time(self, key: str) -> datetime:
        return self._required(key, _readable_time)

    def object(self, key: str) -> "_Reader":
        return _Reader(self.fields.get(key), self._path(key), self.unmapped)


def _statement_time(read: _Reader) -> datetime | None:
    """When the transaction last said what it says: Monzo's `updated`, else its creation;
    None when neither reads as a time."""
    return read.time("updated") or read.time("created")


def _monzo_id(value: object) -> str:
    """The Monzo id of a transaction, a core field every path needs."""
    return _Reader(value, "", set()).required_text("id")


def _mcc(metadata: _Reader) -> str | None:
    """The merchant category code: four ASCII digits, as the column's CHECK requires."""
    value = metadata.text("mcc")
    if value is None or re.fullmatch("[0-9]{4}", value):
        return value
    metadata.unreadable_field("mcc")
    return None


# A pot's fields by the account field each one fills, where the names differ.
_POT_FIELDS = {"type": "product_type"}
# The account fields a source detail it cannot read leaves as they are (SourceDetails.unknown).
_KEEPABLE = frozenset(f.name for f in fields(SourceDetails)) - {"tax_wrapper", "unknown"}


def _kept(read: _Reader, renamed: Mapping[str, str] | None = None) -> frozenset[str]:
    """The account fields to leave as they are: those whose source field it could not read.
    Other fields it could not read, such as a pot's name, have warned and are no detail."""
    return frozenset((renamed or {}).get(key, key) for key in read.unreadable) & _KEEPABLE


def _description(read: _Reader) -> str:
    """The merchant's name, else the counterparty's, else Monzo's description."""
    return (
        read.object("merchant").text("name")
        or read.object("counterparty").text("name")
        or read.text("description")
        or ""
    )


def _pot_id(read: _Reader) -> str | None:
    """The pot a transaction moves money to or from, if it is a pot move."""
    return read.object("metadata").text("pot_id")


def _bank_account(sort_code: str | None, account_number: str | None, whose: str) -> str | None:
    """The UK bank account a sort code and account number name, in the registry's form; None
    when they name none, or a malformed one (which is logged, never fatal)."""
    if not sort_code or not account_number:
        return None
    try:
        return normalize_identifier("uk_bank_account", f"{sort_code}:{account_number}")
    except FinanceValidationError:
        # The details are the owner's or a counterparty's, so they stay out of the log.
        logger.warning("%s names a malformed bank account; stored without it", whose)
        return None


async def _connector_transaction(
    session: AsyncSession, connection_id: UUID, external_id: str
) -> tuple[UUID, TransactionStatus, str | None] | None:
    """The transaction posted under this external id: its id, status and who voided it."""
    t = models.Transaction
    row = (
        await session.execute(
            select(t.id, t.status, t.voided_by).where(
                t.connection_id == connection_id, t.external_id == external_id
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return row.id, cast(TransactionStatus, row.status), row.voided_by


async def _latest_transactions(
    session: AsyncSession, connection_id: UUID
) -> list[tuple[dict[str, Any], str]]:
    """The latest raw copy of each Monzo transaction the connection fetched, with the Monzo
    account its page was fetched for. What each one is (posted, reversed or declined) is left
    to transaction(), which a replay tells not to post anything the ledger does not hold, such
    as a pending payment in the pages of a discarded attempt.

    The latest has the greatest Monzo `updated` (none counts as the oldest, and so does one
    that is not a valid time in that form), then fetch time, then record id: the fetch time
    alone would mislead, as a page fetched again keeps its first one. Picked by the database,
    so memory grows with the transactions, not with the copies of them each sync stores again.
    In a fixed order (the most recently fetched first), so a replay is repeatable.
    """
    r = RawRecord
    elements = func.jsonb_array_elements(r.content).table_valued(column("value", JSONB))
    tx = elements.c.value
    monzo_id = tx["id"].astext
    stated = tx["updated"].astext
    # Never a cast that can fail: one malformed copy must not fail the sync.
    readable = and_(
        stated.regexp_match(f"^{_ISO_TIME}$"), func.pg_input_is_valid(stated, "timestamptz")
    )
    updated = case((readable, stated.cast(DateTime(timezone=True))))
    latest = (
        select(tx.label("tx"), r.request["account_id"].astext.label("account"), r.fetched_at, r.id)
        .select_from(r)
        .join(elements, true())
        .where(r.connection_id == connection_id, r.record_type == "transactions_page")
        .ext(distinct_on(monzo_id))
        .order_by(monzo_id, updated.desc().nulls_last(), r.fetched_at.desc(), r.id.desc())
        .subquery()
    )
    rows = await session.execute(
        select(latest.c.tx, latest.c.account).order_by(
            latest.c.fetched_at.desc(), latest.c.id.desc(), latest.c.tx["id"].astext
        )
    )
    return [(row.tx, row.account) for row in rows]


async def _earliest_connector_posting(
    session: AsyncSession, account_id: UUID, connection_id: UUID, *, excluding: str
) -> datetime | None:
    """When the earliest transaction this connection posted to the account occurred, void
    ones included (history once fetched, whatever became of it), leaving out the one with
    the external id `excluding`."""
    t, p = models.Transaction, models.Posting
    return await session.scalar(
        select(func.min(t.occurred_at))
        .join(p, p.transaction_id == t.id)
        .where(
            p.account_id == account_id,
            t.connection_id == connection_id,
            t.external_id != excluding,
        )
    )


async def _ledger_balance(
    session: AsyncSession,
    account_id: UUID,
    instrument_id: UUID,
    *,
    excluding: str | None = None,
    before: datetime | None = None,
    connection_id: UUID | None = None,
) -> Decimal:
    """Everything posted to the account in the instrument, pending included, void excluded,
    and so is the transaction with the external id `excluding`; only what occurred before
    `before`, and only what the connection `connection_id` posted, when given."""
    t, p = models.Transaction, models.Posting
    query = (
        select(func.sum(p.quantity))
        .join(t, t.id == p.transaction_id)
        .where(p.account_id == account_id, p.instrument_id == instrument_id, t.status != "void")
    )
    if excluding is not None:  # IS DISTINCT FROM NULL would drop manual postings (NULL id)
        query = query.where(t.external_id.is_distinct_from(excluding))
    if before is not None:
        query = query.where(t.occurred_at < before)
    if connection_id is not None:
        query = query.where(t.connection_id == connection_id)
    total = await session.scalar(query)
    return total if total is not None else Decimal(0)


async def oldest_pending(
    session: AsyncSession, connection_id: UUID, config: dict[str, JSONValue], monzo_id: str
) -> datetime | None:
    """When the oldest transaction the ledger still holds as pending on the Monzo account was
    created; None if there is none. It must be fetched again until it settles or is voided."""
    account_map = config.get(ACCOUNT_MAP_KEY)
    account_id = account_map.get(monzo_id) if isinstance(account_map, dict) else None
    if not isinstance(account_id, str):
        return None
    t, p = models.Transaction, models.Posting
    return await session.scalar(
        select(func.min(t.occurred_at))
        .join(p, p.transaction_id == t.id)
        .where(
            t.connection_id == connection_id,
            t.status == "pending",
            p.account_id == UUID(account_id),
        )
    )


async def _posted(
    session: AsyncSession, transaction_id: UUID, account_id: UUID
) -> tuple[Decimal, datetime]:
    """What a transaction posted to the account, and when it occurred."""
    t, p = models.Transaction, models.Posting
    row = (
        await session.execute(
            select(p.quantity, t.occurred_at)
            .join(t, t.id == p.transaction_id)
            .where(t.id == transaction_id, p.account_id == account_id)
        )
    ).one()
    return row.quantity, row.occurred_at


async def _earliest_posting(
    session: AsyncSession, account_id: UUID, *, excluding: str
) -> tuple[datetime, date] | None:
    """The earliest time anything else on the account occurred, and its earliest booking
    date, each on its own: a booking date needn't follow the time (one entered by hand, or
    booked in another time zone), and the opening must precede both."""
    t, p = models.Transaction, models.Posting
    row = (
        await session.execute(
            select(func.min(t.occurred_at), func.min(t.booking_date))
            .join(p, p.transaction_id == t.id)
            .where(
                p.account_id == account_id,
                t.status != "void",
                t.external_id.is_distinct_from(excluding),
            )
        )
    ).one()
    # min() over no rows is NULL.
    occurred_at = cast(datetime | None, row[0])
    booking_date = cast(date | None, row[1])
    if occurred_at is None or booking_date is None:
        return None  # nothing else on the account
    return occurred_at, booking_date


async def _snapshot_still_holds(
    session: AsyncSession, account_id: UUID, instrument_id: UUID, quantity: Decimal
) -> bool:
    """Whether the latest snapshot already says `quantity` with nothing posted after it, so a
    new one would add nothing (a hybrid account counts postings made after its snapshot)."""
    s, t, p = models.BalanceSnapshot, models.Transaction, models.Posting
    latest = (
        await session.execute(
            select(s.quantity, s.as_of)
            .where(s.account_id == account_id, s.instrument_id == instrument_id)
            .order_by(s.as_of.desc(), s.recorded_at.desc())
            .limit(1)
        )
    ).one_or_none()
    if latest is None or latest.quantity != quantity:
        return False
    posted_since = await session.scalar(
        select(func.count())
        .select_from(p)
        .join(t, t.id == p.transaction_id)
        .where(
            p.account_id == account_id,
            p.instrument_id == instrument_id,
            t.status != "void",
            t.occurred_at > latest.as_of,
        )
    )
    return posted_since == 0


class _Normalizer:
    def __init__(self, ctx: SyncContext, tz: ZoneInfo, config: dict[str, JSONValue]) -> None:
        self.command = ctx.command
        self.session = ctx.command.session
        self.session_factory = ctx.session_factory
        self.connection_id = ctx.connection.id
        self.tz = tz
        self.observed_at = datetime.now(UTC)
        stored_map = config.get(ACCOUNT_MAP_KEY)
        self.account_map: dict[str, JSONValue] = (
            dict(stored_map) if isinstance(stored_map, dict) else {}
        )
        stored_boundaries = config.get(OPENING_BOUNDARIES_KEY)
        self.opening_boundaries: dict[str, JSONValue] = (
            dict(stored_boundaries) if isinstance(stored_boundaries, dict) else {}
        )
        self.report = NormalizeReport()
        self._currencies: dict[str, InstrumentRecord] = {}
        self._shared: dict[tuple[str, ...], UUID] = {}
        # Per Monzo merchant id: its place, written once per sync by prepare_places().
        self._places: dict[str, UUID] = {}
        # Recorded by finish(), once this sync's transactions are posted.
        self._snapshots: list[tuple[UUID, int, str, datetime]] = []
        self._deleted_pots: list[UUID] = []
        # Fields seen in a shape not mapped yet (structured-fields Decision 10), by their name.
        self.unmapped: set[str] = set()

    async def currency(self, code: str) -> InstrumentRecord:
        if code not in self._currencies:
            self._currencies[code] = await get_currency(self.session, code)
        return self._currencies[code]

    async def unassigned(self) -> UUID:
        return await self.system_account("unassigned")

    async def _shared_row(
        self, key: tuple[str, ...], get_or_create: Callable[[CommandContext], Awaitable[UUID]]
    ) -> UUID:
        """A row other connections share (a system account, an organisation, a place), found
        or created in a short transaction of its own and committed at once. Its creation is
        serialised across concurrent syncs, and the lock that does it must not be held until
        this sync, which may take minutes, commits."""
        if key not in self._shared:
            self._shared[key] = await self._in_short_transaction(get_or_create)
        return self._shared[key]

    async def _in_short_transaction(
        self, work: Callable[[CommandContext], Awaitable[UUID]]
    ) -> UUID:
        async with (
            self.session_factory() as session,
            command_context(session, self.command.actor, "finance.shared_row", {}) as ctx,
        ):
            return await work(ctx)

    async def system_account(self, key: SystemAccountKey) -> UUID:
        return await self._shared_row(
            ("system_account", key), lambda ctx: get_or_create_system_account(ctx, key)
        )

    async def organization(self, name: str, external_id: tuple[str, str] | None = None) -> UUID:
        return await self._shared_row(
            ("organization", name.lower(), *(external_id or ())),
            lambda ctx: get_or_create_organization(ctx, name, external_id=external_id),
        )

    async def identifier(self, value: str) -> UUID:
        """The registry row of a bank account (in the registry's form), found or created in a
        transaction of its own: its lock must not be held until this sync commits."""
        return await self._shared_row(
            ("identifier", "uk_bank_account", value),
            lambda ctx: get_or_create_identifier(ctx, "uk_bank_account", value),
        )

    async def own_identifier(self, monzo_id: str, read: _Reader) -> None:
        """Register the Monzo account's own bank details to its finance account, unless they
        are registered already (the owner's registration elsewhere wins)."""
        value = _bank_account(
            read.text("sort_code"), read.text("account_number"), f"Monzo account {monzo_id}"
        )
        if value is None:
            return
        # The row exists before ensure_identifier runs, so it takes no lock in this sync's
        # transaction (identifiers._get_or_create).
        await self.identifier(value)
        await ensure_identifier(
            self.command, self.require_mapped(monzo_id), "uk_bank_account", value
        )

    async def counterparty_fields(self, read: _Reader, external_id: str) -> CounterpartyFields:
        """What a transaction says about the other side; nothing for a pot move."""
        if _pot_id(read) is not None:
            return CounterpartyFields()
        counterparty = read.object("counterparty")
        value = _bank_account(
            counterparty.text("sort_code"),
            counterparty.text("account_number"),
            f"transaction {external_id}",
        )
        return CounterpartyFields(
            identifier_id=None if value is None else await self.identifier(value),
            name=counterparty.text("name"),
            reference=read.text("notes"),
            bank_transfer=read.text("scheme") in _BANK_TRANSFER_SCHEMES,
        )

    async def fill_counterparty_fields(self) -> None:
        """Give the transactions this connection posted before counterparty fields existed
        the fields their latest raw record gives (Decision 10). Run once, before this sync's
        transactions are posted, which come with their fields. The latest copy is the one the
        replay reads (_latest_transactions), so the two fill in the same fields."""
        latest = {
            _monzo_id(tx): tx
            for tx, _ in await _latest_transactions(self.session, self.connection_id)
        }
        t = models.Transaction
        posted = await self.session.execute(
            select(t.id, t.external_id)
            .where(t.connection_id == self.connection_id, t.origin == "connector")
            .order_by(t.occurred_at, t.id)
        )
        for row in list(posted):
            tx = latest.get(row.external_id)
            if tx is not None:
                fields = await self.counterparty_fields(
                    _Reader(tx, "", self.unmapped), row.external_id
                )
                await set_counterparty_fields(self.command, row.id, fields)

    async def contra(
        self,
        external_id: str,
        own_account_id: UUID,
        fields: CounterpartyFields,
        *,
        other_postings: int,
    ) -> UUID:
        """Unassigned or In transit, by the owner's decisions, the transfer the transaction
        is a leg of, its counterparty's bank account, and the postings it has besides its
        own and the contra (transfer_contra)."""
        await self.unassigned()  # both must exist for transfer_contra
        await self.system_account("in_transit")
        return await transfer_contra(
            self.session,
            source=("connection", f"{self.connection_id}:{external_id}"),
            own_account_id=own_account_id,
            counterparty_identifier_id=fields.identifier_id,
            other_postings=other_postings,
        )

    def mapped(self, monzo_id: str) -> UUID | None:
        value = self.account_map.get(monzo_id)
        return UUID(value) if isinstance(value, str) else None

    def require_mapped(self, monzo_id: str) -> UUID:
        account_id = self.mapped(monzo_id)
        if account_id is None:
            raise ValueError(f"Monzo account {monzo_id} has no finance account")
        return account_id

    def snapshot(
        self, account_id: UUID, minor_units: int, currency_code: str, as_of: datetime
    ) -> None:
        """Note the balance reported at `as_of` (when it was fetched), for finish()."""
        self._snapshots.append((account_id, minor_units, currency_code, as_of))

    async def finish(self) -> None:
        """Record the noted snapshots, then close the deleted pots.

        Only once this sync's transactions are posted: whether the latest snapshot still holds
        depends on what was posted after it, and a pot is closed after its last moves.
        """
        for snapshot in self._snapshots:
            await self._record_snapshot(*snapshot)
        for account_id in self._deleted_pots:
            if (await get_account(self.session, account_id)).status == "open":
                await close_account(self.command, account_id, local_date(self.observed_at, self.tz))

    async def _record_snapshot(
        self, account_id: UUID, minor_units: int, currency_code: str, as_of: datetime
    ) -> None:
        """Record the balance reported at `as_of`, unless the latest snapshot already
        accounts for it."""
        currency = await self.currency(currency_code)
        quantity = to_quantity(minor_units, currency.decimals)
        if await _snapshot_still_holds(self.session, account_id, currency.id, quantity):
            return
        await record_snapshot(
            self.command,
            account_id=account_id,
            instrument_id=currency.id,
            quantity=quantity,
            as_of=as_of,
            kind="reported",
        )

    async def account(self, account: object) -> str:
        """Map a Monzo account to its finance account, created when first seen; its Monzo id.
        Its id and creation are core: the sync's backfill starts from the creation."""
        read = _Reader(account, "account", self.unmapped)
        monzo_id = read.required_text("id")
        account_id = self.mapped(monzo_id)
        if account_id is None:
            name = (
                _ACCOUNT_NAMES.get(read.text("description") or "")
                or _ACCOUNT_NAMES.get(read.text("type") or "")
                or "Monzo"
            )
            created = await create_account(
                self.command,
                name=name,
                ledger_type="asset",
                subtype="bank",
                balance_mode="transactions",
                institution_entity_id=await self.organization("Monzo"),
                opened_on=local_date(read.required_time("created"), self.tz),
                source_connection_id=self.connection_id,
            )
            account_id = created.id
            self.account_map[monzo_id] = str(account_id)
        # On every sync: what Monzo says now, written only when it differs.
        await set_source_details(
            self.command,
            account_id,
            SourceDetails(
                product_type=read.text("product_type"),
                owner_type=read.text("owner_type"),
                unknown=_kept(read),
            ),
        )
        await self.own_identifier(monzo_id, read)
        return monzo_id

    async def pot(self, pot: object, parent_monzo_id: str, fetched_at: datetime) -> None:
        """Map a pot to its account, created when first seen, note its balance, and note it
        for closing when deleted. Only its id is core: a pot move posts against it."""
        read = _Reader(pot, "pot", self.unmapped)
        pot_id = read.required_text("id")
        account_id = self.mapped(pot_id)
        # An unreadable flag never closes a pot: a later sync could not open it again.
        deleted = read.flag("deleted") is True
        if account_id is None and deleted:
            return  # gone before we ever saw it
        code, currency = await self._pot_currency(read)
        details = self._pot_details(read, currency)
        if account_id is None:
            opened = read.time("created")
            created = await create_account(
                self.command,
                name=read.text("name") or "Pot",
                ledger_type="asset",
                subtype="savings_pot",
                # Hybrid: its moves value it until the first snapshot, then postings after it.
                balance_mode="hybrid",
                institution_entity_id=await self.organization("Monzo"),
                parent_account_id=self.require_mapped(parent_monzo_id),
                opened_on=None if opened is None else local_date(opened, self.tz),
                source_connection_id=self.connection_id,
            )
            account_id = created.id
            self.account_map[pot_id] = str(account_id)
        # On every sync, as for an account.
        await set_source_details(self.command, account_id, details)
        balance = read.minor_units("balance")
        if balance is not None and currency is not None:
            # One it cannot read or store is no snapshot: the pot keeps its last one.
            if _storable(to_quantity(balance, currency.decimals)):
                self.snapshot(account_id, balance, code, fetched_at)
            else:
                read.unreadable_field("balance")
        if deleted:
            self._deleted_pots.append(account_id)

    async def _pot_currency(self, read: _Reader) -> tuple[str, InstrumentRecord | None]:
        """The pot's currency code and instrument; no instrument when it cannot read the
        code or has no instrument for it, which warns."""
        code = read.text("currency")
        if code is None:
            return "", None
        try:
            return code, await self.currency(code)
        except UnknownCurrencyError:
            read.unreadable_field("currency")
            return code, None

    def _pot_details(self, read: _Reader, currency: InstrumentRecord | None) -> SourceDetails:
        goal = read.minor_units("goal_amount")
        goal_amount = None
        # A goal in a currency it cannot read keeps the stored one: its scale is unknown.
        unscaled = goal is not None and currency is None
        if goal is not None and currency is not None:
            goal_amount = to_quantity(goal, currency.decimals)
            if not _storable(goal_amount):
                read.unreadable_field("goal_amount")
                goal_amount = None
        details = SourceDetails(
            product_type=read.text("type"),
            goal_amount=goal_amount,
            locked=read.flag("locked"),
            round_up=read.flag("round_up"),
            round_up_multiplier=read.number("round_up_multiplier"),
            tax_wrapper=self._tax_wrapper(read),
        )
        # A field it cannot read keeps what the account has, as an unknown tax wrapper does.
        unknown = _kept(read, _POT_FIELDS) | ({"goal_amount"} if unscaled else set[str]())
        return replace(details, unknown=unknown)

    def _tax_wrapper(self, read: _Reader) -> TaxWrapper | None:
        """A pot's isa_wrapper as its tax wrapper: none when empty, else one Lararium knows by
        name. None leaves the account's as it is: when Monzo says nothing, and when it says
        something unknown, which warns instead of failing the sync."""
        value = read.text("isa_wrapper", keep_empty=True)
        if value is None:
            return None
        if value == "":
            return "none"
        wrapper = _TAX_WRAPPERS.get(value.lower())
        if wrapper is None:
            read.unreadable_field("isa_wrapper")
        return wrapper

    async def void_if_posted(self, external_id: str) -> bool:
        """Void the ledger transaction posted for this Monzo transaction; False if none."""
        existing = await _connector_transaction(self.session, self.connection_id, external_id)
        if existing is None:
            return False
        transaction_id, status, _ = existing
        if status == "void":
            self.report.unchanged += 1
        else:
            await void_transaction(self.command, transaction_id)
            self.report.voided += 1
        return True

    async def transaction(
        self, tx: dict[str, Any], monzo_account_id: str, *, backfill: bool = False
    ) -> None:
        """Apply one Monzo transaction: posted, voided when reversed, or recorded as declined.
        `backfill` (the replay): never post one the ledger does not already hold.

        Its id and amount are core, and so are its currency and creation where it is posted
        or recorded as declined (the reader-everywhere plan, Decision 4)."""
        read = _Reader(tx, "", self.unmapped)
        external_id = read.required_text("id")
        # What it says about its merchant holds whatever the ledger makes of it: declined,
        # reversed or posted, every Monzo merchant id gets its place.
        counterparty, place = await self.merchant(read)
        if read.states("decline_reason"):
            # Any stated reason is a decline, which is never posted, even one it cannot read.
            reason = read.text("decline_reason") or ""
            if not await self.void_if_posted(external_id):
                self.report.skipped_declined += 1
            await self.declined(read, external_id, monzo_account_id, counterparty, place, reason)
            return
        amount = read.required_minor_units("amount")
        if amount == 0:
            # A reversed pending payment; a new zero-amount one moves no money.
            await self.void_if_posted(external_id)
            return
        if backfill and (
            await _connector_transaction(self.session, self.connection_id, external_id) is None
        ):
            return
        currency = await self.currency(read.required_text("currency"))
        own_account_id = self.require_mapped(monzo_account_id)
        pot_id = _pot_id(read)
        fields = await self.counterparty_fields(read, external_id)
        fees = self._fees(read, amount, currency)
        # A pot that was deleted before the connection existed has no account; its moves are
        # posted like any other payment.
        other = (None if pot_id is None else self.mapped(pot_id)) or (
            # Its fee postings are the postings it has besides its own and the contra.
            await self.contra(external_id, own_account_id, fields, other_postings=len(fees))
        )
        created = read.required_time("created")
        # One it cannot read leaves it pending, so it is fetched again until it reads as settled.
        settled = read.time("settled")
        _, outcome = await upsert_connector_transaction(
            self.command,
            TransactionInput(
                occurred_at=created,
                booking_date=local_date(created, self.tz),
                value_date=None if settled is None else local_date(settled, self.tz),
                status="pending" if settled is None else "booked",
                description=_description(read),
                counterparty_entity_id=counterparty,
                origin="connector",
                connection_id=self.connection_id,
                external_id=external_id,
                postings=self._postings(read, amount, currency, own_account_id, other, fees),
                counterparty=fields,
                details=await self._details(read, amount, currency, place),
            ),
        )
        self.count(outcome)

    async def replay_copies(self, skipping: Collection[str]) -> list[tuple[dict[str, Any], str]]:
        """The latest raw copy of every Monzo transaction this connection fetched, except those
        in `skipping`, for a replay (structured-fields Decision 12): each is run through
        transaction(backfill=True), which decides what it is as for a sync and only refuses to
        post one the ledger does not hold. Through its upserts, an owner's void survives and
        an unchanged transaction is not written."""
        return [
            (tx, monzo_account_id)
            for tx, monzo_account_id in await _latest_transactions(self.session, self.connection_id)
            if _monzo_id(tx) not in skipping
        ]

    def _fees(self, read: _Reader, amount: int, currency: InstrumentRecord) -> dict[str, int]:
        """The transaction's fees that are posted on their own (structured-fields Decision 8),
        by kind, in minor units."""
        fees = {kind: fee for kind, fee in sorted(read.amounts("fees").items()) if fee != 0}
        before_fees = amount - sum(fees.values())
        storable = all(
            _storable(to_quantity(part, currency.decimals))
            for part in (before_fees, *fees.values())
        )
        if fees and (before_fees == 0 or not storable):
            # All of it fees, which would leave a zero posting on the account, or a fee the
            # ledger cannot store: the transaction is posted whole, and the fees stay raw.
            read.unreadable_field("fees")
            return {}
        return fees

    def _postings(
        self,
        read: _Reader,
        amount: int,
        currency: InstrumentRecord,
        account_id: UUID,
        other: UUID,
        fees: dict[str, int],
    ) -> list[PostingInput]:
        """Monzo's amount is what reached the balance, after its fees (structured-fields
        Decision 8): the account posts the amount before them, the other side -amount (at
        position 1, where M4 expects it), and each fee (_fees) is a posting of its own on the
        account."""
        before_fees = amount - sum(fees.values())
        return [
            PostingInput(
                account_id,
                currency.id,
                to_quantity(before_fees, currency.decimals),
                memo=read.text("notes") or "",
            ),
            PostingInput(other, currency.id, -to_quantity(amount, currency.decimals)),
            *(
                PostingInput(
                    account_id,
                    currency.id,
                    to_quantity(fee, currency.decimals),
                    is_fee=True,
                    memo=kind,
                )
                for kind, fee in fees.items()
            ),
        ]

    async def _details(
        self, read: _Reader, amount: int, currency: InstrumentRecord, place: UUID | None
    ) -> TransactionDetails:
        # Always null so far: nothing shows its shape yet.
        read.warn_if_stated("atm_fees_detailed")
        original_quantity, original_instrument_id = await self._original(read)
        return TransactionDetails(
            original_quantity=original_quantity,
            original_instrument_id=original_instrument_id,
            category=read.text("category"),
            labels=read.texts("labels"),
            include_in_spending=read.flag("include_in_spending"),
            payment_scheme=read.text("scheme"),
            place_entity_id=place,
            category_splits=self._category_splits(read, amount, currency.decimals),
        )

    async def _original(self, read: _Reader) -> tuple[Decimal | None, UUID | None]:
        """The amount in the currency it was spent in, signed like the account's posting
        (structured-fields Decision 5). A currency with no instrument leaves both out, with a
        warning: its decimals, which the amount in minor units needs, are unknown."""
        code, minor_units = read.text("local_currency"), read.minor_units("local_amount")
        if code is None or minor_units is None:
            # A pair: half of it stated without the other cannot be stored, which warns rather
            # than vanishes (a half it could not read has warned already).
            if code is not None and "local_amount" not in read.unreadable:
                read.unreadable_field("local_currency")
            if minor_units is not None and "local_currency" not in read.unreadable:
                read.unreadable_field("local_amount")
            return None, None
        try:
            local = await self.currency(code)
        except UnknownCurrencyError:
            read.unreadable_field("local_currency")
            return None, None
        quantity = to_quantity(minor_units, local.decimals)
        if not _storable(quantity):
            read.unreadable_field("local_amount")
            return None, None
        return quantity, local.id

    def _category_splits(
        self, read: _Reader, amount: int, decimals: int
    ) -> tuple[CategorySplit, ...]:
        """Monzo's split of the amount between categories, only when it is one
        (structured-fields Decision 7): more than one entry, or one other than
        {category: amount}, which is derivable."""
        amounts = read.amounts("categories")
        category = read.text("category")
        if not amounts or (category is not None and amounts == {category: amount}):
            return ()
        splits = tuple(
            CategorySplit(category, to_quantity(part, decimals))
            for category, part in sorted(amounts.items())
        )
        if not all(_storable(split.quantity) for split in splits):
            read.unreadable_field("categories")
            return ()
        return splits

    async def merchant(self, read: _Reader) -> tuple[UUID | None, UUID | None]:
        """The merchant's brand, an organization merged by name, and its place, one per Monzo
        merchant id (structured-fields Decision 6): one name can cover several ids, each with
        its own address, logo and online flag."""
        merchant = read.object("merchant")
        name, merchant_id = merchant.text("name"), merchant.text("id")
        tag = None if merchant_id is None else ("monzo_merchant", merchant_id)
        # A brand is known by its name; a place by the merchant id alone.
        organization = None if name is None else await self.organization(name, external_id=tag)
        if merchant_id is None:
            return organization, None
        if merchant_id not in self._places:  # a transaction prepare_places() was not given
            await self.prepare_places([read.fields])
        return organization, self._places[merchant_id]

    async def prepare_places(self, txs: Iterable[dict[str, Any]]) -> None:
        """Write each merchant's place once, from everything these transactions say about it,
        folded by statement time (_fold), before any of them is applied: so neither the order
        they come in, the replay's included, nor how many of them disagree decides what is
        stored, and an unchanged sync writes nothing."""
        said: dict[str, list[_Statement]] = {}
        for tx in txs:
            read = _Reader(tx, "", self.unmapped)
            merchant = read.object("merchant")
            merchant_id = merchant.text("id")
            if merchant_id is None:
                continue
            details = self._place_details(merchant, read.object("metadata"))
            said.setdefault(merchant_id, []).append(
                _Statement(_statement_time(read), merchant.text("name"), details)
            )
        for merchant_id, statements in said.items():
            name, details = _fold(statements)
            self._places[merchant_id] = await self._in_short_transaction(
                partial(
                    get_or_create_place,
                    name=name,
                    external_id=("monzo_merchant", merchant_id),
                    details=details,
                )
            )

    def _place_details(self, merchant: _Reader, metadata: _Reader) -> PlaceDetails:
        """The merchant, with what the transaction's metadata says about where it was paid."""
        address = merchant.object("address")
        return PlaceDetails(
            address_street=address.text("address"),
            address_city=address.text("city"),
            address_region=address.text("region"),
            address_postcode=address.text("postcode"),
            address_country=address.text("country"),
            address_formatted=address.text("formatted"),
            latitude=address.number("latitude"),
            longitude=address.number("longitude"),
            location_approximate=address.flag("approximate"),
            online=merchant.flag("online"),
            mcc=_mcc(metadata),
            website=merchant.object("metadata").text("website"),
            card_acceptor_website=metadata.text("card_acceptor_website"),
            logo_url=merchant.text("logo"),
        )

    async def declined(
        self,
        read: _Reader,
        external_id: str,
        monzo_account_id: str,
        counterparty: UUID | None,
        place: UUID | None,
        reason: str,
    ) -> None:
        """Record a declined payment, outside the ledger (structured-fields Decision 11)."""
        currency = await self.currency(read.required_text("currency"))
        await upsert_declined(
            self.command,
            DeclinedInput(
                connection_id=self.connection_id,
                external_id=external_id,
                account_id=self.require_mapped(monzo_account_id),
                occurred_at=read.required_time("created"),
                quantity=to_quantity(read.required_minor_units("amount"), currency.decimals),
                instrument_id=currency.id,
                description=_description(read),
                counterparty_entity_id=counterparty,
                place_entity_id=place,
                decline_reason=reason,
            ),
        )

    def count(self, outcome: Literal["created", "updated", "unchanged"]) -> None:
        if outcome == "created":
            self.report.created += 1
        elif outcome == "updated":
            self.report.updated += 1
        else:
            self.report.unchanged += 1

    def opening_boundary(self, monzo_id: str) -> datetime | None:
        value = self.opening_boundaries.get(monzo_id)
        return _parse(value) if isinstance(value, str) else None

    async def _earliest(self, monzo_id: str) -> datetime | None:
        return await _earliest_connector_posting(
            self.session,
            self.require_mapped(monzo_id),
            self.connection_id,
            excluding=f"opening:{monzo_id}",
        )

    def _move_boundary(self, monzo_id: str, to: datetime) -> None:
        """The boundary only ever moves earlier: the opening accounts for everything before
        the earliest point it has covered."""
        stored = self.opening_boundary(monzo_id)
        boundary = to if stored is None else min(stored, to)
        self.opening_boundaries[monzo_id] = boundary.astimezone(UTC).isoformat()

    async def adopt_boundary(self, monzo_id: str) -> None:
        """Give an account its boundary from what this connection posted before this run
        (run before it posts anything): one reconciled before boundaries were recorded has its
        opening accounting for all of that. An account with nothing posted yet gets none, and
        its first opening is computed from Monzo's balance."""
        if self.opening_boundary(monzo_id) is not None:
            return
        earliest = await self._earliest(monzo_id)
        if earliest is not None:
            self._move_boundary(monzo_id, earliest)

    async def record_opening_boundary(self, monzo_id: str, balance_at: datetime) -> None:
        """Record that the opening accounts for everything before this connection's earliest
        posting on the account. With none, it accounts for everything in the balance read at
        `balance_at`: any transaction a later fetch finds before then is in that balance, so
        it must move the opening, however recent (the account's creation would be too early
        when this run fetched only the last 89 days)."""
        earliest = await self._earliest(monzo_id)
        self._move_boundary(monzo_id, min(earliest, balance_at) if earliest else balance_at)

    async def opening(
        self,
        monzo_id: str,
        monzo_account: object,
        minor_units: int,
        code: str,
        balance_at: datetime,
    ) -> None:
        """Post the difference between Monzo's balance (read at `balance_at`) and the rest of
        the ledger, the day
        before the earliest other transaction. An opening the owner voided stays void; one no
        longer needed is voided by the connector, and posted again if it is needed again.

        It is computed from Monzo's balance only the first time, which records where the
        history it accounts for begins (the boundary), even when no opening is needed. After
        that only this connection's postings before the boundary, which a later backfill
        fetched, move it (ruling R18): anything else that changed since, such as a
        transaction the owner voided or posted by hand, is a reconciliation mismatch to
        report, never absorbed into it.
        """
        external_id = f"opening:{monzo_id}"
        existing = await _connector_transaction(self.session, self.connection_id, external_id)
        if existing is not None and existing[1] == "void" and existing[2] != "connector":
            self.report.unchanged += 1
            return
        posted = existing if existing is not None and existing[1] != "void" else None
        account_id = self.require_mapped(monzo_id)
        currency = await self.currency(code)
        # None only for an account with nothing posted before this run (see adopt_boundary).
        boundary = self.opening_boundary(monzo_id)
        if boundary is None:
            balance = to_quantity(minor_units, currency.decimals)
            others = await _ledger_balance(
                self.session, account_id, currency.id, excluding=external_id
            )
            difference = balance - others
        else:
            placed = (
                (await _posted(self.session, posted[0], account_id))[0]
                if posted is not None
                else Decimal(0)
            )
            older = await _ledger_balance(
                self.session,
                account_id,
                currency.id,
                excluding=external_id,
                before=boundary,
                connection_id=self.connection_id,
            )
            difference = placed - older
        await self.record_opening_boundary(monzo_id, balance_at)
        if difference == 0:
            if posted is not None:
                await void_transaction(self.command, posted[0])
                self.report.voided += 1
            return
        earliest = await _earliest_posting(self.session, account_id, excluding=external_id)
        if earliest is None:
            created = _Reader(monzo_account, "account", self.unmapped).required_time("created")
            earliest = (created, local_date(created, self.tz))
        occurred_at, booking_date = earliest
        _, outcome = await upsert_connector_transaction(
            self.command,
            TransactionInput(
                occurred_at=occurred_at - timedelta(days=1),
                booking_date=booking_date - timedelta(days=1),
                description=OPENING_DESCRIPTION,
                origin="connector",
                connection_id=self.connection_id,
                external_id=external_id,
                postings=[
                    PostingInput(account_id, currency.id, difference),
                    PostingInput(
                        await self.system_account("opening_balances"),
                        currency.id,
                        -difference,
                    ),
                ],
            ),
        )
        self.count(outcome)

    async def reconcile(self, monzo_id: str, minor_units: int, code: str) -> tuple[str, str] | None:
        """(difference, warning) for this account, or None when the ledger agrees."""
        account_id = self.require_mapped(monzo_id)
        currency = await self.currency(code)
        balance = to_quantity(minor_units, currency.decimals)
        ledger = await _ledger_balance(self.session, account_id, currency.id)
        difference = balance - ledger
        if difference.copy_abs() <= tolerance(currency.decimals):
            return None
        # Shown at the currency's precision, so an empty ledger reads 0.00, not 0.
        ledger = ledger.quantize(Decimal(1).scaleb(-currency.decimals))
        return to_plain_str(difference), (
            f"{_mismatch_prefix(monzo_id)}: Monzo reports {to_plain_str(balance)} {code},"
            f" the ledger has {to_plain_str(ledger)} {code}"
            f" (difference {to_plain_str(difference)} {code})"
        )


async def normalize(
    ctx: SyncContext,
    fetched: Sequence[FetchedRecord],
    *,
    reconciled: Collection[str] = (),
    openings: Collection[str] = (),
) -> NormalizeReport:
    """Apply one sync's responses to the ledger.

    `openings`: the Monzo accounts whose opening balance adjustment is (re)computed in this
    sync: those whose full backfill it completed, and those it saw first. `reconciled`: the
    accounts that already have one, which are checked against Monzo's balance (as are
    `openings`, after their adjustment).

    Its arithmetic on amounts is exact, however many digits they have, as the ledger's
    balance check is: the default context rounds to 28 digits, so a negation or a difference
    could otherwise leave postings that don't balance, or an opening that is off.
    """
    with localcontext(prec=MAX_PREC):
        return await _normalize(ctx, fetched, reconciled=reconciled, openings=openings)


async def _normalize(
    ctx: SyncContext,
    fetched: Sequence[FetchedRecord],
    *,
    reconciled: Collection[str],
    openings: Collection[str],
) -> NormalizeReport:
    session = ctx.command.session
    connection = await get_connection(session, ctx.connection.id)
    n = _Normalizer(ctx, await get_timezone(session), connection.config)
    records = await get_raw_records(session, [f.record_id for f in fetched])
    responses = [
        (record.record_type, f.request, f.fetched_at, cast(Any, record.content))
        for f, record in zip(fetched, records, strict=True)
    ]

    accounts: dict[str, object] = {}
    for record_type, _, _, content in responses:
        if record_type == "accounts":
            for account in content:
                accounts[await n.account(account)] = account
    # Before anything is posted, so this run's transactions can't count as earlier history.
    for monzo_id in accounts:
        await n.adopt_boundary(monzo_id)
    if connection.config.get(COUNTERPARTY_FIELDS_KEY) is not True:
        await n.fill_counterparty_fields()
    # Per account: its balance in minor units and its currency, core fields of the opening
    # and the reconciliation, and when it was read.
    balances: dict[str, tuple[int, str, datetime]] = {}
    for record_type, request, fetched_at, content in responses:
        if record_type == "pots":
            for pot in content:
                await n.pot(pot, str(request["current_account_id"]), fetched_at)
        elif record_type == "balance":
            monzo_id = str(request["account_id"])
            read = _Reader(content, "balance", n.unmapped)
            minor_units, code = read.required_minor_units("balance"), read.required_text("currency")
            balances[monzo_id] = (minor_units, code, fetched_at)
            n.snapshot(n.require_mapped(monzo_id), minor_units, code, fetched_at)
    pages = [
        (str(request["account_id"]), content)
        for record_type, request, _, content in responses
        if record_type == "transactions_page"
    ]
    # Under a new version, first the history this sync's pages don't cover, once the accounts
    # and pots it posts to are mapped (and after adopt_boundary, which must precede any posting).
    replayed = (
        await n.replay_copies(skipping={_monzo_id(tx) for _, content in pages for tx in content})
        if connection.config.get(NORMALIZER_VERSION_KEY) != NORMALIZER_VERSION
        else []
    )
    # Every merchant's place from all it is said to be in this sync, before anything links it.
    await n.prepare_places(
        [tx for tx, _ in replayed] + [tx for _, content in pages for tx in content]
    )
    for tx, monzo_account_id in replayed:
        await n.transaction(tx, monzo_account_id, backfill=True)
    for monzo_account_id, content in pages:
        for tx in content:
            await n.transaction(tx, monzo_account_id)
    await n.finish()

    checked = set(reconciled) | set(openings)
    mismatches: dict[str, tuple[str, str] | None] = {}
    for monzo_id, (minor_units, code, balance_at) in balances.items():
        if monzo_id not in checked:
            continue
        if monzo_id in openings:
            await n.opening(monzo_id, accounts[monzo_id], minor_units, code, balance_at)
        mismatches[monzo_id] = await n.reconcile(monzo_id, minor_units, code)

    # Warnings and notifications: one warning per mismatched account; the owner hears about
    # each new difference once, and a fixed one is forgotten.
    stored_notified = connection.config.get(NOTIFIED_KEY)
    notified: dict[str, JSONValue] = (
        dict(stored_notified) if isinstance(stored_notified, dict) else {}
    )
    warnings = [
        w
        for w in connection.warnings
        if not any(w.startswith(_mismatch_prefix(m)) for m in mismatches)
    ]
    # Each unmapped field warns once, until the owner clears it. Added before the mismatch
    # warnings, which are taken out and added again on each sync, so the order holds.
    for name in sorted(n.unmapped):
        if (warning := _unmapped_warning(name)) not in warnings:
            warnings.append(warning)
    for monzo_id, mismatch in mismatches.items():
        if mismatch is None:
            notified.pop(monzo_id, None)
            continue
        difference, warning = mismatch
        warnings.append(warning)
        n.report.mismatches.append(warning)
        if notified.get(monzo_id) != difference:
            notified[monzo_id] = difference
            await _notify(ctx, f"{connection.display_name}: {warning}")

    # Only this normaliser's keys: others (such as an OAuth state) may have changed meanwhile.
    values: dict[str, JSONValue] = {
        ACCOUNT_MAP_KEY: n.account_map,
        NORMALIZER_VERSION_KEY: NORMALIZER_VERSION,
        COUNTERPARTY_FIELDS_KEY: True,
    }
    if n.opening_boundaries:
        values[OPENING_BOUNDARIES_KEY] = n.opening_boundaries
    if notified:
        values[NOTIFIED_KEY] = notified
    remove = [] if notified or NOTIFIED_KEY not in connection.config else [NOTIFIED_KEY]
    if remove or any(connection.config.get(key) != value for key, value in values.items()):
        await update_config(ctx.command, connection.id, values, remove=remove)
    if warnings != connection.warnings:
        await set_warnings(ctx.command, connection.id, warnings)
    return n.report
