"""Reading transfers, the candidates a record could pair with, and the invariant check."""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import (
    CTE,
    ColumnElement,
    Date,
    ScalarSelect,
    Select,
    SQLColumnExpression,
    Uuid,
    all_,
    and_,
    any_,
    case,
    exists,
    func,
    or_,
    select,
    tuple_,
    union,
    update,
)
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from lararium.core.commands import CommandContext
from lararium.core.json_types import JSONValue
from lararium.modules.finance import models
from lararium.modules.finance.errors import (
    FinanceValidationError,
    TransferNotFoundError,
)
from lararium.modules.finance.identifiers import IdentifierKind, uk_bank_account_parts
from lararium.modules.finance.money import to_plain_str
from lararium.modules.finance.system_accounts import system_account_ids
from lararium.modules.finance.transfers.contra import (
    DERIVED_ORIGINS,
    RecordColumns,
    SourceRef,
    eligible_records,
    is_rejected,
    load_own_record,
    names_source,
    source_ref,
)
from lararium.modules.finance.transfers.pairs import pair_relation
from lararium.modules.finance.transfers.settings import TransferSettings, load_transfer_settings

# What the ledger holds for the transfer: one own record waiting for the connected other
# side's (awaiting), one plus a placeholder on the manual other side (inferred), both
# (matched), one whose other side predates that account's history (before_history: the
# connected account's first record, or the manual account's first snapshot), or the
# owner's word that it is not a transfer (rejected).
TransferStatus = Literal["awaiting", "inferred", "matched", "before_history", "rejected"]
# Why the rule could not decide, apart from the status ("needs review").
ReviewReason = Literal["several_candidates", "amount_differs"]
# The statuses of a transfer that still misses one own record.
OPEN_STATUSES: tuple[TransferStatus, ...] = ("awaiting", "inferred", "before_history")


@dataclass(frozen=True)
class TransferRecord:
    id: UUID
    from_account_id: UUID
    to_account_id: UUID
    instrument_id: UUID
    amount: Decimal  # positive, as sent
    status: TransferStatus
    review_reason: ReviewReason | None
    out_transaction_id: UUID | None
    in_transaction_id: UUID | None
    placeholder_transaction_id: UUID | None
    settlement_transaction_id: UUID | None
    opened_at: datetime
    matched_at: datetime | None
    notified_at: datetime | None
    created_at: datetime


async def load_transfers(
    session: AsyncSession, *conditions: ColumnElement[bool]
) -> list[TransferRecord]:
    """Transfers matching all conditions, newest opened_at first (then id)."""
    # Select columns, not ORM objects, so a stale identity map can't hide an update.
    tr = models.Transfer
    rows = await session.execute(
        select(
            tr.id,
            tr.from_account_id,
            tr.to_account_id,
            tr.instrument_id,
            tr.amount,
            tr.status,
            tr.review_reason,
            tr.out_transaction_id,
            tr.in_transaction_id,
            tr.placeholder_transaction_id,
            tr.settlement_transaction_id,
            tr.opened_at,
            tr.matched_at,
            tr.notified_at,
            tr.created_at,
        )
        .where(*conditions)
        .order_by(tr.opened_at.desc(), tr.id.desc())
    )
    return [
        TransferRecord(
            id=row.id,
            from_account_id=row.from_account_id,
            to_account_id=row.to_account_id,
            instrument_id=row.instrument_id,
            amount=row.amount,
            status=cast(TransferStatus, row.status),
            review_reason=cast(ReviewReason | None, row.review_reason),
            out_transaction_id=row.out_transaction_id,
            in_transaction_id=row.in_transaction_id,
            placeholder_transaction_id=row.placeholder_transaction_id,
            settlement_transaction_id=row.settlement_transaction_id,
            opened_at=row.opened_at,
            matched_at=row.matched_at,
            notified_at=row.notified_at,
            created_at=row.created_at,
        )
        for row in rows
    ]


def _optional_str(value: object | None) -> str | None:
    return None if value is None else str(value)


def transfer_change_data(record: TransferRecord) -> dict[str, JSONValue]:
    """The transfer as an audit row records it."""
    return {
        "id": str(record.id),
        "from_account_id": str(record.from_account_id),
        "to_account_id": str(record.to_account_id),
        "instrument_id": str(record.instrument_id),
        "amount": to_plain_str(record.amount),
        "status": record.status,
        "review_reason": record.review_reason,
        "out_transaction_id": _optional_str(record.out_transaction_id),
        "in_transaction_id": _optional_str(record.in_transaction_id),
        "placeholder_transaction_id": _optional_str(record.placeholder_transaction_id),
        "settlement_transaction_id": _optional_str(record.settlement_transaction_id),
        "opened_at": record.opened_at.isoformat(),
        "matched_at": None if record.matched_at is None else record.matched_at.isoformat(),
    }


async def get_transfer(session: AsyncSession, transfer_id: UUID) -> TransferRecord:
    found = await load_transfers(session, models.Transfer.id == transfer_id)
    if not found:
        raise TransferNotFoundError(str(transfer_id))
    return found[0]


async def list_transfers(
    session: AsyncSession, *, statuses: Collection[TransferStatus] | None = None
) -> list[TransferRecord]:
    """Transfers, newest opened_at first; only those in `statuses` when given."""
    if statuses is None:
        return await load_transfers(session)
    return await load_transfers(session, models.Transfer.status.in_(list(statuses)))


async def weak_transfers(
    session: AsyncSession, transfers: Collection[TransferRecord]
) -> frozenset[UUID]:
    """The ids of those of the transfers that a sort code alone paired (pairs.WEAK): matched,
    neither own record naming another own account (eligible_records' holder), and no pair
    decision linking them. Read from their records, not from the pair relation, since the
    Transfers page asks on every load."""
    legs = {
        t.id: (t.out_transaction_id, t.in_transaction_id)
        for t in transfers
        if t.status == "matched"
        and t.out_transaction_id is not None
        and t.in_transaction_id is not None
    }
    if not legs:
        return frozenset()
    e = eligible_records(await system_account_ids(session))
    rows = await session.execute(
        select(
            e.c.transaction_id, e.c.account_id, e.c.holder, e.c.source_kind, e.c.source_ref
        ).where(e.c.transaction_id.in_([leg for pair in legs.values() for leg in pair]))
    )
    naming_nobody: dict[UUID, SourceRef] = {
        row.transaction_id: (row.source_kind, row.source_ref)
        for row in rows
        if row.holder is None or row.holder == row.account_id
    }
    unnamed = {
        transfer_id: (naming_nobody[sent], naming_nobody[received])
        for transfer_id, (sent, received) in legs.items()
        if sent in naming_nobody and received in naming_nobody
    }
    if not unnamed:
        return frozenset()
    d = models.TransferDecision
    sources = [source for pair in unnamed.values() for source in pair]
    # Stored once, from the record whose transfer it completed: either leg can be its subject.
    decided = {
        frozenset({(row.subject_kind, row.subject_ref), (row.other_kind, row.other_ref)})
        for row in await session.execute(
            select(d.subject_kind, d.subject_ref, d.other_kind, d.other_ref).where(
                d.kind == "pair", tuple_(d.subject_kind, d.subject_ref).in_(sources)
            )
        )
    }
    return frozenset(
        transfer_id for transfer_id, pair in unnamed.items() if frozenset(pair) not in decided
    )


def counterpart_window(
    r: RecordColumns, settings: TransferSettings
) -> tuple[SQLColumnExpression[date], SQLColumnExpression[date]]:
    """The booking dates the record's counterpart may have (Decision 4;
    pairs.counterpart_bounds in Python): the receiving side's from `before` calendar days
    before to `after` after the sending side's, both included."""
    # date minus an integer is a date; minus an interval it would be a timestamp, compared
    # at the session time zone's midnight.
    before, after = settings.window_before.days, settings.window_after.days
    sends = r.quantity < 0
    earliest = case((sends, r.booking_date - before), else_=r.booking_date - after)
    latest = case((sends, r.booking_date + after), else_=r.booking_date + before)
    return earliest, latest


def _in_ids(
    id_column: SQLColumnExpression[UUID],
    source: Any,  # a table, alias or subquery
    column: SQLColumnExpression[UUID],
    *where: ColumnElement[bool],
) -> ColumnElement[bool]:
    """Whether id_column is one of `column`'s values (of `source`, where `where` holds,
    correlated with any enclosing query): those are gathered into one array first, and each
    is then looked up by index. IN (subquery), or a join, leaves the plan to the planner's
    estimates instead, and it turns quadratic when they are wrong: with statistics taken
    while a bulk insert was uncommitted, the tables look empty."""
    ids = (
        select(func.array_agg(column))
        .select_from(source)
        .where(*where)
        .correlate_except(source)
        .scalar_subquery()
    )
    # The cast makes it ANY (array), not ANY (subquery), which would be a semi-join.
    return id_column == any_(sql_cast(ids, ARRAY(Uuid)))


def history_start(
    account_id: SQLColumnExpression[UUID], opening_balances: UUID | None
) -> ScalarSelect[date]:
    """The booking date the account's history from its connection starts on: its earliest
    non-void record's from that connection, not counting one posted against Opening
    balances (the system account `opening_balances`): a connector dates its opening balance
    adjustment before the history it accounts for, which isn't history itself. NULL for a
    manual account or one that has synced nothing else. A date, as the window is
    (counterpart_window)."""
    t = models.Transaction
    p = aliased(models.Posting, name="history_posting")
    a = aliased(models.Account, name="history_account")
    connection = (
        select(a.source_connection_id)
        .where(a.id == account_id)
        .correlate_except(a)
        .scalar_subquery()
    )
    return (
        select(func.min(t.booking_date))
        .where(
            _in_ids(t.id, p, p.transaction_id, p.account_id == account_id),
            t.connection_id == connection,
            t.status != "void",
            *_not_opening(t.id, opening_balances),
        )
        .correlate_except(t)
        .scalar_subquery()
    )


def _not_opening(
    transaction_id: SQLColumnExpression[UUID], opening_balances: UUID | None
) -> list[ColumnElement[bool]]:
    """That the transaction posts nothing on Opening balances (none if it doesn't exist)."""
    if opening_balances is None:
        return []
    o = aliased(models.Posting, name="opening_posting")
    # Their ids once, in an array (see _in_ids); none, and the array is NULL.
    ids = (
        select(func.array_agg(o.transaction_id))
        .where(o.account_id == opening_balances)
        .correlate_except(o)
        .scalar_subquery()
    )
    return [func.coalesce(transaction_id != all_(sql_cast(ids, ARRAY(Uuid))), True)]


def history_starts(opening_balances: UUID | None) -> CTE:
    """Every connected account's history_start, computed once per statement: columns
    account_id, starts_on."""
    a = aliased(models.Account, name="connected")
    return (
        select(a.id.label("account_id"), history_start(a.id, opening_balances).label("starts_on"))
        .where(a.source_connection_id.is_not(None))
        .cte("history_starts")
        .prefix_with("MATERIALIZED")
    )


def before_history(
    r: RecordColumns, starts_on: SQLColumnExpression[date], settings: TransferSettings
) -> ColumnElement[bool]:
    """Whether the record's counterpart would have to be booked before the day the
    counterpart account's history starts on (`starts_on`, its history_start), so that it is
    already inside that account's opening balance. Both are dates: a counterpart booked on
    that day itself is still inside the window, and would pair."""
    _, latest = counterpart_window(r, settings)
    return func.coalesce(latest < starts_on, False)


def manual_predates(
    r: RecordColumns, counterpart_id: SQLColumnExpression[UUID], settings: TransferSettings
) -> ColumnElement[bool]:
    """Whether the manual account `counterpart_id` already holds the record's counterpart,
    in its instrument, through what its own history records, so that a placeholder there
    would count it twice (then the transfer is before_history, settled against Opening
    balances):
    - a hybrid account: the record is at or before its first snapshot. Valuation
      (v1_balances_asof) counts its postings only after the snapshot's as_of, and before
      the first one sums them from zero. Compared with valuation's own cut-off
      (occurred_at <= as_of), on the record's own time, which a placeholder carries.
    - a transactions-mode one with statement imports (M2): every day the counterpart may
      be booked on (the record's window) is no later than the last day its committed
      imports list in full. Each of those days is a statement's, listing the real rows, or
      lies before one, inside its opening or gap adjustment. A placeholder is right only
      for a side that may come after them, not imported yet.
    False for any other account: a connected one (before_history), or a transactions-mode
    manual one with no imports, whose history is all its own records."""
    s = aliased(models.BalanceSnapshot, name="opening_snapshot")
    a = aliased(models.Account, name="snapshot_account")
    first = (
        select(func.min(s.as_of))
        .join(a, a.id == s.account_id)
        .where(
            s.account_id == counterpart_id,
            s.instrument_id == r.instrument_id,
            a.balance_mode == "hybrid",
            a.source_connection_id.is_(None),
        )
        .correlate_except(s, a)
        .scalar_subquery()
    )
    b = aliased(models.ImportBatch, name="listing_import")
    row = aliased(models.ImportRow, name="listing_import_row")
    ia = aliased(models.Account, name="imported_account")
    last_listed = (
        select(func.max(b.complete_to))
        .join(row, row.batch_id == b.id)
        .join(ia, ia.id == b.account_id)
        .where(
            b.account_id == counterpart_id,
            b.status == "committed",
            ia.balance_mode == "transactions",
            row.kind == "statement",
            row.status.in_(("committed", "duplicate")),
            row.instrument_id == r.instrument_id,
        )
        .correlate_except(b, row, ia)
        .scalar_subquery()
    )
    _, latest = counterpart_window(r, settings)
    return or_(
        func.coalesce(r.occurred_at <= first, False),
        func.coalesce(latest <= last_listed, False),
    )


# Why a record could be the transfer's missing side: a candidate of the same amount, a
# mutually identified one of another, or one the owner unpaired from the transfer's record.
CandidateReason = Literal["exact", "amount_differs", "unpaired"]


@dataclass(frozen=True)
class CandidateRecord:
    """An own record that could be the transfer's missing side."""

    transaction_id: UUID
    account_id: UUID
    quantity: Decimal  # signed, as posted on account_id
    occurred_at: datetime
    description: str
    reason: CandidateReason


async def unpaired_from(session: AsyncSession, record_id: UUID) -> list[UUID]:
    """The own records the owner unpaired from this one (a never_pair decision either way
    round) that a pair decision would link with it again: on another account, in the same
    instrument, of opposite sign and the same amount, and neither rejected. The way back
    from a mistaken unpair."""
    system = await system_account_ids(session)
    e = eligible_records(system)
    record = (await session.execute(select(e).where(e.c.transaction_id == record_id))).one_or_none()
    if record is None:
        return []
    source = (record.source_kind, record.source_ref)
    d = models.TransferDecision
    if await is_rejected(session, source):
        return []
    others: set[SourceRef] = set()
    for row in await session.execute(
        select(d.subject_kind, d.subject_ref, d.other_kind, d.other_ref).where(
            d.kind == "never_pair",
            or_(
                and_(d.subject_kind == source[0], d.subject_ref == source[1]),
                and_(d.other_kind == source[0], d.other_ref == source[1]),
            ),
        )
    ):
        subject, other = (row.subject_kind, row.subject_ref), (row.other_kind, row.other_ref)
        others.add(other if subject == source else subject)
    if not others:
        return []
    t = models.Transaction
    found = await session.scalars(
        select(e.c.transaction_id)
        .where(
            e.c.transaction_id.in_(select(t.id).where(or_(*(names_source(t, x) for x in others)))),
            e.c.account_id != record.account_id,
            e.c.instrument_id == record.instrument_id,
            e.c.quantity == -record.quantity,
            ~exists().where(
                d.kind == "reject",
                d.subject_kind == e.c.source_kind,
                d.subject_ref == e.c.source_ref,
            ),
        )
        .order_by(e.c.transaction_id)
    )
    return list(found)


PAIR_CANDIDATES_LIMIT = 20


def in_matched_transfer(transaction_id: SQLColumnExpression[UUID] | UUID) -> ColumnElement[bool]:
    """Whether the transaction is a leg of a matched transfer."""
    tr = models.Transfer
    return exists().where(
        tr.status == "matched",
        or_(tr.out_transaction_id == transaction_id, tr.in_transaction_id == transaction_id),
    )


async def require_pairable(session: AsyncSession, transaction_id: UUID) -> None:
    """Refuse a record the owner can't pair now: a leg of a matched transfer (unpair it
    first) or a rejected one (undo the rejection first: a pair never silently overrides
    one). pair_records and pair_candidates both ask this, so a listed choice can be paired."""
    if await session.scalar(select(in_matched_transfer(transaction_id))):
        raise FinanceValidationError(
            f"transaction {transaction_id} is a leg of a matched transfer: unpair it first"
        )
    if await is_rejected(session, await source_ref(session, transaction_id)):
        raise FinanceValidationError(
            f"transaction {transaction_id} was rejected as a transfer: undo the rejection first"
        )


@dataclass(frozen=True)
class PairCandidateRecord:
    """An own record the owner could pair with another as one transfer."""

    transaction_id: UUID
    account_id: UUID
    quantity: Decimal  # signed, as posted on account_id
    occurred_at: datetime
    booking_date: date
    description: str


async def pair_candidates(
    session: AsyncSession, transaction_id: UUID, limit: int = PAIR_CANDIDATES_LIMIT
) -> list[PairCandidateRecord]:
    """The own records the owner could pair this one with (pair_records): on another
    account, in the same instrument, of the opposite and equal amount, and not a leg of a
    matched transfer nor rejected. One the owner unpaired from this record (a never_pair
    decision) is still listed: pair_records withdraws that decision, which is the way back
    from a mistaken unpair. Nearest booking date first (then the earlier occurred_at); no
    window applies, as for any pair decision. Refuses a record that can't be a transfer's leg,
    and one pair_records would refuse (require_pairable)."""
    await source_ref(session, transaction_id)  # an unknown id raises here
    system = await system_account_ids(session)
    subject = await load_own_record(session, system, transaction_id)
    if subject is None:
        raise FinanceValidationError(f"transaction {transaction_id} cannot be a transfer's leg")
    await require_pairable(session, transaction_id)
    e = eligible_records(system)
    t, d = models.Transaction, models.TransferDecision
    rejected = exists().where(
        d.kind == "reject", d.subject_kind == e.c.source_kind, d.subject_ref == e.c.source_ref
    )
    rows = await session.execute(
        select(
            e.c.transaction_id,
            e.c.account_id,
            e.c.quantity,
            e.c.occurred_at,
            e.c.booking_date,
            t.description,
        )
        .join(t, t.id == e.c.transaction_id)
        .where(
            e.c.transaction_id != transaction_id,
            e.c.account_id != subject.account_id,
            e.c.instrument_id == subject.instrument_id,
            e.c.quantity == -subject.quantity,
            ~in_matched_transfer(e.c.transaction_id),
            ~rejected,
        )
        .order_by(
            func.abs(e.c.booking_date - sql_cast(subject.booking_date, Date)),
            e.c.occurred_at,
            e.c.transaction_id,
        )
        .limit(limit)
    )
    return [
        PairCandidateRecord(
            transaction_id=row.transaction_id,
            account_id=row.account_id,
            quantity=row.quantity,
            occurred_at=row.occurred_at,
            booking_date=row.booking_date,
            description=row.description,
        )
        for row in rows
    ]


async def candidates(session: AsyncSession, transfer_id: UUID) -> list[CandidateRecord]:
    """The own records that could complete a transfer missing one side, for the owner's
    review: its own record's candidates in the pair relation, those of the same amount
    first, then those whose amount differs, then those the owner unpaired from it (which
    pair can link again), each oldest first. Empty for a transfer with both sides."""
    transfer = await get_transfer(session, transfer_id)
    if transfer.status not in OPEN_STATUSES:
        return []
    present_id = transfer.out_transaction_id or transfer.in_transaction_id
    if present_id is None:
        return []
    system = await system_account_ids(session)
    relation = await pair_relation(session, system, await load_transfer_settings(session))
    reasons: list[tuple[CandidateReason, list[UUID]]] = [
        ("exact", relation.candidates(present_id)),
        ("amount_differs", relation.candidates(present_id, exact=False)),
        ("unpaired", await unpaired_from(session, present_id)),
    ]
    e = eligible_records(system)
    t = models.Transaction
    found: list[CandidateRecord] = []
    for reason, ids in reasons:
        if not ids:
            continue
        rows = await session.execute(
            select(e.c.transaction_id, e.c.account_id, e.c.quantity, e.c.occurred_at, t.description)
            .join(t, t.id == e.c.transaction_id)
            .where(e.c.transaction_id.in_(ids))
            .order_by(e.c.occurred_at, e.c.transaction_id)
        )
        found += [
            CandidateRecord(
                transaction_id=row.transaction_id,
                account_id=row.account_id,
                quantity=row.quantity,
                occurred_at=row.occurred_at,
                description=row.description,
                reason=reason,
            )
            for row in rows
        ]
    return found


@dataclass(frozen=True)
class SuggestionRecord:
    """An identifier (a bank account, a chain address) transactions name that isn't
    registered to an own account nor dismissed: maybe one of the owner's."""

    identifier_id: UUID
    kind: IdentifierKind
    value: str  # as stored
    sort_code: str | None  # a UK bank account's only
    account_number: str | None
    names: tuple[str, ...]  # the distinct counterparty names it appeared under, sorted
    outgoing: int  # own records paying it
    incoming: int  # own records paid by it
    latest: date  # the latest booking date of those records


# The identifier kinds the owner can register from Suggestions; another kind (IBKR's
# `ibkr_account`) is never suggested.
SUGGESTED_KINDS: tuple[IdentifierKind, ...] = ("uk_bank_account", "chain_address")


async def suggestions(session: AsyncSession) -> list[SuggestionRecord]:
    """Unregistered, undismissed identifiers of a kind the owner can register
    (`SUGGESTED_KINDS`) that own records name, most named first, then the most recently
    named."""
    system = await system_account_ids(session)
    e = eligible_records(system)
    i, t = models.AccountIdentifier, models.Transaction
    outgoing = func.count().filter(e.c.quantity < 0)
    incoming = func.count().filter(e.c.quantity > 0)
    latest = func.max(e.c.booking_date)
    rows = await session.execute(
        select(
            i.id,
            i.kind,
            i.value,
            func.array_agg(t.counterparty_name.distinct()).label("names"),
            outgoing.label("outgoing"),
            incoming.label("incoming"),
            latest.label("latest"),
        )
        .join(e, e.c.counterparty_identifier_id == i.id)
        .join(t, t.id == e.c.transaction_id)
        .where(i.kind.in_(SUGGESTED_KINDS), i.account_id.is_(None), i.dismissed_at.is_(None))
        .group_by(i.id, i.kind, i.value)
        .order_by((outgoing + incoming).desc(), latest.desc(), i.id)
    )
    found: list[SuggestionRecord] = []
    for row in rows:
        bank = row.kind == "uk_bank_account"
        sort_code, account_number = uk_bank_account_parts(row.value) if bank else (None, None)
        found.append(
            SuggestionRecord(
                identifier_id=row.id,
                kind=row.kind,
                value=row.value,
                sort_code=sort_code,
                account_number=account_number,
                names=tuple(sorted(name for name in row.names if name)),
                outgoing=row.outgoing,
                incoming=row.incoming,
                latest=row.latest,
            )
        )
    return found


def is_overdue(transfer: TransferRecord, now: datetime, settings: TransferSettings) -> bool:
    """Whether an awaiting transfer has been in transit too long, as due_notifications
    judges it."""
    return transfer.status == "awaiting" and transfer.opened_at < now - settings.overdue


def stray_in_transit(in_transit: UUID) -> Select[*tuple[Any, ...]]:
    """(id, occurred_at) of the recorded transactions (not void, not derived) posting on In
    transit without being a leg of a transfer that is not rejected, whatever their shape:
    one with a fee posting besides is no leg, yet its posting there still counts."""
    t, p, tr = models.Transaction, models.Posting, models.Transfer
    # Set-wise on ids, then a lookup of each (_in_ids).
    legs = [
        select(leg).where(tr.status != "rejected", leg.is_not(None))
        for leg in (tr.out_transaction_id, tr.in_transaction_id)
    ]
    stray = select(p.transaction_id).where(p.account_id == in_transit).except_(*legs).subquery()
    return select(t.id.label("id"), t.occurred_at).where(
        _in_ids(t.id, stray, stray.c.transaction_id),
        t.status != "void",
        t.origin.not_in(DERIVED_ORIGINS),
    )


async def find_transfer_violations(session: AsyncSession) -> list[UUID]:
    """Ids of transactions that break the invariant (design §5.5): recorded transactions
    posting on In transit without being a leg of a transfer that is not rejected, whatever
    their shape, and own records that are such a leg with their contra elsewhere;
    placeholders and settlements whose transfer is gone; those of a transfer that is not
    rejected which are void or don't post on In transit; and the own record of a transfer
    whose placeholder or settlement is missing."""
    system = await system_account_ids(session)
    in_transit = system.get("in_transit")
    if in_transit is None:
        return []  # nothing has ever been in transit
    tr = models.Transfer
    active = tr.status != "rejected"
    e = eligible_records(system)
    is_leg = exists().where(
        active,
        or_(
            tr.out_transaction_id == e.c.transaction_id, tr.in_transaction_id == e.c.transaction_id
        ),
    )
    stray = stray_in_transit(in_transit).subquery()
    elsewhere = select(e.c.transaction_id.label("id")).where(
        e.c.contra_account_id != in_transit, is_leg
    )
    t, p = models.Transaction, models.Posting
    orphaned = select(t.id.label("id")).where(
        t.origin.in_(DERIVED_ORIGINS),
        t.status != "void",
        exists().where(p.transaction_id == t.id, p.account_id == in_transit),
        ~exists().where(
            active,
            or_(
                tr.placeholder_transaction_id == t.id,
                tr.settlement_transaction_id == t.id,
            ),
        ),
    )
    # A transfer's placeholder or settlement that no longer moves money through In transit
    # (voided, or re-posted elsewhere) leaves the transfer's own record alone in it.
    broken = (
        select(t.id.label("id"))
        .join(
            tr,
            or_(tr.placeholder_transaction_id == t.id, tr.settlement_transaction_id == t.id),
        )
        .where(
            active,
            or_(
                t.status == "void",
                ~exists().where(p.transaction_id == t.id, p.account_id == in_transit),
            ),
        )
    )
    # An inferred transfer without its placeholder, or a before_history one without its
    # settlement, leaves its own record alone in In transit.
    missing = select(func.coalesce(tr.out_transaction_id, tr.in_transaction_id).label("id")).where(
        or_(
            and_(tr.status == "inferred", tr.placeholder_transaction_id.is_(None)),
            and_(tr.status == "before_history", tr.settlement_transaction_id.is_(None)),
        )
    )
    found = union(select(stray.c.id), elsewhere, orphaned, broken, missing).subquery()
    return list(await session.scalars(select(found.c.id).order_by(found.c.id)))


async def due_notifications(session: AsyncSession, now: datetime) -> list[tuple[UUID, str]]:
    """The transfers the owner hasn't been told about yet and should be, oldest first, each
    with its message: those needing review, and awaiting ones overdue (in transit for longer
    than the `overdue` setting). One message per transfer; review comes first, as it asks
    the owner to act."""
    settings = await load_transfer_settings(session)
    tr, i = models.Transfer, models.Instrument
    sender, receiver = (
        aliased(models.Account, name="sender"),
        aliased(models.Account, name="receiver"),
    )
    rows = await session.execute(
        select(
            tr.id,
            tr.amount,
            tr.opened_at,
            tr.review_reason,
            sender.name.label("from_name"),
            receiver.name.label("to_name"),
            i.symbol,
            i.decimals,
        )
        .join(sender, sender.id == tr.from_account_id)
        .join(receiver, receiver.id == tr.to_account_id)
        .join(i, i.id == tr.instrument_id)
        .where(
            tr.notified_at.is_(None),
            tr.status != "rejected",
            or_(
                tr.review_reason.is_not(None),
                and_(tr.status == "awaiting", tr.opened_at < now - settings.overdue),
            ),
        )
        .order_by(tr.opened_at, tr.id)
    )
    due: list[tuple[UUID, str]] = []
    for row in rows:
        amount = to_plain_str(row.amount.quantize(Decimal(1).scaleb(-row.decimals)))
        what = f"A transfer of {amount} {row.symbol} from {row.from_name} to {row.to_name}"
        if row.review_reason is not None:
            due.append((row.id, f"{what} needs your review."))
        else:
            days = (now - row.opened_at).days
            due.append((row.id, f"{what} has been in transit for {days} days."))
    return due


async def mark_notified(ctx: CommandContext, transfer_ids: Collection[UUID]) -> None:
    """Record that the owner was told about these transfers (due_notifications)."""
    if not transfer_ids:
        return
    tr = models.Transfer
    marked = await ctx.session.execute(
        update(tr)
        .where(tr.id.in_(list(transfer_ids)), tr.notified_at.is_(None))
        .values(notified_at=func.now())
        .returning(tr.id, tr.notified_at)
    )
    for row in marked:
        ctx.record_change(f"transfer:{row.id}", {"notified_at": row.notified_at.isoformat()})
