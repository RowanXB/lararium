"""Staging: parse an uploaded statement, classify its rows and store them for review.

Nothing is posted here: commit.py turns a staged batch's new rows into transactions.
"""

import asyncio
import hashlib
import json
import threading
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import MAX_PREC, Decimal, localcontext
from typing import Any, Literal, cast, get_args
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from lararium.core.commands import CommandContext
from lararium.core.documents import DocumentStore, record_document
from lararium.core.entities import get_self_entity_id
from lararium.core.json_types import JSONValue
from lararium.core.settings_store import get_timezone
from lararium.modules.finance import models
from lararium.modules.finance.accounts import AccountRecord, get_account
from lararium.modules.finance.errors import (
    FileFormatError,
    ImportBatchNotFoundError,
    ImportNotAllowedError,
    NotACurrencyError,
    TransactionImportedError,
    UnknownCurrencyError,
)
from lararium.modules.finance.importing import csv_parser, ofx_parser, pdf, qif_parser
from lararium.modules.finance.importing.parsed import (
    ParsedRow,
    ParsedStatement,
    RowError,
    SkippedRow,
    StatementAccount,
    StatementRate,
)
from lararium.modules.finance.importing.profiles import (
    CsvProfileConfig,
    OfxProfileConfig,
    PdfProfileConfig,
    ProfileConfigModel,
    QifProfileConfig,
    TimeUnit,
    get_profile,
)
from lararium.modules.finance.instruments import InstrumentRecord, get_currency, get_instrument
from lararium.modules.finance.ledger import (
    PostingInput,
    TransactionInput,
    post_transaction,
    void_transaction,
)
from lararium.modules.finance.money import to_plain_str
from lararium.modules.finance.system_accounts import get_or_create_system_account

BatchStatus = Literal["staged", "committed", "discarded", "rolled_back"]
RowStatus = Literal["new", "duplicate", "skipped", "error", "committed", "rolled_back"]
RowKind = Literal["statement", "opening_adjustment"]

ROW_STATUSES: tuple[RowStatus, ...] = get_args(RowStatus)
# No balance the opening can come from: none in the rows, and no closing the file lists
# everything up to (an OFX LEDGERBAL as of the download day, after its period, isn't).
NO_BALANCES_WARNING = (
    "This file gives no balance to take its opening from, so its opening balance can't be checked"
)
OPENING_DESCRIPTION = "Opening balance adjustment"
OUTSIDE_COVERED_DAY = (
    "this period was already imported from another file, and this row is not in it"
)


@dataclass(frozen=True)
class ImportRowRecord:
    id: UUID
    position: int
    kind: RowKind
    status: RowStatus
    booking_date: date | None
    occurred_at: datetime | None
    time_unit: TimeUnit | None  # what the file gave occurred_at to; None with it
    interest: bool  # the row pays interest: never a bank transfer
    description: str
    quantity: Decimal | None
    fee: Decimal  # NOT NULL: 0 when the file states none
    instrument_id: UUID | None
    balance: Decimal | None
    external_id: str | None
    bank_type: str | None
    value_date: date | None
    payee: str | None
    memo: str | None
    check_number: str | None
    counterparty_name: str | None
    counterparty_sort_code: str | None
    counterparty_account_number: str | None
    counterparty_bank: str | None
    counterparty_account_ending: str | None
    payment_reference: str | None
    payment_kind: str | None
    bank_reference: str | None
    purpose_code: str | None
    remittance_info: str | None
    original_currency: str | None
    exchange_rate: Decimal | None
    started_at: datetime | None
    card_number: str | None
    original_amount: Decimal | None
    fx_rate: Decimal | None
    fx_reference_rate: Decimal | None
    error: str | None
    note: str | None
    raw: dict[str, str]
    transaction_id: UUID | None
    duplicate_of: UUID | None  # the committed row this one duplicates (Decision 16)
    # Adjustment rows only: True when a committed statement of the account starts earlier,
    # so the adjustment points at a missing period rather than the opening (Decision 13).
    is_gap: bool | None = None


@dataclass(frozen=True)
class StatementRecord:
    """What the file says at statement level (Decision 18); fields it doesn't print are None."""

    period_from: date | None
    period_to: date | None
    currency: str | None
    opening_balance: Decimal | None
    closing_balance: Decimal | None
    closing_balance_date: date | None
    money_in: Decimal | None
    money_out: Decimal | None
    interest: Decimal | None
    payments_in: int | None
    payments_out: int | None
    statement_reference: str | None
    document_date: date | None
    account_type: str | None
    account_name: str | None
    sort_code: str | None
    account_number: str | None
    iban: str | None
    bic: str | None
    customer_number: str | None
    clearing_account_number: str | None
    branch: str | None
    bank: str | None
    addressee: str | None
    address: str | None
    overdraft_limit: Decimal | None
    overdraft_limit_date: date | None
    available_balance: Decimal | None
    available_balance_date: date | None
    account_reference: str | None
    account_opened_on: date | None
    rates: list[StatementRate]


@dataclass(frozen=True)
class ImportBatchRecord:
    id: UUID
    account_id: UUID
    profile_id: UUID
    profile_version: int
    document_id: UUID
    filename: str
    status: BatchStatus
    created_at: datetime
    committed_at: datetime | None
    rolled_back_at: datetime | None
    counts: dict[RowStatus, int]  # every status, zeros included
    warnings: list[str]
    statement: StatementRecord | None


# The statement columns, in StatementRecord's order (rates live in their own table).
_STATEMENT_FIELDS = [f.name for f in fields(StatementRecord) if f.name != "rates"]


# Pure rules ----------------------------------------------------------------------------


def _chronological[T](rows: Sequence[T], row_order: str) -> list[T]:
    return list(reversed(rows)) if row_order == "newest_first" else list(rows)


def _plain(value: Decimal) -> str:
    """Canonical text for an amount: "-3.5" for -3.50, and "0" for -0.00."""
    # Amounts may have 40 digits on each side (money.require_amount); every computation on
    # them here runs at unbounded precision, as ledger.py does, never rounding at 28 digits.
    with localcontext(prec=MAX_PREC):
        return to_plain_str((value + 0).normalize())


def _text(value: str) -> str:
    return " ".join(value.casefold().split())


def _hash(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def _same_row(row: ParsedRow, currency: str, timed: bool = False) -> tuple[str, ...]:
    """What tells a row from others without an external id: its day, amount, currency and
    description, and its time of day when timed (see _deduplicate)."""
    day = (row.booking_date.isoformat(),)
    if timed and row.occurred_at is not None:
        day += (row.occurred_at.astimezone(UTC).isoformat(),)
    return (*day, _plain(row.quantity), currency, _text(row.description))


def dedupe_key(
    account_id: UUID, row: ParsedRow, currency: str, occurrence: int, timed: bool = False
) -> str:
    """Decision 5: by external id, else by running balance, else by occurrence in the file.
    For a row keyed by its balance, occurrence counts the file's earlier rows alike in every
    field the key takes. A balance can come back to the same figure on the same day (the same
    transfer in twice, a payment between): in a file without times of day such a repeat is a
    row of its own and takes its occurrence (owner ruling, 2026-11-19), while the first keeps
    the key it always had."""
    account = str(account_id)
    if row.external_id is not None:
        return _hash("ext", account, row.external_id)
    common = _same_row(row, currency, timed)
    if row.balance is not None:
        if occurrence == 0:
            return _hash("bal", account, *common, _plain(row.balance))
        return _hash("bal", account, *common, _plain(row.balance), str(occurrence))
    return _hash("seq", account, *common, str(occurrence))


def _opening_key(
    account_id: UUID, batch_id: UUID, day: date, quantity: Decimal, currency: str
) -> str:
    # The batch too: two statements starting on one day may need the same adjustment.
    return _hash(
        "open", str(account_id), str(batch_id), day.isoformat(), _plain(quantity), currency
    )


def check_balance_chain(
    rows: Sequence[ParsedRow | None],
    instrument_of: Callable[[ParsedRow], UUID],
    row_order: str,
) -> dict[int, str]:
    """Decision 12: position -> message for each stated balance that doesn't follow from the
    previous one in its instrument. None stands for a parse error, which resets every chain."""
    expected: dict[UUID, Decimal] = {}
    errors: dict[int, str] = {}
    with localcontext(prec=MAX_PREC):
        for row in _chronological(rows, row_order):
            if row is None:
                expected.clear()
                continue
            instrument = instrument_of(row)
            running = expected.get(instrument)
            if running is not None:
                running += row.quantity - row.fee
            if row.balance is not None:
                if running is not None and running != row.balance:
                    errors[row.position] = (
                        "balance doesn't follow from the previous row: expected "
                        f"{to_plain_str(running)}, the file says {to_plain_str(row.balance)}"
                    )
                running = row.balance
            if running is not None:
                expected[instrument] = running
    return errors


def opening_adjustments(
    rows: list[ParsedRow],
    instrument_of: Callable[[ParsedRow], UUID],
    closing: tuple[Decimal, date, UUID] | None,
    row_order: str,
) -> dict[UUID, tuple[Decimal, date]]:
    """Decision 13: the statement's opening balance and first date per instrument.

    From the chronologically first row with a balance, else from the closing balance
    (amount, date, its instrument). That is the account's balance in the statement's
    currency, so it is used only when every row is in that one instrument (a row in another
    currency is in it too, converted), and when it is dated on or after the last row.
    Instruments with neither are absent.
    """
    chains: dict[UUID, list[ParsedRow]] = defaultdict(list)
    for row in _chronological(rows, row_order):
        chains[instrument_of(row)].append(row)
    openings: dict[UUID, tuple[Decimal, date]] = {}
    with localcontext(prec=MAX_PREC):
        for instrument, chain in chains.items():
            first_day = min(row.booking_date for row in chain)
            net = Decimal(0)
            opening: Decimal | None = None
            for row in chain:
                net += row.quantity - row.fee
                if row.balance is not None:
                    opening = row.balance - net
                    break
            # A closing balance dated before the last row doesn't include it.
            last_day = max(row.booking_date for row in chain)
            if (
                opening is None
                and closing is not None
                and chains.keys() == {closing[2]}
                and closing[1] >= last_day
            ):
                opening = closing[0] - sum((row.quantity - row.fee for row in chain), Decimal(0))
            if opening is not None:
                openings[instrument] = (opening, first_day)
    return openings


# Queries --------------------------------------------------------------------------------


async def ledger_balance_before(
    session: AsyncSession, account_id: UUID, instrument_id: UUID, day: date
) -> Decimal:
    """The account's balance in the instrument from non-void transactions booked before day."""
    p, t = models.Posting, models.Transaction
    total = await session.scalar(
        select(func.coalesce(func.sum(p.quantity), 0))
        .join(t, t.id == p.transaction_id)
        .where(
            p.account_id == account_id,
            p.instrument_id == instrument_id,
            t.status != "void",
            t.booking_date < day,
        )
    )
    assert total is not None  # coalesce
    return Decimal(total)


def _row_record(row: models.ImportRow, is_gap: bool | None = None) -> ImportRowRecord:
    values = {f.name: getattr(row, f.name) for f in fields(ImportRowRecord) if f.name != "is_gap"}
    return ImportRowRecord(**values, is_gap=is_gap)


async def list_batch_rows(session: AsyncSession, batch_id: UUID) -> list[ImportRowRecord]:
    """The batch's rows by position (opening adjustments first).

    An adjustment row's is_gap is worked out here, at read time, so it follows commits and
    rollbacks of the account's other batches.
    """
    r = models.ImportRow
    rows = list(
        await session.scalars(
            select(r)
            .where(r.batch_id == batch_id)
            .order_by(r.position)
            .execution_options(populate_existing=True)
        )
    )
    adjustments = [row for row in rows if row.kind == "opening_adjustment"]
    if not adjustments:
        return [_row_record(row) for row in rows]
    others = [
        batch
        for batch in await _committed_batches(session, adjustments[0].account_id)
        if batch.id != batch_id
    ]
    claims = await _claims(session, [*others, await _batch_info(session, batch_id)])
    # Where this statement's opening sits, and the earliest another committed one's does,
    # per instrument (OpeningClaim.opens_on).
    mine = {claim.instrument_id: claim.opens_on for claim in claims.get(batch_id, [])}
    earliest: dict[UUID, date] = {}
    for claim in (c for owner, found in claims.items() if owner != batch_id for c in found):
        known = earliest.get(claim.instrument_id, claim.opens_on)
        earliest[claim.instrument_id] = min(known, claim.opens_on)

    def is_gap(row: models.ImportRow) -> bool | None:
        # A superseded (rolled back) adjustment is no longer a balance gap, or anything.
        if row.kind != "opening_adjustment" or row.status == "rolled_back":
            return None
        assert row.instrument_id is not None
        first_day, other = mine.get(row.instrument_id), earliest.get(row.instrument_id)
        return first_day is not None and other is not None and other < first_day

    return [_row_record(row, is_gap(row)) for row in rows]


async def _load_batches(
    session: AsyncSession, *conditions: ColumnElement[bool]
) -> list[ImportBatchRecord]:
    """Batches matching all conditions, newest first."""
    b, r, s, q = (
        models.ImportBatch,
        models.ImportRow,
        models.ImportStatement,
        models.ImportStatementRate,
    )
    batches = (
        await session.execute(
            select(
                b.id,
                b.account_id,
                b.profile_id,
                b.profile_version,
                b.document_id,
                b.filename,
                b.status,
                b.created_at,
                b.committed_at,
                b.rolled_back_at,
                b.warnings,
            )
            .where(*conditions)
            .order_by(b.created_at.desc(), b.id.desc())
        )
    ).all()
    ids = [batch.id for batch in batches]
    counts: dict[UUID, dict[RowStatus, int]] = {
        batch_id: dict.fromkeys(ROW_STATUSES, 0) for batch_id in ids
    }
    for batch_id, status, n in await session.execute(
        select(r.batch_id, r.status, func.count())
        .where(r.batch_id.in_(ids))
        .group_by(r.batch_id, r.status)
    ):
        counts[batch_id][cast(RowStatus, status)] = n
    rates: dict[UUID, list[StatementRate]] = defaultdict(list)
    for rate in await session.execute(
        select(
            q.batch_id,
            q.kind,
            q.band_from,
            q.band_to,
            q.rate,
            q.ear,
            q.valid_from,
            q.valid_to,
            q.frequency,
        )
        .where(q.batch_id.in_(ids))
        .order_by(q.batch_id, q.position)
    ):
        rates[rate.batch_id].append(
            StatementRate(
                rate.kind,
                rate.band_from,
                rate.band_to,
                rate.rate,
                rate.ear,
                rate.valid_from,
                rate.valid_to,
                rate.frequency,
            )
        )
    statements: dict[UUID, StatementRecord] = {}
    columns = [getattr(s, name) for name in _STATEMENT_FIELDS]
    for row in await session.execute(select(s.batch_id, *columns).where(s.batch_id.in_(ids))):
        values = {name: getattr(row, name) for name in _STATEMENT_FIELDS}
        statements[row.batch_id] = StatementRecord(**values, rates=rates[row.batch_id])
    return [
        ImportBatchRecord(
            id=batch.id,
            account_id=batch.account_id,
            profile_id=batch.profile_id,
            profile_version=batch.profile_version,
            document_id=batch.document_id,
            filename=batch.filename,
            status=cast(BatchStatus, batch.status),
            created_at=batch.created_at,
            committed_at=batch.committed_at,
            rolled_back_at=batch.rolled_back_at,
            counts=counts[batch.id],
            warnings=list(batch.warnings),
            statement=statements.get(batch.id),
        )
        for batch in batches
    ]


async def batches_relying_on(session: AsyncSession, batch_id: UUID) -> list[tuple[UUID, str]]:
    """The other committed batches (id, filename) with rows that duplicate this batch's rows
    (Decision 16): rolling this batch back would leave the days they skipped uncovered."""
    r, b = models.ImportRow, models.ImportBatch
    target = aliased(models.ImportRow)
    found = await session.execute(
        select(b.id, b.filename)
        .distinct()
        .join(r, r.batch_id == b.id)
        .join(target, target.id == r.duplicate_of)
        .where(target.batch_id == batch_id, b.id != batch_id, b.status == "committed")
        .order_by(b.filename, b.id)
    )
    return [(row.id, row.filename) for row in found]


async def require_not_imported(session: AsyncSession, transaction_id: UUID) -> None:
    """TransactionImportedError when a committed import's row (or adjustment) posted the
    transaction. The import tables take a committed row's transaction to be live, so it is
    voided only by rolling the import back (or by align_openings), never by hand."""
    r, b = models.ImportRow, models.ImportBatch
    filename = await session.scalar(
        select(b.filename)
        .join(r, r.batch_id == b.id)
        .where(r.transaction_id == transaction_id, r.status == "committed")
        .limit(1)
    )
    if filename is not None:
        raise TransactionImportedError(filename)


async def get_batch(session: AsyncSession, batch_id: UUID) -> ImportBatchRecord:
    found = await _load_batches(session, models.ImportBatch.id == batch_id)
    if not found:
        raise ImportBatchNotFoundError(str(batch_id))
    return found[0]


async def list_batches(session: AsyncSession) -> list[ImportBatchRecord]:
    """Every batch, newest first."""
    return await _load_batches(session)


# Opening adjustments (Decision 13) ------------------------------------------------------

SUPERSEDED = "superseded"
# A staged batch's adjustment rows and new rows count in the walk while it is previewed.
_LIVE_ROWS = ("new", "duplicate", "committed")


@dataclass(frozen=True)
class OpeningClaim:
    """A batch's statement opening balance in one instrument: the balance just before the
    file's first listed row, which is on first_day."""

    batch_id: UUID
    instrument_id: UUID
    opening: Decimal
    first_day: date
    committed_at: datetime | None  # None: the staged batch being previewed, after its ties
    # The movements the file lists itself (its own rows, and the originals its duplicates
    # stand for, whatever their dates): never before its opening.
    own: frozenset[UUID] = frozenset()
    # The first day its opening is vouched for from (final review C1): its first day listed
    # in full when that is not after first_day, else first_day.
    vouched_from: date | None = None
    # It lists first_day in full: no other movement that day can be before its opening.
    first_day_in_full: bool = False
    # The last day its rows carry its balance through in this instrument: the end of the
    # days it lists in full, or, when they end before its first row (or there are none),
    # just its opening (the day before first_day). Per claim, never per batch.
    vouched_to: date | None = None
    # The movements it lists on first_day. An earlier file listing any of them lists the
    # start of that day's run too, so it speaks of the same opening: no row order needed
    # (an OFX may list newest first, and its positions are file order).
    first_day_sources: frozenset[UUID] = frozenset()
    # The day its adjustment is dated (adjustment_day): before the run of days that every
    # file of the account, with an opening or not, lists in full up to its opening, so the
    # adjustment lands inside none of them. Set by _claims; None means opens_on's.
    dated_on: date | None = None

    @property
    def opens_on(self) -> date:
        """The day its opening sits in, the one place the walk order, the dating and the
        gap flag all take it from. A claim listing its first day in full opens at the start
        of its first day listed in full (vouched_from: no movement lies between that and
        its first row); one listing it in part opens partway through its first day."""
        if self.first_day_in_full and self.vouched_from is not None:
            return self.vouched_from
        return self.first_day

    @property
    def adjustment_day(self) -> date:
        """Where its adjustment is dated: the day before opens_on when the opening is a
        start of day (the gap is before that day starts); on it otherwise, after whatever
        another file lists of that day, so the adjustment never lands inside a day another
        file lists in full."""
        if self.dated_on is not None:
            return self.dated_on
        if self.first_day_in_full:
            return self.opens_on - timedelta(days=1)
        return self.opens_on


@dataclass(frozen=True)
class Movement:
    """One ledger movement of an account in an instrument: a transaction's postings there,
    or a staged row being previewed (source is the transaction's or the row's id)."""

    day: date
    amount: Decimal
    source: UUID


type Movements = dict[UUID, list[Movement]]


def _walk_order(claim: OpeningClaim) -> tuple[date, bool, bool, datetime, UUID]:
    """By where its opening sits: the day (opens_on), a start of day before partway
    through it; only then committed before the staged batch, and by commit time. So the
    order, and with it each adjustment's day and owner, never depends on import order
    where the files say when their openings are."""
    staged = claim.committed_at is None
    return (
        claim.opens_on,
        not claim.first_day_in_full,
        staged,
        claim.committed_at or datetime.min.replace(tzinfo=UTC),
        claim.batch_id,
    )


def plan_openings(
    claims: Sequence[OpeningClaim], movements: Movements
) -> dict[tuple[UUID, UUID], Decimal]:
    """The walk: (batch, instrument) -> the adjustment that makes each opening hold.

    Claims are taken by first date (ties by commit time). Each needs its opening minus the
    account's movements (its postings other than opening adjustments) before its first
    listed row, minus the adjustments assigned to the claims before it.

    Before its first listed row means by what the file lists, not by date alone: every
    movement before its first day, except those it lists itself. Its own rows and the
    originals its duplicates stand for come after its opening even when a duplicate's
    original is dated earlier (an id matched across days). On a first day it lists only in
    part (an export may start partway through a day), another file's movement it doesn't
    list came before it too; on a first day it lists in full there is no such movement
    (staging refuses one), and none is counted.
    """

    assigned: dict[UUID, Decimal] = defaultdict(Decimal)
    needed: dict[tuple[UUID, UUID], Decimal] = {}
    with localcontext(prec=MAX_PREC):
        for claim in sorted(claims, key=_walk_order):
            before = sum(
                (
                    movement.amount
                    for movement in movements.get(claim.instrument_id, [])
                    if movement.source not in claim.own
                    and (
                        movement.day < claim.first_day
                        or (movement.day == claim.first_day and not claim.first_day_in_full)
                    )
                ),
                Decimal(0),
            )
            need = claim.opening - before - assigned[claim.instrument_id]
            needed[(claim.batch_id, claim.instrument_id)] = need
            assigned[claim.instrument_id] += need
    return needed


@dataclass(frozen=True)
class OpeningConflict:
    """A claim that needs an adjustment where an earlier statement vouches for the balance:
    the two files disagree about it. ledger is the balance the walk has there."""

    batch_id: UUID
    instrument_id: UUID
    first_day: date
    opening: Decimal
    ledger: Decimal
    vouched_by: UUID


def opening_conflicts(
    claims: Sequence[OpeningClaim], needed: dict[tuple[UUID, UUID], Decimal]
) -> list[OpeningConflict]:
    """Final review C1: an adjustment is legitimate only where no other statement vouches
    for the balance (a gap, or the account's first statement).

    An earlier claim in the walk (in the same instrument) vouches for the balance at the
    start of every day from its first date to the day after its vouched_to: its opening
    holds at its first date, and its rows carry it through the days it lists in full. A
    claim whose opening is such a start of day (it lists its first day in full) must then
    need nothing. One whose first day it lists only in part opens partway through that
    day, before its first row: the earlier claim vouches for that only if it lists the
    whole day, or lists that row too (then both give the balance before the same row).
    """
    earlier: dict[UUID, list[OpeningClaim]] = defaultdict(list)
    conflicts: list[OpeningConflict] = []
    with localcontext(prec=MAX_PREC):
        for claim in sorted(claims, key=_walk_order):
            need = needed[(claim.batch_id, claim.instrument_id)]
            if need != 0:
                start = claim.vouched_from or claim.first_day
                for prior in earlier[claim.instrument_id]:
                    end = prior.vouched_to
                    if end is None:
                        continue
                    if claim.first_day_in_full:
                        vouched = start <= end + timedelta(days=1)
                    else:
                        shared = bool(claim.first_day_sources & prior.own)
                        vouched = claim.first_day <= end or shared
                    if vouched:
                        conflicts.append(
                            OpeningConflict(
                                claim.batch_id,
                                claim.instrument_id,
                                claim.first_day,
                                claim.opening,
                                claim.opening - need,
                                prior.batch_id,
                            )
                        )
                        break
            earlier[claim.instrument_id].append(claim)
    return conflicts


def _conflict_message(conflict: OpeningConflict, names: dict[UUID, str]) -> str:
    file, other = names[conflict.batch_id], names[conflict.vouched_by]
    return (
        f"{file} gives a balance of {to_plain_str(conflict.opening)} before "
        f"{conflict.first_day.isoformat()}, but {other}, which covers that day, gives "
        f"{to_plain_str(conflict.ledger)}: the two files disagree. Check which one is right, "
        "and roll the other back or leave it out"
    )


@dataclass(frozen=True)
class _BatchInfo:
    id: UUID
    filename: str
    committed_at: datetime | None
    timezone: str | None
    complete_from: date | None
    complete_to: date | None
    default_instrument_id: UUID | None  # the closing balance's when the file states none


def _by_position(instruments: dict[int, UUID]) -> Callable[[ParsedRow], UUID]:
    return lambda parsed: instruments[parsed.position]


async def _claims(
    session: AsyncSession, batches: list[_BatchInfo]
) -> dict[UUID, list[OpeningClaim]]:
    """Each batch's openings, from its stored statement rows (opening_adjustments)."""
    b, r, s = models.ImportBatch, models.ImportRow, models.ImportStatement
    ids = [batch.id for batch in batches]
    configs = {
        row.id: row.profile_config
        for row in await session.execute(select(b.id, b.profile_config).where(b.id.in_(ids)))
    }
    defaults = {batch.id: batch.default_instrument_id for batch in batches}
    closings: dict[UUID, tuple[Decimal, date, UUID]] = {}
    period_ends: dict[UUID, date | None] = {}
    for batch_id, balance, day, code, period_to in await session.execute(
        select(
            s.batch_id, s.closing_balance, s.closing_balance_date, s.currency, s.period_to
        ).where(s.batch_id.in_(ids))
    ):
        # The closing is in the statement's currency, or the one picked at upload, as the
        # rows are (_resolve_currencies): it anchors that instrument's chain only. And only
        # if the file lists everything up to it: an OFX LEDGERBAL is often the balance when
        # it was downloaded, after its period, and would take movements the file never
        # lists into the opening.
        instrument_id = (await get_currency(session, code)).id if code else defaults[batch_id]
        if balance is not None and day is not None and instrument_id is not None:
            closings[batch_id] = (balance, day, instrument_id)
            period_ends[batch_id] = period_to
    stored: dict[UUID, list[models.ImportRow]] = defaultdict(list)
    for row in await session.scalars(
        select(r)
        .where(r.batch_id.in_(ids), r.kind == "statement", r.status.in_(_LIVE_ROWS))
        .order_by(r.batch_id, r.position)
    ):
        stored[row.batch_id].append(row)
    # A closing anchors an opening only if the file lists everything up to it: dated within
    # its period, or by its last row when that runs past the period end.
    for batch_id, (_, day, _) in list(closings.items()):
        rows_end = max(
            (row.booking_date for row in stored[batch_id] if row.booking_date), default=None
        )
        ends = [end for end in (period_ends[batch_id], rows_end) if end is not None]
        if ends and day > max(ends):
            del closings[batch_id]
    # Each batch's days listed in full, by the instruments it has live rows in.
    full_days: dict[UUID, list[tuple[date, date]]] = defaultdict(list)
    for batch in batches:
        if batch.complete_from is not None and batch.complete_to is not None:
            for instrument_id in {row.instrument_id for row in stored[batch.id]}:
                if instrument_id is not None:
                    full_days[instrument_id].append((batch.complete_from, batch.complete_to))
    originals = aliased(models.ImportRow)
    original_of = {
        row_id: transaction_id
        for row_id, transaction_id in await session.execute(
            select(r.id, originals.transaction_id)
            .join(originals, originals.id == r.duplicate_of)
            .where(r.batch_id.in_(ids), r.status == "duplicate")
        )
        if transaction_id is not None
    }
    claims: dict[UUID, list[OpeningClaim]] = {}
    for batch in batches:
        instruments: dict[int, UUID] = {}
        rows: list[ParsedRow] = []
        sources = (_movement_source(row, original_of) for row in stored[batch.id])
        own = frozenset(source for source in sources if source is not None)
        for row in stored[batch.id]:
            # Live rows have these (the table's CHECK).
            assert row.booking_date is not None and row.quantity is not None
            assert row.instrument_id is not None
            instruments[row.position] = row.instrument_id
            rows.append(
                ParsedRow(
                    position=row.position,
                    booking_date=row.booking_date,
                    occurred_at=None,
                    description=row.description,
                    quantity=row.quantity,
                    fee=row.fee,
                    currency=None,
                    balance=row.balance,
                    external_id=None,
                    bank_type=None,
                    raw={},
                )
            )
        row_order = configs[batch.id].get("row_order", "oldest_first")
        openings = opening_adjustments(
            rows,
            _by_position(instruments),
            closings.get(batch.id),
            row_order if isinstance(row_order, str) else "oldest_first",
        )
        start, end = batch.complete_from, batch.complete_to
        claims[batch.id] = [
            OpeningClaim(
                batch.id,
                instrument_id,
                opening,
                first_day,
                batch.committed_at,
                own=own,
                vouched_from=start if start is not None and start <= first_day else first_day,
                first_day_in_full=start is not None
                and end is not None
                and start <= first_day <= end,
                vouched_to=(
                    first_day - timedelta(days=1)
                    if end is None
                    else max(first_day - timedelta(days=1), end)
                ),
                first_day_sources=frozenset(
                    source
                    for row in stored[batch.id]
                    if row.instrument_id == instrument_id
                    and row.booking_date == first_day
                    and (source := _movement_source(row, original_of)) is not None
                ),
            )
            for instrument_id, (opening, first_day) in openings.items()
        ]
    # Dated only once every claim is known: a run may carry an adjustment back no further
    # than the opening of the claim before it in the walk, which must still hold there.
    flat = sorted((claim for found in claims.values() for claim in found), key=_walk_order)
    previous: dict[UUID, date] = {}
    dated: dict[tuple[UUID, UUID], date] = {}
    for claim in flat:
        day = _dated_on(claim, full_days[claim.instrument_id])
        floor = previous.get(claim.instrument_id)
        dated[(claim.batch_id, claim.instrument_id)] = day if floor is None else max(day, floor)
        previous[claim.instrument_id] = claim.opens_on
    for batch_id, found in claims.items():
        claims[batch_id] = [
            replace(claim, dated_on=dated[(batch_id, claim.instrument_id)]) for claim in found
        ]
    return claims


def _dated_on(claim: OpeningClaim, full_days: list[tuple[date, date]]) -> date:
    """OpeningClaim.dated_on. An opening at a start of day (or partway through a day that
    some file lists in full) is the balance as that day starts. Then every run of days some
    file lists in full that reaches it carries it back, row by listed row, to the start of
    the run: the adjustment is dated the day before. Otherwise it is dated on its day."""
    covered = any(start <= claim.opens_on <= end for start, end in full_days)
    if not claim.first_day_in_full and not covered:
        return claim.opens_on
    boundary = claim.opens_on
    moved = True
    while moved:
        moved = False
        for start, end in full_days:
            if start < boundary <= end + timedelta(days=1):
                boundary, moved = start, True
    return boundary - timedelta(days=1)


def _movement_source(row: models.ImportRow, original_of: dict[UUID, UUID]) -> UUID | None:
    """The movement a live statement row stands for: its transaction once committed, itself
    while staged (preview_openings adds staged new rows as movements by their ids), or its
    original's transaction for a duplicate."""
    if row.status == "committed":
        return row.transaction_id
    if row.status == "new":
        return row.id
    return original_of.get(row.id)


def _batch_info_columns() -> tuple[Any, ...]:
    b = models.ImportBatch
    return (
        b.id,
        b.filename,
        b.committed_at,
        b.profile_config["timezone"].astext,
        b.complete_from,
        b.complete_to,
        b.default_instrument_id,
    )


async def _batch_info(session: AsyncSession, batch_id: UUID) -> _BatchInfo:
    found = await session.execute(
        select(*_batch_info_columns()).where(models.ImportBatch.id == batch_id)
    )
    return _BatchInfo(*found.one())


async def _committed_batches(session: AsyncSession, account_id: UUID) -> list[_BatchInfo]:
    b = models.ImportBatch
    found = await session.execute(
        select(*_batch_info_columns()).where(b.account_id == account_id, b.status == "committed")
    )
    return [_BatchInfo(*row) for row in found]


async def _movements(session: AsyncSession, account_id: UUID) -> Movements:
    """The account's non-void postings by instrument and transaction, without opening
    adjustments nor M4's placeholders: the walk reads what statements and real records
    say. A placeholder is matching's estimate of a side not yet in, which matching posts
    and takes off at any time (another account's import, a sync); an adjustment sized
    around one would go stale, and one on a day a statement lists in full would read as
    a gap. None lies on days the account's imports list or open with: a transfer there is
    before_history (M4's manual_predates)."""
    p, t, r, x = models.Posting, models.Transaction, models.ImportRow, models.Transfer
    adjustments = select(r.transaction_id).where(
        r.kind == "opening_adjustment", r.transaction_id.is_not(None)
    )
    placeholders = select(x.placeholder_transaction_id).where(
        x.placeholder_transaction_id.is_not(None)
    )
    movements: Movements = defaultdict(list)
    for instrument_id, transaction_id, day, amount in await session.execute(
        select(p.instrument_id, t.id, t.booking_date, func.sum(p.quantity))
        .join(t, t.id == p.transaction_id)
        .where(
            p.account_id == account_id,
            t.status != "void",
            t.id.not_in(adjustments),
            t.id.not_in(placeholders),
        )
        .group_by(p.instrument_id, t.id, t.booking_date)
    ):
        movements[instrument_id].append(Movement(day, amount, transaction_id))
    return movements


async def _live_adjustments(
    session: AsyncSession, account_id: UUID
) -> dict[tuple[UUID, UUID], models.ImportRow]:
    """The committed adjustment rows of the account's committed batches."""
    r, b = models.ImportRow, models.ImportBatch
    rows = await session.scalars(
        select(r)
        .join(b, b.id == r.batch_id)
        .where(
            r.account_id == account_id,
            r.kind == "opening_adjustment",
            r.status == "committed",
            b.status == "committed",
        )
        .execution_options(populate_existing=True)
    )
    live: dict[tuple[UUID, UUID], models.ImportRow] = {}
    for row in rows:
        assert row.instrument_id is not None
        live[(row.batch_id, row.instrument_id)] = row
    return live


@dataclass(frozen=True)
class OpeningChange:
    """Another batch's adjustment that committing the previewed batch would change."""

    batch_id: UUID
    filename: str
    instrument_id: UUID
    current: Decimal
    needed: Decimal


@dataclass(frozen=True)
class OpeningPreview:
    """The walk with a staged batch included. needed: its adjustment per instrument it has
    an opening in (zero when none is needed); changes: what it does to the other batches."""

    needed: dict[UUID, Decimal]
    # Where each of its adjustments is dated (OpeningClaim.adjustment_day).
    days: dict[UUID, date]
    changes: list[OpeningChange]
    # Final review C1: where the batch and another statement disagree about a balance.
    conflicts: list[str]


async def preview_openings(session: AsyncSession, batch_id: UUID) -> OpeningPreview:
    """Decision 13's walk as if the staged batch were committed now: its new rows count as
    movements. Staging adds its adjustment rows from this; commit compares it with them.

    Only transactions accounts take adjustments: for any other mode, nothing is needed.
    """
    r = models.ImportRow
    account_id = await session.scalar(
        select(models.ImportBatch.account_id).where(models.ImportBatch.id == batch_id)
    )
    assert account_id is not None
    if (await get_account(session, account_id)).balance_mode != "transactions":
        return OpeningPreview(needed={}, days={}, changes=[], conflicts=[])
    staged = await _batch_info(session, batch_id)
    batches = [
        *(batch for batch in await _committed_batches(session, account_id) if batch.id != batch_id),
        replace(staged, committed_at=None),  # previewed as if committed after the others
    ]
    claims = await _claims(session, batches)
    movements = await _movements(session, account_id)
    with localcontext(prec=MAX_PREC):
        for row_id, instrument_id, day, quantity, fee in await session.execute(
            select(r.id, r.instrument_id, r.booking_date, r.quantity, r.fee).where(
                r.batch_id == batch_id, r.kind == "statement", r.status == "new"
            )
        ):
            assert instrument_id is not None and day is not None and quantity is not None
            movements[instrument_id].append(Movement(day, quantity - fee, row_id))
    flat = [claim for found in claims.values() for claim in found]
    plan = plan_openings(flat, movements)
    live = await _live_adjustments(session, account_id)
    names = {batch.id: batch.filename for batch in batches}
    conflicts = [_conflict_message(conflict, names) for conflict in opening_conflicts(flat, plan)]
    changes: list[OpeningChange] = []
    for owner, instrument_id in sorted(plan.keys() | live.keys(), key=str):
        if owner == batch_id:
            continue
        need = plan.get((owner, instrument_id), Decimal(0))
        row = live.get((owner, instrument_id))
        current = row.quantity if row is not None and row.quantity is not None else Decimal(0)
        if current != need:
            changes.append(OpeningChange(owner, names[owner], instrument_id, current, need))
    needed = {
        instrument_id: need for (owner, instrument_id), need in plan.items() if owner == batch_id
    }
    days = {claim.instrument_id: claim.adjustment_day for claim in claims.get(batch_id, [])}
    return OpeningPreview(needed=needed, days=days, changes=changes, conflicts=conflicts)


async def _post_adjustment(
    ctx: CommandContext,
    account_id: UUID,
    instrument_id: UUID,
    quantity: Decimal,
    day: date,
    local_tz: ZoneInfo,
) -> UUID:
    opening_account = await get_or_create_system_account(ctx, "opening_balances")
    with localcontext(prec=MAX_PREC):
        contra = -quantity
    record = await post_transaction(
        ctx,
        TransactionInput(
            occurred_at=_midnight(day, local_tz),
            occurred_tz=local_tz.key,
            booking_date=day,
            description=OPENING_DESCRIPTION,
            postings=[
                PostingInput(account_id, instrument_id, quantity),
                PostingInput(opening_account, instrument_id, contra),
            ],
        ),
    )
    return record.id


async def align_openings(ctx: CommandContext, account_id: UUID) -> None:
    """Decision 13: re-align the committed batches' opening adjustments after history
    changed (a commit or a rollback), so every committed statement's opening holds.

    A changed adjustment's transaction is voided and a new one posted; an adjustment that
    is no longer needed turns its row rolled_back ("superseded"); a batch that now needs
    one gets a new row.

    It never meets an opening conflict (final review C1): staging and commit refuse them,
    and a rollback only removes a statement. A remaining claim that an earlier statement
    vouches for still gets that statement's balance, since the rows it covers stay posted:
    rollback is refused while another batch relies on the rolled-back one's rows.
    """
    session = ctx.session
    account = await get_account(session, account_id)
    if account.balance_mode != "transactions":
        return
    batches = await _committed_batches(session, account_id)
    claims = await _claims(session, batches)
    flat = [claim for found in claims.values() for claim in found]
    plan = plan_openings(flat, await _movements(session, account_id))
    live = await _live_adjustments(session, account_id)
    days = {(c.batch_id, c.instrument_id): c.adjustment_day for c in flat}
    zones = {batch.id: batch.timezone for batch in batches}
    default_tz = await get_timezone(session)
    r = models.ImportRow
    changed = False
    for key in sorted(plan.keys() | live.keys(), key=str):
        batch_id, instrument_id = key
        need = plan.get(key, Decimal(0))
        row = live.get(key)
        current = row.quantity if row is not None else Decimal(0)
        if current == need:
            continue
        changed = True
        if row is not None and row.transaction_id is not None:
            await void_transaction(ctx, row.transaction_id)
        if need == 0:
            assert row is not None
            await session.execute(
                update(r).where(r.id == row.id).values(status="rolled_back", note=SUPERSEDED)
            )
            continue
        zone = zones.get(batch_id)
        local_tz = ZoneInfo(zone) if zone else default_tz
        day = days[key]
        currency = _iso_code(await get_instrument(session, instrument_id))
        transaction_id = await _post_adjustment(ctx, account_id, instrument_id, need, day, local_tz)
        values: dict[str, Any] = {
            "quantity": need,
            "booking_date": day,
            "occurred_at": _midnight(day, local_tz),
            "time_unit": "day",
            "occurred_tz": local_tz.key,
            "transaction_id": transaction_id,
            "dedupe_key": _opening_key(account_id, batch_id, day, need, currency),
        }
        if row is not None:
            await session.execute(update(r).where(r.id == row.id).values(**values))
            continue
        lowest = await session.scalar(select(func.min(r.position)).where(r.batch_id == batch_id))
        await session.execute(
            insert(r).values(
                batch_id=batch_id,
                account_id=account_id,
                position=min(lowest or 0, 0) - 1,
                kind="opening_adjustment",
                status="committed",
                description=OPENING_DESCRIPTION,
                instrument_id=instrument_id,
                raw={},
                **values,
            )
        )
    if changed:
        ctx.emit("finance.import.openings_aligned", account_id, {"account_id": str(account_id)})


def _midnight(day: date, local_tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time(), tzinfo=local_tz)


# Checks ---------------------------------------------------------------------------------


def require_importable(account: AccountRecord) -> None:
    """Decision 3: only open, manual accounts whose balance comes from transactions, and
    only assets and liabilities: a statement is a bank account's or a card's, never an
    income, expense or equity account's, nor one of the ledger's own accounts (In transit
    is an asset, but holds M4's transfers in flight)."""
    if account.system:
        raise ImportNotAllowedError(
            f"{account.name} belongs to the ledger, so it can't take imports"
        )
    if account.ledger_type not in ("asset", "liability"):
        raise ImportNotAllowedError(
            f"this is an {account.ledger_type} account; statements import into a bank, card "
            "or other asset or liability account"
        )
    if account.status != "open":
        raise ImportNotAllowedError("this account is closed, so it can't take imports")
    if account.source_kind != "manual":
        raise ImportNotAllowedError(
            "this account is synced from a connection, so it can't take imports"
        )
    if account.balance_mode == "snapshots":
        raise ImportNotAllowedError(
            "this account's balance comes from snapshots, so imported transactions wouldn't "
            "count; switch it to transactions or hybrid first"
        )


def _ending(value: str) -> str:
    return value[-4:]


async def _check_statement_account(
    session: AsyncSession, account_id: UUID, stated: StatementAccount | None
) -> None:
    """Decision 18: the file's own account must agree with every committed statement of the
    ledger account, field by field where both state it. Messages show only last digits."""
    if stated is None:
        return
    b, s = models.ImportBatch, models.ImportStatement
    earlier = await session.execute(
        select(s.sort_code, s.account_number, s.iban, s.account_reference)
        .join(b, b.id == s.batch_id)
        .where(b.account_id == account_id, b.status == "committed")
    )
    for sort_code, account_number, iban, reference in earlier:
        if stated.account_number and account_number and stated.account_number != account_number:
            raise ImportNotAllowedError(
                f"this file is for account number ending {_ending(stated.account_number)}, but "
                f"earlier imports into this account were for account number ending "
                f"{_ending(account_number)}"
            )
        if stated.iban and iban and stated.iban != iban:
            raise ImportNotAllowedError(
                f"this file is for the IBAN ending {_ending(stated.iban)}, but earlier imports "
                f"into this account were for the IBAN ending {_ending(iban)}"
            )
        if stated.sort_code and sort_code and stated.sort_code != sort_code:
            raise ImportNotAllowedError(
                "this file is for another sort code than earlier imports into this account"
            )
        if stated.account_reference and reference and stated.account_reference != reference:
            raise ImportNotAllowedError(
                f"this file is for the account ending {_ending(stated.account_reference)}, but "
                f"earlier imports into this account were for the account ending "
                f"{_ending(reference)}"
            )


# Staging --------------------------------------------------------------------------------


# A statement is decoded in full in memory, under the api container's memory cap (compose),
# which everything the process serves shares: parses in parallel threads would add up. So
# a second upload waits for the first, as it did when parsing ran on the event loop.
_PARSING = threading.Lock()


def _parse_alone(
    content: bytes, config: ProfileConfigModel, local_tz: ZoneInfo
) -> tuple[ParsedStatement, str]:
    with _PARSING:
        return _parse(content, config, local_tz)


def _parse(
    content: bytes, config: ProfileConfigModel, local_tz: ZoneInfo
) -> tuple[ParsedStatement, str]:
    match config:
        case CsvProfileConfig():
            statement, version = (
                csv_parser.parse_csv(content, config, local_tz),
                csv_parser.PARSER_VERSION,
            )
        case OfxProfileConfig():
            statement, version = ofx_parser.parse_ofx(content, config), ofx_parser.PARSER_VERSION
        case QifProfileConfig():
            statement, version = (
                qif_parser.parse_qif(content, config, local_tz),
                qif_parser.PARSER_VERSION,
            )
        case PdfProfileConfig():
            statement, version = pdf.parse_pdf(content, config, local_tz), pdf.PARSER_VERSION
    _refuse_nul(statement)
    return statement, version


def _has_nul(value: object) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        items = cast(dict[object, object], value).items()
        return any(_has_nul(key) or _has_nul(item) for key, item in items)
    if isinstance(value, list | tuple):
        return any(_has_nul(item) for item in cast(Sequence[object], value))
    if is_dataclass(value) and not isinstance(value, type):
        return any(_has_nul(getattr(value, f.name)) for f in fields(value))
    return False


def _refuse_nul(statement: ParsedStatement) -> None:
    """PostgreSQL text can't hold NUL, whichever parser read it: refuse the file here,
    naming the row, rather than fail at insert with a message that names nothing."""
    for row in statement.rows:
        if _has_nul(row):
            raise FileFormatError(
                f"row {row.position + 1} has a NUL character, which no field can hold"
            )
    if _has_nul(replace(statement, rows=[])):
        raise FileFormatError("the statement has a NUL character, which no field can hold")


@dataclass
class _Row:
    """A row being classified."""

    position: int
    status: RowStatus
    raw: dict[str, str]
    parsed: ParsedRow | None = None
    kind: RowKind = "statement"
    instrument_id: UUID | None = None
    currency: str | None = None
    dedupe_key: str | None = None
    error: str | None = None
    note: str | None = None
    # The committed row this one duplicates.
    duplicate_of: UUID | None = None
    # An error that still has a valid amount (counts in the balance chain).
    real_error: bool = False
    # New on a day another committed file lists only in part: the owner should see it.
    review: bool = False

    def fail(self, message: str, *, real: bool = False) -> None:
        # What a duplicate or skipped row carries goes with its status (import_rows' CHECK
        # allows duplicate_of only on a duplicate).
        self.status, self.error, self.real_error = "error", message, real
        self.duplicate_of = self.note = None


def _iso_code(instrument: InstrumentRecord) -> str:
    return next(i.value for i in instrument.identifiers if i.scheme == "iso4217")


async def _default_currency(session: AsyncSession, instrument_id: UUID | None) -> str | None:
    if instrument_id is None:
        return None
    instrument = await get_instrument(session, instrument_id)
    if instrument.kind != "fiat":
        raise NotACurrencyError(instrument.symbol)
    return _iso_code(instrument)


async def _resolve_currencies(
    session: AsyncSession, rows: list[_Row], default: str | None
) -> dict[UUID, InstrumentRecord]:
    """Gives each parsed row its instrument; rows without a known currency become errors."""
    found: dict[str, InstrumentRecord | None] = {}
    for row in rows:
        if row.parsed is None or row.status != "new":
            continue
        code = row.parsed.currency or default
        if code is None:
            row.fail("no currency: pick a currency at upload, or map a currency column")
            continue
        if code not in found:
            try:
                found[code] = await get_currency(session, code)
            except UnknownCurrencyError:
                found[code] = None
        instrument = found[code]
        if instrument is None:
            row.fail(
                f"unknown currency {code}: pick a currency at upload, or map a currency column"
            )
            continue
        row.instrument_id, row.currency = instrument.id, code
    return {i.id: i for i in found.values() if i is not None}


def _classify(statement: ParsedStatement) -> list[_Row]:
    rows: list[_Row] = []
    for item in statement.rows:
        match item:
            case SkippedRow():
                rows.append(_Row(item.position, "skipped", item.raw, note=item.reason))
            case RowError():
                rows.append(_Row(item.position, "error", item.raw, error=item.message))
            case ParsedRow():
                rows.append(_Row(item.position, "new", item.raw, parsed=item))
    return rows


async def _deduplicate(session: AsyncSession, account_id: UUID, rows: list[_Row]) -> None:
    """Zero rows are skipped; the rest get a key (Decision 5). A key that repeats within the
    file (an external id, or a row alike to its time of day) is an error; a key a committed
    row has is a duplicate."""
    keyed: list[tuple[_Row, ParsedRow, str]] = []
    for row in rows:
        parsed = row.parsed
        if parsed is None or row.status != "new" or row.currency is None:
            continue
        if parsed.quantity == 0 and parsed.fee == 0:
            row.status, row.note = "skipped", "zero amount"
            continue
        keyed.append((row, parsed, row.currency))

    def balance_key(parsed: ParsedRow, currency: str) -> tuple[str, ...] | None:
        """The fields of the key by balance, for a row keyed so (dedupe_key)."""
        if parsed.external_id is not None or parsed.balance is None:
            return None
        return (*_same_row(parsed, currency), _plain(parsed.balance))

    # A balance can come back (a debit, its refund, the same debit). The time of day, when
    # the file gives one, tells such rows apart; it goes into their keys alone, so every key a
    # committed row can have stays as it was (such a file was refused before), and rows alike
    # to their time stay an error. Without times, each repeat takes its occurrence in the file
    # (owner ruling, 2026-11-19).
    balances = Counter(balance_key(parsed, currency) for _, parsed, currency in keyed)
    occurrences: Counter[tuple[str, ...]] = Counter()
    repeats: Counter[tuple[str, ...]] = Counter()
    for row, parsed, currency in keyed:
        bal = balance_key(parsed, currency)
        timed = bal is not None and balances[bal] > 1
        same = _same_row(parsed, currency)
        occurrence = occurrences[same]
        if bal is not None:
            occurrence = 0 if parsed.occurred_at is not None else repeats[bal]
            repeats[bal] += 1
        row.dedupe_key = dedupe_key(account_id, parsed, currency, occurrence, timed)
        occurrences[same] += 1
    await _mark_duplicates(session, account_id, [row for row in rows if row.dedupe_key])


async def _mark_duplicates(session: AsyncSession, account_id: UUID, keyed: list[_Row]) -> None:
    """A key that repeats among the rows is an error; a key a committed row has makes the row
    that row's duplicate."""
    r, b = models.ImportRow, models.ImportBatch
    committed: dict[str | None, tuple[UUID, Decimal | None, UUID | None, str]] = {
        key: (row_id, quantity, instrument_id, filename)
        for key, row_id, quantity, instrument_id, filename in await session.execute(
            select(r.dedupe_key, r.id, r.quantity, r.instrument_id, b.filename)
            .join(b, b.id == r.batch_id)
            .where(
                r.account_id == account_id,
                r.status == "committed",
                r.dedupe_key.in_([row.dedupe_key for row in keyed]),
            )
        )
    }
    seen: set[str] = set()
    for row in keyed:
        assert row.dedupe_key is not None and row.parsed is not None
        if row.dedupe_key in seen:
            # A repeat would count in the chain and the opening but never be committed
            # (ruling 2026-11-15), so it blocks the commit instead.
            external_id = row.parsed.external_id
            row.fail(
                f"the file lists transaction id {external_id} twice"
                if external_id is not None
                else "the file lists this row twice, with the same date"
                + (", time" if row.parsed.occurred_at is not None else "")
                + ", amount, description and balance"
            )
        elif row.dedupe_key in committed:
            row_id, quantity, instrument_id, filename = committed[row.dedupe_key]
            # Only an external id's key leaves the amount out; a row it matches may still
            # move on another day (a pending date), but never by another amount.
            if (quantity, instrument_id) != (row.parsed.quantity, row.instrument_id):
                row.fail(
                    f"transaction id {row.parsed.external_id} was imported from {filename} "
                    f"as {to_plain_str(quantity or Decimal(0))}; this file gives "
                    f"{to_plain_str(row.parsed.quantity)}"
                    + ("" if instrument_id == row.instrument_id else " in another currency"),
                    real=True,
                )
            else:
                row.status, row.duplicate_of = "duplicate", row_id
        seen.add(row.dedupe_key)


type _DayKey = tuple[date, UUID, Decimal]


type _Range = tuple[date | None, date | None]


@dataclass(frozen=True)
class _Coverage:
    """Decision 16: the days a file covers (its period and its rows' days, first to last),
    and among them the days it lists in full."""

    covered: _Range
    complete: _Range


@dataclass(frozen=True)
class _CommittedCoverage:
    filename: str
    coverage: _Coverage
    instruments: frozenset[UUID]  # what it has live rows in (_LIVE_ROWS): it speaks for those


async def _committed_coverage(session: AsyncSession, account_id: UUID) -> list[_CommittedCoverage]:
    """The account's committed batches' days (Decision 16), oldest commit first."""
    b, r = models.ImportBatch, models.ImportRow
    instruments: dict[UUID, set[UUID]] = defaultdict(set)
    for batch_id, instrument_id in await session.execute(
        select(r.batch_id, r.instrument_id)
        .distinct()
        .where(r.account_id == account_id, r.status.in_(_LIVE_ROWS), r.kind == "statement")
    ):
        assert instrument_id is not None  # committed rows have one (the table's CHECK)
        instruments[batch_id].add(instrument_id)
    found = await session.execute(
        select(b.id, b.filename, b.covered_from, b.covered_to, b.complete_from, b.complete_to)
        .where(b.account_id == account_id, b.status == "committed")
        .order_by(b.committed_at, b.id)
    )
    return [
        _CommittedCoverage(
            name,
            _Coverage((covered_from, covered_to), (complete_from, complete_to)),
            frozenset(instruments[batch_id]),
        )
        for batch_id, name, covered_from, covered_to, complete_from, complete_to in found
    ]


async def _match_covered_days(
    session: AsyncSession, account_id: UUID, rows: list[_Row], own: _Coverage
) -> None:
    """Decision 16: on a day this file or a committed batch covers, new rows are matched
    with that day's committed rows by (date, instrument, quantity) as a multiset. A new row
    left unmatched on a day a committed batch lists in full contradicts that file, so it is
    an error; on a day a committed batch lists only in part it is new, with a note naming
    that file for review (the file may lack it, or list it under another date or amount).
    The other direction is _missing_committed_row's."""
    b, r = models.ImportBatch, models.ImportRow
    covers: list[_Range] = [own.covered]
    committed = await _committed_coverage(session, account_id)
    covers += [batch.coverage.covered for batch in committed]

    def within(day: date, ranges: list[_Range]) -> bool:
        return any(
            start is not None and end is not None and start <= day <= end for start, end in ranges
        )

    days = {
        row.parsed.booking_date
        for row in rows
        if row.parsed is not None
        and row.status == "new"
        and within(row.parsed.booking_date, covers)
    }
    if not days:
        return
    # Each day's committed rows, as (row id, its file's name) by (day, instrument, quantity).
    available: dict[_DayKey, list[tuple[UUID, str]]] = defaultdict(list)
    for row_id, day, instrument_id, quantity, filename in await session.execute(
        select(r.id, r.booking_date, r.instrument_id, r.quantity, b.filename)
        .join(b, b.id == r.batch_id)
        .where(
            r.account_id == account_id,
            r.status == "committed",
            r.kind == "statement",
            r.booking_date.in_(days),
        )
        .order_by(r.booking_date, r.position)
    ):
        # Committed rows have all three (the table's CHECK).
        assert day is not None and instrument_id is not None and quantity is not None
        available[(day, instrument_id, quantity)].append((row_id, filename))

    def key(row: _Row) -> _DayKey:
        assert row.parsed is not None and row.instrument_id is not None
        return row.parsed.booking_date, row.instrument_id, row.parsed.quantity

    # A row whose key matched a committed row has already claimed that row, wherever it is
    # (an external id's match may be on another day).
    claimed = {row.duplicate_of for row in rows if row.duplicate_of is not None}
    for pool in available.values():
        pool[:] = [entry for entry in pool if entry[0] not in claimed]
    for row in rows:
        if row.parsed is None or row.status != "new" or row.parsed.booking_date not in days:
            continue
        matches = available.get(key(row))
        if matches:
            row_id, filename = matches.pop(0)
            row.status, row.duplicate_of = "duplicate", row_id
            row.note = f"already imported from {filename}"
            continue
        day, instrument_id = row.parsed.booking_date, row.instrument_id
        # A file speaks only for the instruments it has rows in: one currency's statement
        # says nothing about another's days.
        mine = [batch for batch in committed if instrument_id in batch.instruments]
        if within(day, [batch.coverage.complete for batch in mine]):
            row.fail(OUTSIDE_COVERED_DAY, real=True)
            continue
        names = [batch.filename for batch in mine if within(day, [batch.coverage.covered])]
        if names:
            row.note = f"not in {names[0]}, which lists {day.isoformat()} only in part"
            row.review = True


async def _missing_committed_row(
    session: AsyncSession,
    account_id: UUID,
    rows: list[_Row],
    complete: _Range,
) -> str | None:
    """Decision 16 the other way: on the days this file lists in full, every committed
    statement row must have been matched by a row of this file. Otherwise the ledger would
    contradict one of the two files; returns the message naming the first such row, or
    None."""
    start, end = complete
    if start is None or end is None:
        return None
    # It lists in full only the instruments it has live rows in, as _committed_coverage
    # and _claims count them (_LIVE_ROWS).
    instruments = {
        row.instrument_id
        for row in rows
        if row.kind == "statement" and row.status in _LIVE_ROWS and row.instrument_id is not None
    }
    matched = {row.duplicate_of for row in rows if row.duplicate_of is not None}
    r, b = models.ImportRow, models.ImportBatch
    found = await session.execute(
        select(r.id, r.booking_date, r.quantity, b.filename)
        .join(b, b.id == r.batch_id)
        .where(
            r.account_id == account_id,
            r.status == "committed",
            r.kind == "statement",
            b.status == "committed",
            r.booking_date.between(start, end),
            r.instrument_id.in_(instruments),
        )
        .order_by(r.booking_date, r.position)
    )
    for row_id, day, quantity, filename in found:
        if row_id in matched:
            continue
        assert day is not None and quantity is not None  # committed rows have them
        return (
            f"{filename} has {to_plain_str(quantity)} on {day.isoformat()}, which this file "
            f"doesn't list; if this file is right, roll {filename} back first"
        )
    return None


async def recheck_duplicates(session: AsyncSession, batch_id: UUID) -> int:
    """Commit's re-run of the duplicate and covered-day checks (Decisions 5 and 16), for
    rows other batches committed or rolled back since staging. Rows that became duplicates,
    or whose committed original is gone, are updated. Returns how many problems there now
    are, left as they are: rows that would be errors (an unmatched row on a day another file
    lists in full), rows newly on a day another file lists only in part (to be reviewed),
    plus one when a committed row on this batch's days is missing from it."""
    r, b = models.ImportRow, models.ImportBatch
    account_id = await session.scalar(select(b.account_id).where(b.id == batch_id))
    assert account_id is not None
    stored = list(
        await session.scalars(
            select(r)
            .where(
                r.batch_id == batch_id,
                r.kind == "statement",
                r.status.in_(("new", "duplicate")),
            )
            .order_by(r.position)
            .execution_options(populate_existing=True)
        )
    )
    targets = [row.duplicate_of for row in stored if row.duplicate_of is not None]
    still_committed = set(
        await session.scalars(select(r.id).where(r.id.in_(targets), r.status == "committed"))
    )
    rows: list[_Row] = []
    for row in stored:
        # Statement rows that are new or duplicate have these (the table's CHECK).
        assert row.booking_date is not None and row.quantity is not None
        keep = row.status == "duplicate" and row.duplicate_of in still_committed
        rows.append(
            _Row(
                row.position,
                "duplicate" if keep else "new",
                row.raw,
                parsed=ParsedRow(
                    position=row.position,
                    booking_date=row.booking_date,
                    occurred_at=None,
                    description=row.description,
                    quantity=row.quantity,
                    fee=row.fee,
                    currency=None,
                    balance=row.balance,
                    external_id=row.external_id,
                    bank_type=None,
                    raw={},
                ),
                instrument_id=row.instrument_id,
                dedupe_key=row.dedupe_key,
                note=row.note if keep else None,
                duplicate_of=row.duplicate_of if keep else None,
            )
        )
    await _mark_duplicates(session, account_id, [row for row in rows if row.status == "new"])
    found = await session.execute(
        select(b.covered_from, b.covered_to, b.complete_from, b.complete_to).where(b.id == batch_id)
    )
    covered_from, covered_to, complete_from, complete_to = found.one()
    own = _Coverage((covered_from, covered_to), (complete_from, complete_to))
    await _match_covered_days(session, account_id, rows, own)
    errors = 0
    if await _missing_committed_row(session, account_id, rows, own.complete):
        errors += 1
    for row, original in zip(rows, stored, strict=True):
        # A row that now overlaps a file committed since staging was never reviewed as such.
        if row.status == "error" or (row.review and row.note != original.note):
            errors += 1
            continue
        change = (row.status, row.duplicate_of, row.note)
        if change != (original.status, original.duplicate_of, original.note):
            await session.execute(
                update(r)
                .where(r.id == original.id)
                .values(status=row.status, duplicate_of=row.duplicate_of, note=row.note)
            )
    return errors


async def recheck_statement_account(session: AsyncSession, batch_id: UUID) -> None:
    """Commit's re-run of the statement-account check (Decision 18), for batches committed
    since staging: ImportNotAllowedError when the stored account disagrees with theirs."""
    b, s = models.ImportBatch, models.ImportStatement
    found = (
        await session.execute(
            select(b.account_id, s.sort_code, s.account_number, s.iban, s.account_reference)
            .join(s, s.batch_id == b.id)
            .where(b.id == batch_id)
        )
    ).one_or_none()
    if found is None:
        return
    stated = StatementAccount(
        sort_code=found.sort_code,
        account_number=found.account_number,
        iban=found.iban,
        account_reference=found.account_reference,
    )
    if stated != StatementAccount():
        await _check_statement_account(session, found.account_id, stated)


def _check_chain(rows: list[_Row], row_order: str) -> None:
    chain: list[ParsedRow | None] = []
    by_position = {row.position: row for row in rows}
    instruments: dict[int, UUID] = {}
    for row in rows:
        if row.status in ("new", "duplicate") or row.real_error:
            assert row.parsed is not None and row.instrument_id is not None
            chain.append(row.parsed)
            instruments[row.position] = row.instrument_id
        elif row.status == "error":
            chain.append(None)
    for position, message in check_balance_chain(
        chain, _by_position(instruments), row_order
    ).items():
        row = by_position[position]
        if row.status != "error":
            row.fail(message)


def _row_values(row: _Row, batch_id: UUID, account_id: UUID, local_tz: ZoneInfo) -> dict[str, Any]:
    values: dict[str, Any] = {
        "batch_id": batch_id,
        "account_id": account_id,
        "position": row.position,
        "kind": row.kind,
        "status": row.status,
        "instrument_id": row.instrument_id,
        "dedupe_key": row.dedupe_key,
        "duplicate_of": row.duplicate_of,
        "error": row.error,
        "note": row.note,
        "raw": row.raw,
    }
    parsed = row.parsed
    if parsed is None:
        return values
    counterparty = parsed.counterparty
    return values | {
        "booking_date": parsed.booking_date,
        "occurred_at": parsed.occurred_at or _midnight(parsed.booking_date, local_tz),
        "time_unit": parsed.time_unit,
        "interest": parsed.interest,
        "occurred_tz": local_tz.key,
        "description": parsed.description,
        "quantity": parsed.quantity,
        "fee": parsed.fee,
        "balance": parsed.balance,
        "external_id": parsed.external_id,
        "bank_type": parsed.bank_type,
        "value_date": parsed.value_date,
        "payee": parsed.payee,
        "memo": parsed.memo,
        "check_number": parsed.check_number,
        "counterparty_name": counterparty and counterparty.name,
        "counterparty_sort_code": counterparty and counterparty.sort_code,
        "counterparty_account_number": counterparty and counterparty.account_number,
        "counterparty_bank": counterparty and counterparty.bank,
        "counterparty_account_ending": counterparty and counterparty.account_ending,
        "payment_reference": counterparty and counterparty.reference,
        "payment_kind": parsed.payment_kind,
        "bank_reference": parsed.bank_reference,
        "purpose_code": parsed.purpose_code,
        "remittance_info": parsed.remittance_info,
        "original_currency": parsed.original_currency,
        "exchange_rate": parsed.exchange_rate,
        "started_at": parsed.started_at,
        "card_number": parsed.card_number,
        "original_amount": parsed.original_amount,
        "fx_rate": parsed.fx_rate,
        "fx_reference_rate": parsed.fx_reference_rate,
    }


def _statement_values(statement: ParsedStatement) -> dict[str, Any] | None:
    """The import_statements columns, or None when the file states nothing at that level."""
    account = statement.account or StatementAccount()
    period = statement.period
    values: dict[str, Any] = {
        "period_from": period and period[0],
        "period_to": period and period[1],
        "currency": statement.currency,
        "opening_balance": statement.opening_balance,
        "closing_balance": statement.closing_balance,
        "closing_balance_date": statement.closing_balance_date,
        "money_in": statement.money_in,
        "money_out": statement.money_out,
        "interest": statement.interest,
        "payments_in": statement.payments_in,
        "payments_out": statement.payments_out,
        "statement_reference": statement.statement_reference,
        "document_date": statement.document_date,
        "account_type": account.account_type,
        "account_name": account.name,
        "sort_code": account.sort_code,
        "account_number": account.account_number,
        "iban": account.iban,
        "bic": account.bic,
        "customer_number": account.customer_number,
        "clearing_account_number": account.clearing_account_number,
        "branch": account.branch,
        "bank": account.bank,
        "addressee": account.addressee,
        "address": account.address,
        "overdraft_limit": statement.overdraft_limit,
        "overdraft_limit_date": statement.overdraft_limit_date,
        "available_balance": statement.available_balance,
        "available_balance_date": statement.available_balance_date,
        "account_reference": account.account_reference,
        "account_opened_on": statement.account_opened_on,
    }
    assert set(values) == set(_STATEMENT_FIELDS)
    if all(value is None for value in values.values()) and not statement.rates:
        return None
    return values


def _covered(statement: ParsedStatement, rows: list[_Row]) -> _Coverage:
    """Decision 16: the days a file covers, its stated period and its importable rows' days
    together (OFX rows may fall outside DTSTART..DTEND), and the days it lists in full.

    A statement whose source vouches for its period (a closed, self-checked PDF statement)
    lists all of it, up to the day before it was made: one made on or before its period's
    last day (Revolut's, which end on the day they are generated) was made partway through
    that day, so lists neither it nor later days in full. An export may be taken partway
    through a day, so it lists in full only the days strictly between its first and last;
    and one whose include_only filter left rows out (a pending payment, say, that later
    appears completed) lists no day in full.
    """
    days = [
        row.parsed.booking_date
        for row in rows
        if row.kind == "statement"
        and row.parsed is not None
        and row.status != "skipped"
        and row.instrument_id is not None
    ]
    if statement.period is not None:
        days += list(statement.period)
    if not days:
        return _Coverage((None, None), (None, None))
    first, last = min(days), max(days)
    if statement.period_complete:
        made = statement.document_date
        end = last if made is None or made > last else made - timedelta(days=1)
        if end < first:
            return _Coverage((first, last), (None, None))
        return _Coverage((first, last), (first, end))
    filtered = any(row.status == "skipped" and row.parsed is None for row in rows)
    start, end = first + timedelta(days=1), last - timedelta(days=1)
    if filtered or start > end:
        return _Coverage((first, last), (None, None))
    return _Coverage((first, last), (start, end))


async def _stage_openings(
    session: AsyncSession,
    account_id: UUID,
    batch_id: UUID,
    rows: list[_Row],
    instruments: dict[UUID, InstrumentRecord],
    local_tz: ZoneInfo,
) -> None:
    """Decision 13: the staged batch's adjustment rows and warnings, from the walk with it
    included. Its statement rows must be stored already."""
    preview = await preview_openings(session, batch_id)
    if preview.conflicts:
        raise ImportNotAllowedError(preview.conflicts[0])
    stated = {row.instrument_id for row in rows if row.status in ("new", "duplicate")}
    warnings = [NO_BALANCES_WARNING] if stated - preview.needed.keys() else []
    warnings += [
        f"committing this changes the opening adjustment of {change.filename} from "
        f"{to_plain_str(change.current)} to {to_plain_str(change.needed)}"
        for change in preview.changes
    ]
    adjustments: list[_Row] = []
    for instrument_id in sorted(preview.needed, key=lambda i: instruments[i].symbol):
        need = preview.needed[instrument_id]
        if need == 0:
            continue
        day = preview.days[instrument_id]
        currency = _iso_code(instruments[instrument_id])
        position = -(len(adjustments) + 1)
        adjustment = ParsedRow(
            position=position,
            booking_date=day,
            occurred_at=None,
            description=OPENING_DESCRIPTION,
            quantity=need,
            fee=Decimal(0),
            currency=currency,
            balance=None,
            external_id=None,
            bank_type=None,
            raw={},
        )
        adjustments.append(
            _Row(
                position,
                "new",
                {},
                parsed=adjustment,
                kind="opening_adjustment",
                instrument_id=instrument_id,
                currency=currency,
                dedupe_key=_opening_key(account_id, batch_id, day, need, currency),
            )
        )
    if adjustments:
        await session.execute(
            insert(models.ImportRow),
            [_row_values(row, batch_id, account_id, local_tz) for row in adjustments],
        )
    if warnings:
        b = models.ImportBatch
        await session.execute(update(b).where(b.id == batch_id).values(warnings=warnings))


async def stage_import(
    ctx: CommandContext,
    store: DocumentStore,
    *,
    account_id: UUID,
    profile_id: UUID,
    content: bytes,
    filename: str,
    media_type: str,
    default_instrument_id: UUID | None,
) -> ImportBatchRecord:
    """Parse a file into a staged batch for review. FileFormatError (the file can't be read
    as a whole) propagates, and the command rolls back."""
    session = ctx.session
    account = await get_account(session, account_id)
    require_importable(account)
    default_currency = await _default_currency(session, default_instrument_id)
    profile = await get_profile(session, profile_id)
    zone = profile.config.timezone
    local_tz = ZoneInfo(zone) if zone is not None else await get_timezone(session)
    # Parsing is pure CPU work (a long PDF takes seconds): off the event loop, so the other
    # requests this process serves meanwhile aren't held up; one at a time (_parse_alone).
    statement, parser_version = await asyncio.to_thread(
        _parse_alone, content, profile.config, local_tz
    )
    await _check_statement_account(session, account_id, statement.account)
    row_order = (
        profile.config.row_order if isinstance(profile.config, CsvProfileConfig) else "oldest_first"
    )

    rows = _classify(statement)
    instruments = await _resolve_currencies(session, rows, default_currency)
    await _deduplicate(session, account_id, rows)
    coverage = _covered(statement, rows)
    await _match_covered_days(session, account_id, rows, coverage)
    # Before the chain check: a matched row it fails is still listed by this file.
    missing = await _missing_committed_row(session, account_id, rows, coverage.complete)
    if missing is not None:
        raise ImportNotAllowedError(missing)
    _check_chain(rows, row_order)

    document = await record_document(
        ctx, content, filename=filename, media_type=media_type, source="finance.import"
    )
    b = models.ImportBatch
    batch_id = await session.scalar(
        insert(b)
        .values(
            account_id=account_id,
            profile_id=profile.id,
            profile_version=profile.version,
            profile_config=profile.config.model_dump(mode="json"),
            parser_version=parser_version,
            document_id=document.id,
            filename=filename,
            default_instrument_id=default_instrument_id,
            covered_from=coverage.covered[0],
            covered_to=coverage.covered[1],
            complete_from=coverage.complete[0],
            complete_to=coverage.complete[1],
            owner_entity_id=await get_self_entity_id(session),
        )
        .returning(b.id)
    )
    assert batch_id is not None
    statement_values = _statement_values(statement)
    if statement_values is not None:
        await session.execute(
            insert(models.ImportStatement).values(batch_id=batch_id, **statement_values)
        )
        if statement.rates:
            await session.execute(
                insert(models.ImportStatementRate),
                [
                    {
                        "batch_id": batch_id,
                        "position": position,
                        "kind": rate.kind,
                        "band_from": rate.band_from,
                        "band_to": rate.band_to,
                        "rate": rate.rate,
                        "ear": rate.ear,
                        "valid_from": rate.valid_from,
                        "valid_to": rate.valid_to,
                        "frequency": rate.frequency,
                    }
                    for position, rate in enumerate(statement.rates)
                ],
            )
    await session.execute(
        insert(models.ImportRow),
        [_row_values(row, batch_id, account_id, local_tz) for row in rows],
    )
    if account.balance_mode == "transactions" and all(row.status != "error" for row in rows):
        await _stage_openings(session, account_id, batch_id, rows, instruments, local_tz)
    # The bytes last, once nothing can refuse the file: a refused one leaves no bank
    # statement on disk (or in the backups).
    store.put(document.sha256, content)

    record = await get_batch(session, batch_id)
    counts: dict[str, JSONValue] = {status: n for status, n in record.counts.items()}
    ctx.emit("finance.import.staged", batch_id, {"batch_id": str(batch_id), "counts": counts})
    ctx.record_change(
        f"import_batch:{batch_id}",
        {
            "account_id": str(account_id),
            "profile_id": str(profile.id),
            "document_id": str(document.id),
            "status": record.status,
            "counts": counts,
        },
    )
    return record
