"""Reading transfers, the candidates a record could pair with, and the invariant check."""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    Select,
    SQLColumnExpression,
    and_,
    case,
    exists,
    func,
    or_,
    select,
    union,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from lararium.core.json_types import JSONValue
from lararium.modules.finance import models
from lararium.modules.finance.money import to_plain_str
from lararium.modules.finance.system_accounts import SystemAccountKey, system_account_ids
from lararium.modules.finance.transfers.contra import (
    DERIVED_ORIGINS,
    RecordColumns,
    eligible_records,
    record_columns,
)
from lararium.modules.finance.transfers.settings import TransferSettings, load_transfer_settings

# What the ledger holds for the transfer: one own record waiting for the connected other
# side's (awaiting), one plus a placeholder on the manual other side (inferred), both
# (matched), one whose other side predates that account's history (before_history), or
# the owner's word that it is not a transfer (rejected).
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


async def get_transfer(session: AsyncSession, transfer_id: UUID) -> TransferRecord | None:
    found = await load_transfers(session, models.Transfer.id == transfer_id)
    return found[0] if found else None


async def list_transfers(
    session: AsyncSession, *, statuses: Collection[TransferStatus] | None = None
) -> list[TransferRecord]:
    """Transfers, newest opened_at first; only those in `statuses` when given."""
    if statuses is None:
        return await load_transfers(session)
    return await load_transfers(session, models.Transfer.status.in_(list(statuses)))


def counterpart_window(
    r: RecordColumns, settings: TransferSettings
) -> tuple[SQLColumnExpression[datetime], SQLColumnExpression[datetime]]:
    """When the record's counterpart may occur (Decision 4): the receiving side from
    `before` before to `after` after the sending side."""
    sends = r.quantity < 0
    earliest = case(
        (sends, r.occurred_at - settings.window_before),
        else_=r.occurred_at - settings.window_after,
    )
    latest = case(
        (sends, r.occurred_at + settings.window_after),
        else_=r.occurred_at + settings.window_before,
    )
    return earliest, latest


def _never_paired(r: RecordColumns, kind: SQLColumnExpression[str], ref: SQLColumnExpression[str]):
    d = aliased(models.TransferDecision, name="never_pair")
    # Correlated explicitly: r may come from a query two levels up.
    return (
        exists()
        .where(
            d.kind == "never_pair",
            or_(
                and_(
                    d.subject_kind == r.source_kind,
                    d.subject_ref == r.source_ref,
                    d.other_kind == kind,
                    d.other_ref == ref,
                ),
                and_(
                    d.subject_kind == kind,
                    d.subject_ref == ref,
                    d.other_kind == r.source_kind,
                    d.other_ref == r.source_ref,
                ),
            ),
        )
        .correlate_except(d)
    )


def _amount_fits(
    given: SQLColumnExpression[Decimal], wanted: SQLColumnExpression[Decimal], exact: bool
):
    """Equal when exact, else different."""
    return given == wanted if exact else given != wanted


def pair_candidates(
    system: Mapping[SystemAccountKey, UUID], settings: TransferSettings, *, exact: bool
) -> Select[*tuple[Any, ...]]:
    """Every pair of own records that could be one transfer's two sides, both ways round
    (record_id, candidate_id): the single definition matching, heal and review all use.

    It depends only on the records and the owner's decisions, never on which transfers
    exist, so it gives the same answer whatever synced first. Two records pair when they
    are on different accounts, in the same instrument, of opposite sign and, if exact, the
    same amount (else a different one); the receiving one occurs in the sending one's
    window; at least one is identified through the registry; each identified one names the
    other's account; each other one is a bank transfer; and neither is rejected nor
    decided never to pair with the other."""
    r_rows, c_rows = eligible_records(system, "record"), eligible_records(system, "candidate")
    r_named = aliased(models.AccountIdentifier, name="record_named")
    c_named = aliased(models.AccountIdentifier, name="candidate_named")
    r_holder, c_holder = r_named.account_id, c_named.account_id
    # Identified: its counterparty bank account is registered to another own account.
    r_identified = and_(r_holder.is_not(None), r_holder != r_rows.c.account_id)
    c_identified = and_(c_holder.is_not(None), c_holder != c_rows.c.account_id)
    r, c = record_columns(r_rows), record_columns(c_rows)
    d = aliased(models.TransferDecision, name="rejected")

    def rejected(x: RecordColumns) -> ColumnElement[bool]:
        return (
            exists()
            .where(
                d.kind == "reject",
                d.subject_kind == x.source_kind,
                d.subject_ref == x.source_ref,
            )
            .correlate_except(d)
        )

    return (
        select(
            r_rows.c.transaction_id.label("record_id"),
            c_rows.c.transaction_id.label("candidate_id"),
        )
        .select_from(r_rows)
        .join(
            c_rows,
            and_(
                c_rows.c.account_id != r_rows.c.account_id,
                c_rows.c.instrument_id == r_rows.c.instrument_id,
            ),
        )
        .outerjoin(r_named, r_named.id == r_rows.c.counterparty_identifier_id)
        .outerjoin(c_named, c_named.id == c_rows.c.counterparty_identifier_id)
        .where(
            _amount_fits(c.quantity, -r.quantity, exact),
            (c.quantity < 0) != (r.quantity < 0),
            c.occurred_at.between(*counterpart_window(r, settings)),
            or_(r_identified, c_identified),
            or_(~r_identified, r_holder == c_rows.c.account_id),
            or_(~c_identified, c_holder == r_rows.c.account_id),
            or_(r_identified, r_rows.c.bank_transfer),
            or_(c_identified, c_rows.c.bank_transfer),
            ~rejected(r),
            ~rejected(c),
            ~_never_paired(r, c.source_kind, c.source_ref),
        )
    )


def history_start(account_id: SQLColumnExpression[UUID]) -> SQLColumnExpression[datetime]:
    """When the account's history from its connection starts: its earliest non-void record
    from that connection; NULL for a manual account or one that has synced nothing."""
    t = aliased(models.Transaction, name="history")
    p = aliased(models.Posting, name="history_posting")
    a = aliased(models.Account, name="history_account")
    # Joins, not nested subqueries, so account_id correlates with the enclosing query.
    return (
        select(func.min(t.occurred_at))
        .join(a, a.source_connection_id == t.connection_id)
        .join(p, p.transaction_id == t.id)
        .where(a.id == account_id, p.account_id == account_id, t.status != "void")
        .scalar_subquery()
    )


def before_history(
    r: RecordColumns, counterpart_id: SQLColumnExpression[UUID], settings: TransferSettings
) -> ColumnElement[bool]:
    """Whether the record's counterpart would have to occur before the counterpart
    account's history starts, so that it is already inside that account's opening balance."""
    _, latest = counterpart_window(r, settings)
    return func.coalesce(latest < history_start(counterpart_id), False)


@dataclass(frozen=True)
class CandidateRecord:
    """An own record that could be the transfer's missing side."""

    transaction_id: UUID
    account_id: UUID
    quantity: Decimal  # signed, as posted on account_id
    occurred_at: datetime
    description: str
    reason: Literal["exact", "amount_differs"]


async def candidates(session: AsyncSession, transfer_id: UUID) -> list[CandidateRecord]:
    """The own records that could complete a transfer missing one side, for the owner's
    review (pair_candidates of its own record): those of the same amount first, then those
    whose amount differs, each oldest first. Empty for a transfer with both sides."""
    transfer = await get_transfer(session, transfer_id)
    if transfer is None or transfer.status not in OPEN_STATUSES:
        return []
    present_id = transfer.out_transaction_id or transfer.in_transaction_id
    if present_id is None:
        return []
    system = await system_account_ids(session)
    settings = await load_transfer_settings(session)
    e = eligible_records(system)
    t = models.Transaction
    found: list[CandidateRecord] = []
    for exact in (True, False):
        pairs = pair_candidates(system, settings, exact=exact).subquery()
        rows = await session.execute(
            select(e.c.transaction_id, e.c.account_id, e.c.quantity, e.c.occurred_at, t.description)
            .join(pairs, pairs.c.candidate_id == e.c.transaction_id)
            .join(t, t.id == e.c.transaction_id)
            .where(pairs.c.record_id == present_id)
            .order_by(e.c.occurred_at, e.c.transaction_id)
        )
        found += [
            CandidateRecord(
                transaction_id=row.transaction_id,
                account_id=row.account_id,
                quantity=row.quantity,
                occurred_at=row.occurred_at,
                description=row.description,
                reason="exact" if exact else "amount_differs",
            )
            for row in rows
        ]
    return found


async def find_transfer_violations(session: AsyncSession) -> list[UUID]:
    """Ids of transactions that break the invariant (design §5.5): own records whose contra
    is In transit without being a leg of a transfer that is not rejected, or the reverse;
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
    in_transit_contra = e.c.contra_account_id == in_transit
    misplaced = select(e.c.transaction_id.label("id")).where(
        or_(and_(in_transit_contra, ~is_leg), and_(~in_transit_contra, is_leg))
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
    found = union(misplaced, orphaned, broken, missing).subquery()
    return list(await session.scalars(select(found.c.id).order_by(found.c.id)))
