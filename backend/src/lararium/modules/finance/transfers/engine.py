"""Matching: linking a transfer's sending and receiving records, opening a transfer for an
identified record, and keeping placeholders in step with the records they mirror."""

import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import and_, delete, exists, func, insert, or_, select, union, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from lararium.core.commands import CommandContext
from lararium.modules.finance import models
from lararium.modules.finance.accounts import AccountRecord, get_account
from lararium.modules.finance.errors import TransactionNotFoundError
from lararium.modules.finance.identifiers import identifier_account
from lararium.modules.finance.ledger import (
    PostingInput,
    TransactionInput,
    delete_derived_transaction,
    post_transaction,
    repoint_contra,
)
from lararium.modules.finance.system_accounts import (
    SystemAccountKey,
    get_or_create_system_account,
    system_account_ids,
)
from lararium.modules.finance.transfers.contra import (
    eligible_records,
    is_active_leg,
    is_rejected,
    source_columns,
)
from lararium.modules.finance.transfers.queries import (
    ReviewReason,
    TransferRecord,
    TransferStatus,
    load_transfers,
    transfer_change_data,
)
from lararium.modules.finance.transfers.settings import TransferSettings, load_transfer_settings

logger = logging.getLogger(__name__)

# Serialises matching, so two syncs of different connections can't both link one transfer.
_LOCK_KEY = "finance.transfers"
_MAX_HEAL_PASSES = 5
# The statuses of a transfer that still misses one own record.
_OPEN_STATUSES: tuple[TransferStatus, ...] = ("awaiting", "inferred", "before_history")


@dataclass(frozen=True)
class _Record:
    """An own record (see contra.eligible_records) with what matching needs."""

    id: UUID
    occurred_at: datetime
    booking_date: date
    account_id: UUID
    instrument_id: UUID
    quantity: Decimal
    contra_account_id: UUID
    counterparty_identifier_id: UUID | None
    bank_transfer: bool
    source: tuple[str, str]

    @property
    def role(self) -> Literal["out", "in"]:
        return "out" if self.quantity < 0 else "in"

    @property
    def amount(self) -> Decimal:
        return self.quantity.copy_abs()


@dataclass(frozen=True)
class _System:
    unassigned: UUID
    in_transit: UUID


async def _lock(ctx: CommandContext) -> None:
    await ctx.session.execute(select(func.pg_advisory_xact_lock(func.hashtext(_LOCK_KEY))))


async def _load_record(
    session: AsyncSession, system: dict[SystemAccountKey, UUID], transaction_id: UUID
) -> _Record | None:
    e = eligible_records(system)
    row = (
        await session.execute(select(e).where(e.c.transaction_id == transaction_id))
    ).one_or_none()
    if row is None:
        return None
    return _Record(
        id=row.transaction_id,
        occurred_at=row.occurred_at,
        booking_date=row.booking_date,
        account_id=row.account_id,
        instrument_id=row.instrument_id,
        quantity=row.quantity,
        contra_account_id=row.contra_account_id,
        counterparty_identifier_id=row.counterparty_identifier_id,
        bank_transfer=row.bank_transfer,
        source=(row.source_kind, row.source_ref),
    )


def _publish(
    ctx: CommandContext,
    event_type: str,
    transfer_id: UUID,
    before: TransferRecord | None,
    after: TransferRecord | None,
) -> None:
    ctx.emit(event_type, transfer_id, {"transfer_id": str(transfer_id)})
    ctx.record_change(
        f"transfer:{transfer_id}",
        {
            "before": None if before is None else transfer_change_data(before),
            "after": None if after is None else transfer_change_data(after),
        },
    )


async def _get(session: AsyncSession, transfer_id: UUID) -> TransferRecord:
    [record] = await load_transfers(session, models.Transfer.id == transfer_id)
    return record


async def _dissolve(ctx: CommandContext, transaction_id: UUID) -> list[UUID]:
    """Delete the transaction's transfer (unless rejected) with its placeholder and
    settlement; returns the transfer's other own record, which needs matching again."""
    tr = models.Transfer
    found = await load_transfers(
        ctx.session,
        tr.status != "rejected",
        or_(tr.out_transaction_id == transaction_id, tr.in_transaction_id == transaction_id),
    )
    others: list[UUID] = []
    for transfer in found:
        await ctx.session.execute(delete(tr).where(tr.id == transfer.id))
        for derived in (transfer.placeholder_transaction_id, transfer.settlement_transaction_id):
            if derived is not None:
                await delete_derived_transaction(ctx, derived)
        _publish(ctx, "finance.transfer.dissolved", transfer.id, transfer, None)
        others += [
            leg
            for leg in (transfer.out_transaction_id, transfer.in_transaction_id)
            if leg is not None and leg != transaction_id
        ]
    return others


async def _set_contra(ctx: CommandContext, record: _Record, account_id: UUID) -> bool:
    """Re-point the record's contra; False when its postings changed under us (a concurrent
    upsert), in which case the heal after that upsert matches it again."""
    if record.contra_account_id == account_id:
        return True
    if await repoint_contra(ctx, record.id, record.contra_account_id, account_id):
        return True
    logger.warning("transaction %s changed while it was matched; left for the next heal", record.id)
    return False


async def _registry_counterpart(session: AsyncSession, record: _Record) -> UUID | None:
    """The other own account the record's counterparty bank account is registered to."""
    if record.counterparty_identifier_id is None:
        return None
    holder = await identifier_account(session, record.counterparty_identifier_id)
    return None if holder == record.account_id else holder


def _accounts(record: _Record, counterpart_id: UUID) -> tuple[UUID, UUID]:
    """(from, to) of a transfer between the record's account and the counterpart."""
    if record.role == "out":
        return record.account_id, counterpart_id
    return counterpart_id, record.account_id


async def _never_paired(session: AsyncSession, a: tuple[str, str], b: tuple[str, str]) -> bool:
    d = models.TransferDecision

    def pair(x: tuple[str, str], y: tuple[str, str]):
        return and_(
            d.subject_kind == x[0], d.subject_ref == x[1], d.other_kind == y[0], d.other_ref == y[1]
        )

    return bool(
        await session.scalar(
            select(exists().where(d.kind == "never_pair", or_(pair(a, b), pair(b, a))))
        )
    )


async def _open_transfer_candidates(
    session: AsyncSession, record: _Record, counterpart_id: UUID, settings: TransferSettings
) -> list[UUID]:
    """Candidate (i): open transfers between the record's account and the counterpart that
    miss the record's side, with the same instrument and amount, whose own record fits the
    window and was not decided never to pair with this one."""
    tr = models.Transfer
    other = aliased(models.Transaction, name="other")
    from_id, to_id = _accounts(record, counterpart_id)
    if record.role == "out":
        missing, present = tr.out_transaction_id, tr.in_transaction_id
        # The other side receives: from `before` before to `after` after the record.
        earliest = record.occurred_at - settings.window_before
        latest = record.occurred_at + settings.window_after
    else:
        missing, present = tr.in_transaction_id, tr.out_transaction_id
        # The other side sends: the record is from `before` before to `after` after it.
        earliest = record.occurred_at - settings.window_after
        latest = record.occurred_at + settings.window_before
    other_kind, other_ref = source_columns(other)
    rows = await session.execute(
        select(tr.id, other_kind.label("kind"), other_ref.label("ref"))
        .join(other, other.id == present)
        .where(
            tr.from_account_id == from_id,
            tr.to_account_id == to_id,
            tr.instrument_id == record.instrument_id,
            tr.amount == record.amount,
            tr.status.in_(_OPEN_STATUSES),
            missing.is_(None),
            other.status != "void",
            other.occurred_at.between(earliest, latest),
        )
        .order_by(tr.opened_at, tr.id)
    )
    return [
        row.id
        for row in rows
        if not await _never_paired(session, record.source, (row.kind, row.ref))
    ]


async def _link(ctx: CommandContext, transfer_id: UUID, record: _Record) -> None:
    """Make the record the transfer's missing side: matched, with its placeholder or
    settlement gone."""
    session = ctx.session
    before = await _get(session, transfer_id)
    side = "out_transaction_id" if record.role == "out" else "in_transaction_id"
    await session.execute(
        update(models.Transfer)
        .where(models.Transfer.id == transfer_id)
        .values(
            {
                side: record.id,
                "placeholder_transaction_id": None,
                "settlement_transaction_id": None,
                "status": "matched",
                "review_reason": None,
                "matched_at": func.now(),
            }
        )
    )
    for derived in (before.placeholder_transaction_id, before.settlement_transaction_id):
        if derived is not None:
            await delete_derived_transaction(ctx, derived)
    _publish(ctx, "finance.transfer.matched", transfer_id, before, await _get(session, transfer_id))


async def _post_placeholder(
    ctx: CommandContext, record: _Record, counterpart: AccountRecord, system: _System
) -> UUID:
    """Post the manual counterpart's side as the record's mirror image, through In transit."""
    own_name = (await get_account(ctx.session, record.account_id)).name
    placeholder = await post_transaction(
        ctx,
        TransactionInput(
            occurred_at=record.occurred_at,
            booking_date=record.booking_date,
            description=f"Transfer from {own_name}"
            if record.role == "out"
            else f"Transfer to {own_name}",
            status="inferred",
            origin="inference",
            postings=[
                PostingInput(counterpart.id, record.instrument_id, -record.quantity),
                PostingInput(system.in_transit, record.instrument_id, record.quantity),
            ],
        ),
    )
    return placeholder.id


async def _open(
    ctx: CommandContext,
    record: _Record,
    counterpart: AccountRecord,
    system: _System,
    review_reason: ReviewReason | None,
) -> None:
    """Open the record's own transfer: inferred with a placeholder when the counterpart is
    manual, else awaiting the counterpart's own record."""
    placeholder_id: UUID | None = None
    status: TransferStatus = "awaiting"
    if counterpart.source_kind == "manual":
        placeholder_id = await _post_placeholder(ctx, record, counterpart, system)
        status = "inferred"
    from_id, to_id = _accounts(record, counterpart.id)
    tr = models.Transfer
    transfer_id = await ctx.session.scalar(
        insert(tr)
        .values(
            from_account_id=from_id,
            to_account_id=to_id,
            instrument_id=record.instrument_id,
            amount=record.amount,
            status=status,
            review_reason=review_reason,
            out_transaction_id=record.id if record.role == "out" else None,
            in_transaction_id=record.id if record.role == "in" else None,
            placeholder_transaction_id=placeholder_id,
            opened_at=record.occurred_at,
        )
        .returning(tr.id)
    )
    assert transfer_id is not None
    _publish(
        ctx, "finance.transfer.opened", transfer_id, None, await _get(ctx.session, transfer_id)
    )


async def _match_identified(
    ctx: CommandContext,
    record: _Record,
    counterpart_id: UUID,
    settings: TransferSettings,
    system: _System,
) -> None:
    """Decision 6 step 3: the record names a bank account of another own account."""
    session = ctx.session
    counterpart = await get_account(session, counterpart_id)
    candidates = await _open_transfer_candidates(session, record, counterpart_id, settings)
    if not await _set_contra(ctx, record, system.in_transit):
        return
    if len(candidates) == 1:
        await _link(ctx, candidates[0], record)
        return
    # Two or more: no choice may depend on which side synced first, so it needs review.
    review_reason: ReviewReason | None = "several_candidates" if candidates else None
    await _open(ctx, record, counterpart, system, review_reason)


async def _match_one(
    ctx: CommandContext, transaction_id: UUID, settings: TransferSettings, system: _System
) -> list[UUID]:
    """Match one record from scratch; returns other records left without their transfer."""
    session = ctx.session
    others = await _dissolve(ctx, transaction_id)
    record = await _load_record(session, await system_account_ids(session), transaction_id)
    if record is None:
        return others  # void, derived or not shaped like a transfer's leg
    # 1. The owner said it is not a transfer.
    if await is_rejected(session, record.source):
        await _set_contra(ctx, record, system.unassigned)
        return others
    # 3. Identified through the registry.
    counterpart_id = await _registry_counterpart(session, record)
    if counterpart_id is not None:
        await _match_identified(ctx, record, counterpart_id, settings, system)
        return others
    # 5. Not a transfer.
    await _set_contra(ctx, record, system.unassigned)
    return others


async def match_transaction(ctx: CommandContext, transaction_id: UUID) -> None:
    """Bring one record in line with the rule (Decision 6): dissolve its current transfer,
    then decide afresh. The dissolved transfer's other own record is matched too, unless
    this cascade has linked it again meanwhile."""
    session = ctx.session
    t = models.Transaction
    if await session.scalar(select(t.id).where(t.id == transaction_id)) is None:
        raise TransactionNotFoundError(str(transaction_id))
    await _lock(ctx)
    settings = await load_transfer_settings(session)
    system = _System(
        unassigned=await get_or_create_system_account(ctx, "unassigned"),
        in_transit=await get_or_create_system_account(ctx, "in_transit"),
    )
    seen: set[UUID] = set()
    queue = [transaction_id]
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        if current != transaction_id and await is_active_leg(session, current):
            continue
        queue += await _match_one(ctx, current, settings, system)


async def _records_to_heal(session: AsyncSession) -> list[UUID]:
    """Records whose ledger state disagrees with the rule (Decision 8), in occurrence order."""
    system = await system_account_ids(session)
    in_transit, unassigned = system.get("in_transit"), system.get("unassigned")
    if unassigned is None:
        return []  # no connector has posted anything yet
    e = eligible_records(system)
    tr, i, d, t = (
        models.Transfer,
        models.AccountIdentifier,
        models.TransferDecision,
        models.Transaction,
    )
    active = tr.status != "rejected"
    is_leg = exists().where(
        active,
        or_(
            tr.out_transaction_id == e.c.transaction_id, tr.in_transaction_id == e.c.transaction_id
        ),
    )
    identified = exists().where(
        i.id == e.c.counterparty_identifier_id,
        i.account_id.is_not(None),
        i.account_id != e.c.account_id,
    )
    rejected = exists().where(
        d.kind == "reject", d.subject_kind == e.c.source_kind, d.subject_ref == e.c.source_ref
    )
    queries = [
        # (b) identified, still on Unassigned, not rejected.
        select(e.c.transaction_id.label("id"), e.c.occurred_at).where(
            e.c.contra_account_id == unassigned, identified, ~rejected
        )
    ]
    if in_transit is not None:
        # (a) in In transit without a transfer.
        queries.append(
            select(e.c.transaction_id.label("id"), e.c.occurred_at).where(
                e.c.contra_account_id == in_transit, ~is_leg
            )
        )
        # (d) legs that no longer fit their transfer.
        for side in ("out", "in"):
            leg, other_leg = (
                (tr.out_transaction_id, tr.in_transaction_id)
                if side == "out"
                else (tr.in_transaction_id, tr.out_transaction_id)
            )
            own_account, counterpart = (
                (tr.from_account_id, tr.to_account_id)
                if side == "out"
                else (tr.to_account_id, tr.from_account_id)
            )
            quantity = -tr.amount if side == "out" else tr.amount
            queries.append(
                select(t.id.label("id"), t.occurred_at)
                .select_from(tr)
                .join(t, t.id == leg)
                .outerjoin(e, e.c.transaction_id == leg)
                .outerjoin(i, i.id == e.c.counterparty_identifier_id)
                .where(
                    active,
                    or_(
                        e.c.transaction_id.is_(None),  # void, or no longer shaped like a leg
                        e.c.account_id != own_account,
                        e.c.instrument_id != tr.instrument_id,
                        e.c.quantity != quantity,
                        e.c.contra_account_id != in_transit,
                        # A transfer with one own record stands on that record naming the
                        # other account; it no longer does once the identifier moves.
                        and_(other_leg.is_(None), i.account_id.is_distinct_from(counterpart)),
                    ),
                )
            )
        # (d) placeholders out of step with their record's dates.
        mirrored, placeholder = aliased(t, name="mirrored"), aliased(t, name="placeholder")
        queries.append(
            select(mirrored.id.label("id"), mirrored.occurred_at)
            .select_from(tr)
            .join(placeholder, placeholder.id == tr.placeholder_transaction_id)
            .join(
                mirrored,
                mirrored.id == func.coalesce(tr.out_transaction_id, tr.in_transaction_id),
            )
            .where(
                active,
                or_(
                    placeholder.occurred_at != mirrored.occurred_at,
                    placeholder.booking_date != mirrored.booking_date,
                ),
            )
        )
    due = union(*queries).subquery()
    return list(await session.scalars(select(due.c.id).order_by(due.c.occurred_at, due.c.id)))


async def heal_transfers(ctx: CommandContext) -> int:
    """Match every record whose state disagrees with the rule, until none does; returns how
    many records it matched. Right after one heal, another writes nothing."""
    await _lock(ctx)
    matched = 0
    for _ in range(_MAX_HEAL_PASSES):
        due = await _records_to_heal(ctx.session)
        if not due:
            return matched
        for transaction_id in due:
            await match_transaction(ctx, transaction_id)
        matched += len(due)
    logger.error("transfers still disagree with the rule after %d heal passes", _MAX_HEAL_PASSES)
    return matched
