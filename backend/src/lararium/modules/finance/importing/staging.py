"""Staging: parse an uploaded statement, classify its rows and store them for review.

Nothing is posted here: commit.py turns a staged batch's new rows into transactions.
"""

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, time, timedelta
from decimal import MAX_PREC, Decimal, localcontext
from typing import Any, Literal, cast, get_args
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from lararium.core.commands import CommandContext
from lararium.core.documents import DocumentStore, store_document
from lararium.core.entities import get_self_entity_id
from lararium.core.json_types import JSONValue
from lararium.core.settings_store import get_timezone
from lararium.modules.finance import models
from lararium.modules.finance.accounts import AccountRecord, get_account
from lararium.modules.finance.errors import (
    ImportBatchNotFoundError,
    ImportNotAllowedError,
    NotACurrencyError,
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
NO_BALANCES_WARNING = "This file has no balances, so its opening balance can't be checked"
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
    description: str
    quantity: Decimal | None
    fee: Decimal | None
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
    payment_reference: str | None
    payment_kind: str | None
    bank_reference: str | None
    purpose_code: str | None
    remittance_info: str | None
    error: str | None
    note: str | None
    raw: dict[str, str]
    transaction_id: UUID | None
    duplicate_of: UUID | None  # the committed row this one duplicates (Decision 16)


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
    overdraft_limit: Decimal | None
    overdraft_limit_date: date | None
    available_balance: Decimal | None
    available_balance_date: date | None
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


def dedupe_key(account_id: UUID, row: ParsedRow, currency: str, occurrence: int) -> str:
    """Decision 5: by external id, else by running balance, else by occurrence in the file."""
    account = str(account_id)
    if row.external_id is not None:
        return _hash("ext", account, row.external_id)
    common = (row.booking_date.isoformat(), _plain(row.quantity), currency, _text(row.description))
    if row.balance is not None:
        return _hash("bal", account, *common, _plain(row.balance))
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
    closing: tuple[Decimal, date] | None,
    row_order: str,
) -> dict[UUID, tuple[Decimal, date]]:
    """Decision 13: the statement's opening balance and first date per instrument.

    From the chronologically first row with a balance, else from the closing balance, which
    a file states for its one currency, so it is used only when every row is in one
    instrument and it is dated on or after the last row. Instruments with neither are absent.
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
                and len(chains) == 1
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


def _row_record(row: models.ImportRow) -> ImportRowRecord:
    values = {f.name: getattr(row, f.name) for f in fields(ImportRowRecord)}
    return ImportRowRecord(**values)


async def list_batch_rows(session: AsyncSession, batch_id: UUID) -> list[ImportRowRecord]:
    """The batch's rows by position (opening adjustments first)."""
    r = models.ImportRow
    rows = await session.scalars(
        select(r)
        .where(r.batch_id == batch_id)
        .order_by(r.position)
        .execution_options(populate_existing=True)
    )
    return [_row_record(row) for row in rows]


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
        select(q.batch_id, q.kind, q.band_from, q.band_to, q.rate, q.ear)
        .where(q.batch_id.in_(ids))
        .order_by(q.batch_id, q.position)
    ):
        rates[rate.batch_id].append(
            StatementRate(rate.kind, rate.band_from, rate.band_to, rate.rate, rate.ear)
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
    """A batch's statement opening balance in one instrument, which must hold at first_day."""

    batch_id: UUID
    instrument_id: UUID
    opening: Decimal
    first_day: date
    committed_at: datetime | None  # None: the staged batch being previewed, after its ties


type Movements = dict[UUID, list[tuple[date, Decimal]]]


def plan_openings(
    claims: Sequence[OpeningClaim], movements: Movements
) -> dict[tuple[UUID, UUID], Decimal]:
    """The walk: (batch, instrument) -> the adjustment that makes each opening hold.

    Claims are taken by first date (ties by commit time). Each needs its opening minus the
    account's movements (its postings other than opening adjustments) before its first date,
    minus the adjustments assigned to the claims before it.
    """

    def order(claim: OpeningClaim) -> tuple[date, bool, datetime, UUID]:
        staged = claim.committed_at is None
        return (
            claim.first_day,
            staged,
            claim.committed_at or datetime.min.replace(tzinfo=UTC),
            claim.batch_id,
        )

    assigned: dict[UUID, Decimal] = defaultdict(Decimal)
    needed: dict[tuple[UUID, UUID], Decimal] = {}
    with localcontext(prec=MAX_PREC):
        for claim in sorted(claims, key=order):
            before = sum(
                (
                    amount
                    for day, amount in movements.get(claim.instrument_id, [])
                    if day < claim.first_day
                ),
                Decimal(0),
            )
            need = claim.opening - before - assigned[claim.instrument_id]
            needed[(claim.batch_id, claim.instrument_id)] = need
            assigned[claim.instrument_id] += need
    return needed


@dataclass(frozen=True)
class _BatchInfo:
    id: UUID
    filename: str
    committed_at: datetime | None
    timezone: str | None


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
    closings = {
        batch_id: (balance, day)
        for batch_id, balance, day in await session.execute(
            select(s.batch_id, s.closing_balance, s.closing_balance_date).where(s.batch_id.in_(ids))
        )
        if balance is not None and day is not None
    }
    stored: dict[UUID, list[models.ImportRow]] = defaultdict(list)
    for row in await session.scalars(
        select(r)
        .where(r.batch_id.in_(ids), r.kind == "statement", r.status.in_(_LIVE_ROWS))
        .order_by(r.batch_id, r.position)
    ):
        stored[row.batch_id].append(row)
    claims: dict[UUID, list[OpeningClaim]] = {}
    for batch in batches:
        instruments: dict[int, UUID] = {}
        rows: list[ParsedRow] = []
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
        claims[batch.id] = [
            OpeningClaim(batch.id, instrument_id, opening, first_day, batch.committed_at)
            for instrument_id, (opening, first_day) in openings.items()
        ]
    return claims


async def _committed_batches(session: AsyncSession, account_id: UUID) -> list[_BatchInfo]:
    b = models.ImportBatch
    found = await session.execute(
        select(b.id, b.filename, b.committed_at, b.profile_config["timezone"].astext).where(
            b.account_id == account_id, b.status == "committed"
        )
    )
    return [_BatchInfo(*row) for row in found]


async def _movements(session: AsyncSession, account_id: UUID) -> Movements:
    """The account's non-void postings by instrument and day, without opening adjustments."""
    p, t, r = models.Posting, models.Transaction, models.ImportRow
    adjustments = select(r.transaction_id).where(
        r.kind == "opening_adjustment", r.transaction_id.is_not(None)
    )
    movements: Movements = defaultdict(list)
    for instrument_id, day, amount in await session.execute(
        select(p.instrument_id, t.booking_date, func.sum(p.quantity))
        .join(t, t.id == p.transaction_id)
        .where(p.account_id == account_id, t.status != "void", t.id.not_in(adjustments))
        .group_by(p.instrument_id, t.booking_date)
    ):
        movements[instrument_id].append((day, amount))
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
    changes: list[OpeningChange]


async def preview_openings(session: AsyncSession, batch_id: UUID) -> OpeningPreview:
    """Decision 13's walk as if the staged batch were committed now: its new rows count as
    movements. Staging adds its adjustment rows from this; commit compares it with them.

    Only transactions accounts take adjustments: for any other mode, nothing is needed.
    """
    b, r = models.ImportBatch, models.ImportRow
    staged = (await session.execute(select(b.account_id, b.filename).where(b.id == batch_id))).one()
    account_id = staged.account_id
    if (await get_account(session, account_id)).balance_mode != "transactions":
        return OpeningPreview(needed={}, changes=[])
    batches = [
        *(batch for batch in await _committed_batches(session, account_id) if batch.id != batch_id),
        _BatchInfo(batch_id, staged.filename, None, None),
    ]
    claims = await _claims(session, batches)
    movements = await _movements(session, account_id)
    with localcontext(prec=MAX_PREC):
        for instrument_id, day, quantity, fee in await session.execute(
            select(r.instrument_id, r.booking_date, r.quantity, r.fee).where(
                r.batch_id == batch_id, r.kind == "statement", r.status == "new"
            )
        ):
            assert instrument_id is not None and day is not None and quantity is not None
            movements[instrument_id].append((day, quantity - fee))
    plan = plan_openings([claim for found in claims.values() for claim in found], movements)
    live = await _live_adjustments(session, account_id)
    names = {batch.id: batch.filename for batch in batches}
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
    return OpeningPreview(needed=needed, changes=changes)


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
    first_days = {(c.batch_id, c.instrument_id): c.first_day for c in flat}
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
        day = first_days[key] - timedelta(days=1)
        currency = _iso_code(await get_instrument(session, instrument_id))
        transaction_id = await _post_adjustment(ctx, account_id, instrument_id, need, day, local_tz)
        values: dict[str, Any] = {
            "quantity": need,
            "booking_date": day,
            "occurred_at": _midnight(day, local_tz),
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
    """Decision 3: only open, manual accounts whose balance comes from transactions."""
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
        select(s.sort_code, s.account_number, s.iban)
        .join(b, b.id == s.batch_id)
        .where(b.account_id == account_id, b.status == "committed")
    )
    for sort_code, account_number, iban in earlier:
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


# Staging --------------------------------------------------------------------------------


def _parse(
    content: bytes, config: ProfileConfigModel, local_tz: ZoneInfo
) -> tuple[ParsedStatement, str]:
    match config:
        case CsvProfileConfig():
            return csv_parser.parse_csv(content, config, local_tz), csv_parser.PARSER_VERSION
        case OfxProfileConfig():
            return ofx_parser.parse_ofx(content, config), ofx_parser.PARSER_VERSION
        case QifProfileConfig():
            return qif_parser.parse_qif(content, config, local_tz), qif_parser.PARSER_VERSION
        case PdfProfileConfig():
            return pdf.parse_pdf(content, config, local_tz), pdf.PARSER_VERSION


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

    def fail(self, message: str, *, real: bool = False) -> None:
        self.status, self.error, self.real_error = "error", message, real


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
    file is an error; a key a committed row has is a duplicate."""
    occurrences: Counter[tuple[str, ...]] = Counter()
    for row in rows:
        parsed = row.parsed
        if parsed is None or row.status != "new" or row.currency is None:
            continue
        if parsed.quantity == 0 and parsed.fee == 0:
            row.status, row.note = "skipped", "zero amount"
            continue
        same = (
            parsed.booking_date.isoformat(),
            _plain(parsed.quantity),
            row.currency,
            _text(parsed.description),
        )
        row.dedupe_key = dedupe_key(account_id, parsed, row.currency, occurrences[same])
        occurrences[same] += 1
    await _mark_duplicates(session, account_id, [row for row in rows if row.dedupe_key])


async def _mark_duplicates(session: AsyncSession, account_id: UUID, keyed: list[_Row]) -> None:
    """A key that repeats among the rows is an error; a key a committed row has makes the row
    that row's duplicate."""
    r = models.ImportRow
    committed: dict[str | None, UUID] = {
        key: row_id
        for key, row_id in await session.execute(
            select(r.dedupe_key, r.id).where(
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
                else "the file lists this row twice, with the same date, amount, description "
                "and balance"
            )
        elif row.dedupe_key in committed:
            row.status, row.duplicate_of = "duplicate", committed[row.dedupe_key]
        seen.add(row.dedupe_key)


type _DayKey = tuple[date, UUID, Decimal]


async def _match_covered_days(session: AsyncSession, account_id: UUID, rows: list[_Row]) -> None:
    """Decision 16: new rows on days a committed batch covers must be among that day's
    committed rows, matched by (date, instrument, quantity) as a multiset."""
    b, r = models.ImportBatch, models.ImportRow
    ranges = [
        (start, end)
        for start, end in await session.execute(
            select(b.covered_from, b.covered_to).where(
                b.account_id == account_id, b.status == "committed"
            )
        )
        if start is not None and end is not None
    ]

    def covered(day: date) -> bool:
        return any(start <= day <= end for start, end in ranges)

    days = {
        row.parsed.booking_date
        for row in rows
        if row.parsed is not None and row.status == "new" and covered(row.parsed.booking_date)
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
        else:
            row.fail(OUTSIDE_COVERED_DAY, real=True)


async def recheck_duplicates(session: AsyncSession, batch_id: UUID) -> int:
    """Commit's re-run of the duplicate and covered-day checks (Decisions 5 and 16), for
    rows other batches committed or rolled back since staging. Rows that became duplicates,
    or whose committed original is gone, are updated. Returns how many rows would now be
    errors (an uncovered row on a covered day); those are left as they are."""
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
    await _match_covered_days(session, account_id, rows)
    errors = 0
    for row, original in zip(rows, stored, strict=True):
        if row.status == "error":
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
            select(b.account_id, s.sort_code, s.account_number, s.iban)
            .join(s, s.batch_id == b.id)
            .where(b.id == batch_id)
        )
    ).one_or_none()
    if found is None:
        return
    stated = StatementAccount(
        sort_code=found.sort_code, account_number=found.account_number, iban=found.iban
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
        "payment_reference": counterparty and counterparty.reference,
        "payment_kind": parsed.payment_kind,
        "bank_reference": parsed.bank_reference,
        "purpose_code": parsed.purpose_code,
        "remittance_info": parsed.remittance_info,
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
        "overdraft_limit": statement.overdraft_limit,
        "overdraft_limit_date": statement.overdraft_limit_date,
        "available_balance": statement.available_balance,
        "available_balance_date": statement.available_balance_date,
    }
    assert set(values) == set(_STATEMENT_FIELDS)
    if all(value is None for value in values.values()) and not statement.rates:
        return None
    return values


def _covered(statement: ParsedStatement, rows: list[_Row]) -> tuple[date | None, date | None]:
    """Decision 16: the statement period, else the first to last day of the importable rows."""
    if statement.period is not None:
        return statement.period
    days = [
        row.parsed.booking_date
        for row in rows
        if row.kind == "statement"
        and row.parsed is not None
        and row.status != "skipped"
        and row.instrument_id is not None
    ]
    return (min(days), max(days)) if days else (None, None)


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
        day = min(
            row.parsed.booking_date
            for row in rows
            if row.parsed is not None
            and row.instrument_id == instrument_id
            and row.status in ("new", "duplicate")
        ) - timedelta(days=1)
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
    document = await store_document(
        ctx, store, content, filename=filename, media_type=media_type, source="finance.import"
    )
    zone = profile.config.timezone
    local_tz = ZoneInfo(zone) if zone is not None else await get_timezone(session)
    statement, parser_version = _parse(content, profile.config, local_tz)
    await _check_statement_account(session, account_id, statement.account)
    row_order = (
        profile.config.row_order if isinstance(profile.config, CsvProfileConfig) else "oldest_first"
    )

    rows = _classify(statement)
    instruments = await _resolve_currencies(session, rows, default_currency)
    await _deduplicate(session, account_id, rows)
    await _match_covered_days(session, account_id, rows)
    _check_chain(rows, row_order)
    covered_from, covered_to = _covered(statement, rows)

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
            covered_from=covered_from,
            covered_to=covered_to,
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
