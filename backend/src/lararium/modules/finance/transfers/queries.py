"""Reading transfers, and the invariant check."""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import ColumnElement, and_, exists, or_, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.json_types import JSONValue
from lararium.modules.finance import models
from lararium.modules.finance.money import to_plain_str
from lararium.modules.finance.system_accounts import system_account_ids
from lararium.modules.finance.transfers.contra import DERIVED_ORIGINS, eligible_records

# What the ledger holds for the transfer: one own record waiting for the connected other
# side's (awaiting), one plus a placeholder on the manual other side (inferred), both
# (matched), one whose other side predates that account's history (before_history), or
# the owner's word that it is not a transfer (rejected).
TransferStatus = Literal["awaiting", "inferred", "matched", "before_history", "rejected"]
# Why the rule could not decide, apart from the status ("needs review").
ReviewReason = Literal["several_candidates", "amount_differs"]


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


async def find_transfer_violations(session: AsyncSession) -> list[UUID]:
    """Ids of transactions that break the invariant (design §5.5): own records whose contra
    is In transit without being a leg of a transfer that is not rejected, or the reverse;
    placeholders and settlements whose transfer is gone; and those of a transfer that is not
    rejected which are void or don't post on In transit."""
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
    found = union(misplaced, orphaned, broken).subquery()
    return list(await session.scalars(select(found.c.id).order_by(found.c.id)))
