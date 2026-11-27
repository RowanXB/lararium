"""Trading 212's two sources of a cash movement: the API's transaction and the CSV export's row
(design §5.3a, §5.7, Decision 14).

Both key it `transaction:<reference>`: the export's `ID` is the API's `reference` (as the owner's
export shows). The API's values make the postings and the export only enriches (its typed
row links the transaction, and its Notes are evidence of the movement's kind, `evidence`,
design §5.9); a deposit or withdrawal only the export has posts from it, under the key the API
would use, so the API's later arrival re-posts it in place only if its values differ. The
export's sign comes from its `Action`, its amount from `Total`. Another `Action` (a trade, a
dividend, interest) is stored typed and flagged, and posts nothing: how its rows match the
API's records is not known until the owner's first real ones show it.

A disagreement in the amount, the currency or the booking date opens a review item (`take:
"statement"` means the export's value), told to the owner once. Messages name a transaction by
the last four characters of its reference and the field, never a value.
"""

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select

from lararium.core import models as core_models
from lararium.core.connectors import SyncContext
from lararium.core.json_types import JSONValue
from lararium.core.raw_records import RawRecord, get_raw_records
from lararium.core.timeutil import local_date
from lararium.modules.finance import source_merge
from lararium.modules.finance.trading212.fields import (
    EXPORT_COLUMNS,
    EXPORT_KEY,
    EXPORT_RECORD_TYPE,
    EXPORT_TABLE,
    NOTES_ACCOUNT_NUMBER,
    NOTES_SORT_CODE,
    NOTES_TRANSACTION_ID,
)
from lararium.modules.finance.trading212.state import LONDON, Run, Trading212DataError
from lararium.modules.finance.trading212_notes import SENT_TO_BANK_NOTES, TRANSACTION_ID_NOTES
from lararium.modules.finance.typed_tables import (
    TypedValueError,
    parse_decimal,
    parse_moment,
    typed_values,
)

TRANSACTION = "transaction:"
# The fields a disagreement is about, in the order reviews name them.
FIELDS = ("amount", "currency", "booking_date")
# Taking the export's booking date takes its moment too.
TOGETHER = {"booking_date": ("occurred_at",)}
# The export's actions that post: the sign of the account's leg, and the API's type for the
# same movement (whose description and contra it takes).
ACTIONS: Mapping[str, tuple[int, str]] = {"Deposit": (1, "DEPOSIT"), "Withdrawal": (-1, "WITHDRAW")}
# The API's types an export row is compared with.
COMPARED = frozenset({"DEPOSIT", "WITHDRAW"})
NOTES_ID = re.compile(rf"^{TRANSACTION_ID_NOTES}$")
# A withdrawal's Notes name the bank account it was sent to (design §5.9).
NOTES_SENT = re.compile(rf"^{SENT_TO_BANK_NOTES}$")
# An action fit to name in a flag: words, as Trading 212 writes them ("Market buy").
_ACTION_NAME = re.compile(r"[A-Za-z][A-Za-z0-9 ()/.,'-]{0,59}")


@dataclass(frozen=True)
class CashEvent:
    """What one source says of a cash movement: the account's leg (signed), its currency,
    and when it happened."""

    amount: Decimal
    currency: str
    booking_date: date
    occurred_at: datetime


@dataclass(frozen=True)
class ExportRecord:
    """A stored export row: its raw record and cells."""

    id: UUID
    cells: Mapping[str, str]
    fetched_at: datetime

    @property
    def reference(self) -> str:
        return self.cells.get(EXPORT_KEY, "").strip()

    @property
    def key(self) -> str:
        return f"{TRANSACTION}{self.reference}"

    @property
    def action(self) -> str:
        return self.cells.get("Action", "").strip()


def _as_export(raw: RawRecord) -> ExportRecord | None:
    content = raw.content
    if raw.record_type != EXPORT_RECORD_TYPE or not isinstance(content, dict):
        return None
    cells = content.get("cells")
    if not isinstance(cells, dict):
        return None
    texts = {k: v for k, v in cells.items() if isinstance(v, str)}
    return ExportRecord(raw.id, texts, raw.fetched_at)


def _latest(raws: Sequence[RawRecord]) -> dict[str, ExportRecord]:
    """The export rows by reference, the latest fetched of each."""
    found: dict[str, ExportRecord] = {}
    for record in sorted(
        (r for r in map(_as_export, raws) if r is not None), key=lambda r: (r.fetched_at, r.id)
    ):
        if record.reference:
            found[record.reference] = record
    return found


async def given(run: Run, raw_record_ids: Sequence[UUID]) -> set[str]:
    """The references of the export rows among the records a run was given: only those are
    read, not the sync's other records. `stored` resolves them."""
    if not raw_record_ids:
        return set()
    r = core_models.RawRecord
    ids = list(
        await run.session.scalars(
            select(r.id).where(r.id.in_(list(raw_record_ids)), r.record_type == EXPORT_RECORD_TYPE)
        )
    )
    return set(_latest(await get_raw_records(run.session, ids)))


async def stored(run: Run, references: Collection[str] | None) -> dict[str, ExportRecord]:
    """The connection's stored export rows (of these references; None: all), by reference."""
    if references is not None and not references:
        return {}
    r = core_models.RawRecord
    query = select(r.id).where(
        r.connection_id == run.connection_id, r.record_type == EXPORT_RECORD_TYPE
    )
    if references is not None:
        query = query.where(r.content["cells"][EXPORT_KEY].astext.in_(sorted(references)))
    ids = list(await run.session.scalars(query.order_by(r.fetched_at, r.id)))
    return _latest(await get_raw_records(run.session, ids))


def _number(record: ExportRecord, column: str) -> Decimal:
    try:
        return parse_decimal(record.cells.get(column, ""), f"{EXPORT_RECORD_TYPE} {column}")
    except TypedValueError as error:
        raise Trading212DataError(str(error)) from None


def _moment(record: ExportRecord) -> datetime:
    label = f"{EXPORT_RECORD_TYPE} Time (UTC)"
    try:
        moment = parse_moment(record.cells.get("Time (UTC)", ""), label)
    except TypedValueError as error:
        raise Trading212DataError(str(error)) from None
    if moment.tzinfo is None:
        raise Trading212DataError(f"{label} has no time zone")
    return moment


def currency(record: ExportRecord) -> str:
    """The row's currency code, as compared and as stored."""
    return record.cells.get("Currency (Total)", "").strip().upper()


def event(record: ExportRecord) -> CashEvent | None:
    """What the row says of the movement, when its action is one that posts."""
    if record.action not in ACTIONS:
        return None
    sign, _ = ACTIONS[record.action]
    occurred_at = _moment(record)
    return CashEvent(
        _number(record, "Total").copy_abs() * sign,
        currency(record),
        local_date(occurred_at, LONDON),
        occurred_at,
    )


def flag(run: Run, record: ExportRecord) -> None:
    """A row whose action posts nothing is flagged, by its action when that reads as one."""
    if record.action in ACTIONS:
        return
    named = _ACTION_NAME.fullmatch(record.action) is not None
    run.flag(f"export action {record.action}" if named else "export action")


def notes_transaction_id(notes: str) -> str | None:
    found = NOTES_ID.match(notes.strip())
    return found.group(1) if found else None


def notes_bank_account(notes: str) -> tuple[str, str] | None:
    """(sort code, account number), digits only, of a "Sent to Bank Account" note."""
    found = NOTES_SENT.match(notes.strip())
    return ("".join(found.group(1, 2, 3)), found.group(4)) if found else None


@dataclass(frozen=True)
class Evidence:
    """What an export row's Notes say of a deposit or withdrawal (design §5.9): `family`, a
    move from or to another Trading 212 account (empty Notes); `outside`, money outside the
    family (a transaction id, or a bank account, whose parts are then given)."""

    family: bool
    outside: bool
    sort_code: str | None
    account_number: str | None


# What a row of another form, or of another action, says: nothing.
NO_EVIDENCE = Evidence(False, False, None, None)


def has_known_notes(notes: str) -> bool:
    """Whether Notes are one of the three forms a deposit or withdrawal prints (§5.9)."""
    return (
        not notes.strip()
        or notes_transaction_id(notes) is not None
        or notes_bank_account(notes) is not None
    )


def evidence(record: ExportRecord) -> Evidence:
    """The evidence of the row's Notes. A row of another action, with Notes of another form
    (an upload refuses those, export.py), or of a file without a Notes column says nothing."""
    if record.action not in ACTIONS or "Notes" not in record.cells:
        return NO_EVIDENCE
    notes = record.cells["Notes"]
    if not notes.strip():
        return Evidence(True, False, None, None)
    if notes_transaction_id(notes) is not None:
        return Evidence(False, True, None, None)
    if (account := notes_bank_account(notes)) is not None:
        return Evidence(False, True, *account)
    return NO_EVIDENCE


async def write(run: Run, record: ExportRecord, transaction_id: UUID | None) -> None:
    """The row typed in finance.trading212_export_rows, linking the transaction it enriches
    or posts (None: neither)."""
    by_column: dict[str, str | None] = {}
    labels: dict[str, str] = {}
    for header, column in EXPORT_COLUMNS.items():
        if header in record.cells:
            by_column[column] = record.cells[header]
            labels[column] = f"{EXPORT_RECORD_TYPE} {header}"
    if "Currency (Total)" in record.cells:
        by_column[EXPORT_COLUMNS["Currency (Total)"]] = currency(record)
    notes = record.cells.get("Notes", "")
    by_column[NOTES_TRANSACTION_ID] = notes_transaction_id(notes)
    by_column[NOTES_SORT_CODE], by_column[NOTES_ACCOUNT_NUMBER] = notes_bank_account(notes) or (
        None,
        None,
    )
    try:
        values = dict(await typed_values(run.session, EXPORT_TABLE, by_column, labels))
    except TypedValueError as error:
        raise Trading212DataError(str(error)) from None
    values.pop("id", None)
    await run.upsert(
        EXPORT_TABLE,
        {"connection_id": run.connection_id, "id": record.reference},
        values | {"transaction_id": transaction_id, "raw_record_id": record.id},
    )


async def notify(ctx: SyncContext, display_name: str) -> dict[str, JSONValue]:
    """Tell the owner of the open review items not told yet, once (source_merge); returns
    the cursor's record of those told."""
    return await source_merge.notify_reviews(
        ctx,
        display_name,
        sources="Trading 212's API and export",
        name=lambda key: f"transaction {source_merge.ending(key.removeprefix(TRANSACTION))}",
        label="a Trading 212",
    )
