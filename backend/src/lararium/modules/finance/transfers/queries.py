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
    bound_id,
    eligible_records,
    load_own_record,
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
    """Equal when exact, else of the same sign but different."""
    if exact:
        return given == wanted
    return and_(given != wanted, (given < 0) == (wanted < 0))


def transfer_matches(
    system: Mapping[SystemAccountKey, UUID],
    r: RecordColumns,
    settings: TransferSettings,
    *,
    counterpart_id: SQLColumnExpression[UUID] | None,
    exact: bool,
) -> Select[*tuple[Any, ...]]:
    """Open transfers the record could complete (candidate (i), and the fallback's when
    `counterpart_id` is None): missing the record's side on the record's account, with the
    same instrument and, if exact, amount (else a different one), whose own record fits the
    window and was not decided never to pair with it. Columns: transfer_id, review_reason
    and the present own record's transaction_id, account_id, quantity, occurred_at,
    description; oldest transfer first."""
    tr = models.Transfer
    present = eligible_records(system, "present")
    described = aliased(models.Transaction, name="described")
    sends = r.quantity < 0
    conditions: list[ColumnElement[bool]] = [
        or_(
            and_(sends, tr.out_transaction_id.is_(None), tr.from_account_id == r.account_id),
            and_(~sends, tr.in_transaction_id.is_(None), tr.to_account_id == r.account_id),
        ),
        tr.instrument_id == r.instrument_id,
        _amount_fits(tr.amount, func.abs(r.quantity), exact),
        tr.status.in_(OPEN_STATUSES),
        present.c.occurred_at.between(*counterpart_window(r, settings)),
        ~_never_paired(r, present.c.source_kind, present.c.source_ref),
    ]
    if counterpart_id is not None:
        conditions.append(
            case((sends, tr.to_account_id), else_=tr.from_account_id) == counterpart_id
        )
    return (
        select(
            tr.id.label("transfer_id"),
            tr.review_reason,
            present.c.transaction_id,
            present.c.account_id,
            present.c.quantity,
            present.c.occurred_at,
            described.description,
        )
        .join(
            present,
            present.c.transaction_id == func.coalesce(tr.out_transaction_id, tr.in_transaction_id),
        )
        .join(described, described.id == present.c.transaction_id)
        .where(*conditions)
        .order_by(tr.opened_at, tr.id)
    )


def loose_matches(
    system: Mapping[SystemAccountKey, UUID],
    r: RecordColumns,
    counterpart_id: SQLColumnExpression[UUID],
    settings: TransferSettings,
    *,
    exact: bool,
) -> Select[*tuple[Any, ...]]:
    """Candidate (ii): own records on the counterpart account that are no transfer's leg,
    of the opposite sign, the same instrument and, if exact, amount (else a different one),
    fitting the window, neither rejected nor decided never to pair with the record, and
    either a bank transfer or naming the record's account (and no third one). Columns:
    transaction_id, account_id, quantity, occurred_at, description; oldest first."""
    loose = eligible_records(system, "loose")
    described = aliased(models.Transaction, name="described")
    named = aliased(models.AccountIdentifier, name="named")
    tr = aliased(models.Transfer, name="linked")
    d = aliased(models.TransferDecision, name="rejected")
    holder = named.account_id
    return (
        select(
            loose.c.transaction_id,
            loose.c.account_id,
            loose.c.quantity,
            loose.c.occurred_at,
            described.description,
        )
        .join(described, described.id == loose.c.transaction_id)
        .outerjoin(named, named.id == loose.c.counterparty_identifier_id)
        .where(
            loose.c.account_id == counterpart_id,
            loose.c.instrument_id == r.instrument_id,
            _amount_fits(loose.c.quantity, -r.quantity, exact),
            loose.c.occurred_at.between(*counterpart_window(r, settings)),
            ~exists().where(
                tr.status != "rejected",
                or_(
                    tr.out_transaction_id == loose.c.transaction_id,
                    tr.in_transaction_id == loose.c.transaction_id,
                ),
            ),
            ~exists().where(
                d.kind == "reject",
                d.subject_kind == loose.c.source_kind,
                d.subject_ref == loose.c.source_ref,
            ),
            ~_never_paired(r, loose.c.source_kind, loose.c.source_ref),
            or_(loose.c.bank_transfer, holder == r.account_id),
            # One naming a third own account is that account's transfer.
            or_(holder.is_(None), holder == loose.c.account_id, holder == r.account_id),
        )
        .order_by(loose.c.occurred_at, loose.c.transaction_id)
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
    review: those of the same amount first, then those whose amount differs, each oldest
    first. Empty for a transfer that has both sides (or none it could be found by)."""
    transfer = await get_transfer(session, transfer_id)
    if transfer is None:
        return []
    present_id = transfer.out_transaction_id or transfer.in_transaction_id
    if present_id is None or transfer.status not in OPEN_STATUSES:
        return []
    system = await system_account_ids(session)
    present = await load_own_record(session, system, present_id)
    if present is None:
        return []
    counterpart = (
        transfer.to_account_id
        if present_id == transfer.out_transaction_id
        else transfer.from_account_id
    )
    settings = await load_transfer_settings(session)
    r, counterpart_id = present.columns(), bound_id(counterpart)
    found: list[CandidateRecord] = []
    for exact in (True, False):
        rows = [
            *await session.execute(
                transfer_matches(system, r, settings, counterpart_id=counterpart_id, exact=exact)
            ),
            *await session.execute(loose_matches(system, r, counterpart_id, settings, exact=exact)),
        ]
        rows.sort(key=lambda row: (row.occurred_at, row.transaction_id))
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
