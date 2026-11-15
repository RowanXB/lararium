"""Matching: linking a transfer's sending and receiving records, opening a transfer for an
identified record, and keeping placeholders and settlements in step with the records they
mirror."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Select,
    and_,
    delete,
    exists,
    func,
    insert,
    or_,
    select,
    union,
    update,
)
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
    TransactionOrigin,
    TransactionStatus,
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
    OwnRecord,
    bound_id,
    eligible_records,
    is_active_leg,
    is_rejected,
    load_own_record,
    record_columns,
)
from lararium.modules.finance.transfers.queries import (
    ReviewReason,
    TransferRecord,
    TransferStatus,
    before_history,
    load_transfers,
    loose_matches,
    transfer_change_data,
    transfer_matches,
)
from lararium.modules.finance.transfers.settings import TransferSettings, load_transfer_settings

logger = logging.getLogger(__name__)

# Serialises matching, so two syncs of different connections can't both link one transfer.
_LOCK_KEY = "finance.transfers"
_MAX_HEAL_PASSES = 5

type _SystemIds = Mapping[SystemAccountKey, UUID]


@dataclass(frozen=True)
class _System:
    unassigned: UUID
    in_transit: UUID


async def _lock(ctx: CommandContext) -> None:
    await ctx.session.execute(select(func.pg_advisory_xact_lock(func.hashtext(_LOCK_KEY))))


async def _system(ctx: CommandContext) -> _System:
    return _System(
        unassigned=await get_or_create_system_account(ctx, "unassigned"),
        in_transit=await get_or_create_system_account(ctx, "in_transit"),
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


async def _any(session: AsyncSession, query: Select[*tuple[Any, ...]]) -> bool:
    return bool(await session.scalar(select(query.exists())))


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


async def _set_contra(ctx: CommandContext, record: OwnRecord, account_id: UUID) -> bool:
    """Re-point the record's contra; False when its postings changed under us (a concurrent
    upsert), in which case the heal after that upsert matches it again."""
    if record.contra_account_id == account_id:
        return True
    if await repoint_contra(ctx, record.id, record.contra_account_id, account_id):
        return True
    logger.warning("transaction %s changed while it was matched; left for the next heal", record.id)
    return False


async def _registry_counterpart(session: AsyncSession, record: OwnRecord) -> UUID | None:
    """The other own account the record's counterparty bank account is registered to."""
    if record.counterparty_identifier_id is None:
        return None
    holder = await identifier_account(session, record.counterparty_identifier_id)
    return None if holder == record.account_id else holder


def _accounts(record: OwnRecord, counterpart_id: UUID) -> tuple[UUID, UUID]:
    """(from, to) of a transfer between the record's account and the counterpart."""
    if record.role == "out":
        return record.account_id, counterpart_id
    return counterpart_id, record.account_id


async def _link(ctx: CommandContext, transfer_id: UUID, record: OwnRecord) -> UUID:
    """Make the record the transfer's missing side: matched, with its placeholder or
    settlement gone. Returns the transfer's other own record."""
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
    other = before.in_transaction_id if record.role == "out" else before.out_transaction_id
    assert other is not None  # an open transfer has the other side
    return other


async def _link_records(ctx: CommandContext, record: OwnRecord, partner: OwnRecord) -> None:
    """A matched transfer of two own records neither of which was a leg."""
    sent, received = (record, partner) if record.role == "out" else (partner, record)
    tr = models.Transfer
    transfer_id = await ctx.session.scalar(
        insert(tr)
        .values(
            from_account_id=sent.account_id,
            to_account_id=received.account_id,
            instrument_id=record.instrument_id,
            amount=record.amount,
            status="matched",
            out_transaction_id=sent.id,
            in_transaction_id=received.id,
            opened_at=min(sent.occurred_at, received.occurred_at),
            matched_at=func.now(),
        )
        .returning(tr.id)
    )
    assert transfer_id is not None
    _publish(
        ctx, "finance.transfer.matched", transfer_id, None, await _get(ctx.session, transfer_id)
    )


async def _post_mirror(
    ctx: CommandContext,
    record: OwnRecord,
    system: _System,
    *,
    account_id: UUID,
    description: str,
    status: TransactionStatus,
    origin: TransactionOrigin,
) -> UUID:
    """Post the amount the record moved through In transit between In transit and
    account_id, dated as the record, so In transit nets to zero for it."""
    derived = await post_transaction(
        ctx,
        TransactionInput(
            occurred_at=record.occurred_at,
            booking_date=record.booking_date,
            description=description,
            status=status,
            origin=origin,
            postings=[
                PostingInput(account_id, record.instrument_id, -record.quantity),
                PostingInput(system.in_transit, record.instrument_id, record.quantity),
            ],
        ),
    )
    return derived.id


async def _post_placeholder(
    ctx: CommandContext, record: OwnRecord, counterpart: AccountRecord, system: _System
) -> UUID:
    """Post the manual counterpart's side as the record's mirror image, through In transit."""
    own_name = (await get_account(ctx.session, record.account_id)).name
    return await _post_mirror(
        ctx,
        record,
        system,
        account_id=counterpart.id,
        description=(
            f"Transfer from {own_name}" if record.role == "out" else f"Transfer to {own_name}"
        ),
        status="inferred",
        origin="inference",
    )


async def _post_settlement(
    ctx: CommandContext, record: OwnRecord, counterpart: AccountRecord, system: _System
) -> UUID:
    """Settle a counterpart that predates its account's history against Opening balances,
    which that account's opening balance already contains it through."""
    return await _post_mirror(
        ctx,
        record,
        system,
        account_id=await get_or_create_system_account(ctx, "opening_balances"),
        description=f"Transfer before {counterpart.name}'s history",
        status="booked",
        origin="rule",
    )


async def _open(
    ctx: CommandContext,
    record: OwnRecord,
    counterpart: AccountRecord,
    system: _System,
    settings: TransferSettings,
    review_reason: ReviewReason | None,
) -> None:
    """Open the record's own transfer: inferred with a placeholder when the counterpart is
    manual; else before_history with a settlement when the counterpart would predate that
    account's history, or awaiting the counterpart's own record."""
    session = ctx.session
    placeholder_id: UUID | None = None
    settlement_id: UUID | None = None
    status: TransferStatus = "awaiting"
    if counterpart.source_kind == "manual":
        placeholder_id = await _post_placeholder(ctx, record, counterpart, system)
        status = "inferred"
    elif await session.scalar(
        select(before_history(record.columns(), bound_id(counterpart.id), settings))
    ):
        settlement_id = await _post_settlement(ctx, record, counterpart, system)
        status = "before_history"
    from_id, to_id = _accounts(record, counterpart.id)
    tr = models.Transfer
    transfer_id = await session.scalar(
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
            settlement_transaction_id=settlement_id,
            opened_at=record.occurred_at,
        )
        .returning(tr.id)
    )
    assert transfer_id is not None
    _publish(ctx, "finance.transfer.opened", transfer_id, None, await _get(session, transfer_id))


async def _match_identified(
    ctx: CommandContext,
    ids: _SystemIds,
    record: OwnRecord,
    counterpart_id: UUID,
    settings: TransferSettings,
    system: _System,
) -> set[UUID]:
    """Decision 6 step 3: the record names a bank account of another own account. Returns
    the own record it was linked with, if any."""
    session = ctx.session
    counterpart = await get_account(session, counterpart_id)
    r, cp = record.columns(), bound_id(counterpart_id)
    open_transfers = (
        await session.execute(transfer_matches(ids, r, settings, counterpart_id=cp, exact=True))
    ).all()
    loose = (await session.execute(loose_matches(ids, r, cp, settings, exact=True))).all()
    if not await _set_contra(ctx, record, system.in_transit):
        return set()
    if len(open_transfers) + len(loose) == 1:
        if open_transfers:
            return {await _link(ctx, open_transfers[0].transfer_id, record)}
        partner = await load_own_record(session, ids, loose[0].transaction_id)
        if partner is not None and await _set_contra(ctx, partner, system.in_transit):
            await _link_records(ctx, record, partner)
            return {partner.id}
    review_reason: ReviewReason | None = None
    if len(open_transfers) + len(loose) >= 2:
        # No choice may depend on which side synced first, so it needs review.
        review_reason = "several_candidates"
    elif await _any(
        session, transfer_matches(ids, r, settings, counterpart_id=cp, exact=False)
    ) or await _any(session, loose_matches(ids, r, cp, settings, exact=False)):
        review_reason = "amount_differs"
    await _open(ctx, record, counterpart, system, settings, review_reason)
    return set()


@dataclass(frozen=True)
class _FallbackPlan:
    """What step 4 does with a record: link it to one transfer, or mark the transfers it
    could complete as needing review."""

    link: UUID | None
    flag: tuple[UUID, ...]  # the candidate transfers not yet marked several_candidates


async def _fallback_plan(
    session: AsyncSession, ids: _SystemIds, record: OwnRecord, settings: TransferSettings
) -> _FallbackPlan | None:
    """Decision 6 step 4 for a record no registry row identifies; None when it does not
    apply. A link needs a single candidate seen from both sides: the one transfer, whose
    other own record has this record as its only loose candidate."""
    if not record.bank_transfer:
        return None
    found = (
        await session.execute(
            transfer_matches(ids, record.columns(), settings, counterpart_id=None, exact=True)
        )
    ).all()
    if not found:
        return None
    if len(found) == 1:
        [row] = found
        present = await load_own_record(session, ids, row.transaction_id)
        if present is not None:
            loose = loose_matches(
                ids, present.columns(), bound_id(record.account_id), settings, exact=True
            )
            if list(await session.scalars(loose)) == [record.id]:
                return _FallbackPlan(link=row.transfer_id, flag=())
    return _FallbackPlan(
        link=None,
        flag=tuple(r.transfer_id for r in found if r.review_reason != "several_candidates"),
    )


async def _flag(ctx: CommandContext, transfer_ids: tuple[UUID, ...]) -> None:
    """Mark transfers as needing review: a record could complete each of them."""
    for transfer_id in transfer_ids:
        before = await _get(ctx.session, transfer_id)
        await ctx.session.execute(
            update(models.Transfer)
            .where(models.Transfer.id == transfer_id)
            .values(review_reason="several_candidates")
        )
        after = await _get(ctx.session, transfer_id)
        _publish(ctx, "finance.transfer.review_needed", transfer_id, before, after)


async def _match_one(
    ctx: CommandContext, transaction_id: UUID, settings: TransferSettings, system: _System
) -> tuple[list[UUID], set[UUID]]:
    """Match one record from scratch; returns other records left without their transfer,
    and the own record it was linked with, if any."""
    session = ctx.session
    others = await _dissolve(ctx, transaction_id)
    ids = await system_account_ids(session)
    record = await load_own_record(session, ids, transaction_id)
    if record is None:
        return others, set()  # void, derived or not shaped like a transfer's leg
    # 1. The owner said it is not a transfer.
    if await is_rejected(session, record.source):
        await _set_contra(ctx, record, system.unassigned)
        return others, set()
    # 3. Identified through the registry.
    counterpart_id = await _registry_counterpart(session, record)
    if counterpart_id is not None:
        return others, await _match_identified(ctx, ids, record, counterpart_id, settings, system)
    # 4. Found by amount and date.
    plan = await _fallback_plan(session, ids, record, settings)
    if plan is not None and plan.link is not None:
        if not await _set_contra(ctx, record, system.in_transit):
            return others, set()
        return others, {await _link(ctx, plan.link, record)}
    # 5. Not a transfer (yet).
    await _set_contra(ctx, record, system.unassigned)
    if plan is not None and plan.flag:
        await _flag(ctx, plan.flag)
    return others, set()


async def _match(
    ctx: CommandContext, transaction_id: UUID, settings: TransferSettings, system: _System
) -> set[UUID]:
    """match_transaction under the lock; returns every record this matched afresh or
    linked, all of which then agree with the rule."""
    session = ctx.session
    seen: set[UUID] = set()
    linked: set[UUID] = set()
    queue = [transaction_id]
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        if current != transaction_id and await is_active_leg(session, current):
            continue
        others, partners = await _match_one(ctx, current, settings, system)
        queue += others
        linked |= partners
    return seen | linked


async def match_transaction(ctx: CommandContext, transaction_id: UUID) -> None:
    """Bring one record in line with the rule (Decision 6): dissolve its current transfer,
    then decide afresh. The dissolved transfer's other own record is matched too, unless
    this cascade has linked it again meanwhile."""
    t = models.Transaction
    if await ctx.session.scalar(select(t.id).where(t.id == transaction_id)) is None:
        raise TransactionNotFoundError(str(transaction_id))
    await _lock(ctx)
    settings = await load_transfer_settings(ctx.session)
    await _match(ctx, transaction_id, settings, await _system(ctx))


async def _fallback_due(
    session: AsyncSession,
    ids: _SystemIds,
    settings: TransferSettings,
    query: Select[*tuple[Any, ...]],
) -> list[tuple[UUID, datetime]]:
    """Of the (id, occurred_at) rows, those step 4 would change: link, or newly flag."""
    due: list[tuple[UUID, datetime]] = []
    for row in await session.execute(query):
        record = await load_own_record(session, ids, row.id)
        if record is None:
            continue
        plan = await _fallback_plan(session, ids, record, settings)
        if plan is not None and (plan.link is not None or plan.flag):
            due.append((row.id, row.occurred_at))
    return due


async def _records_to_heal(session: AsyncSession, settings: TransferSettings) -> list[UUID]:
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
    # (c) a bank transfer on Unassigned that an open transfer may be waiting for; which of
    # them step 4 would change is decided below, by the same code that changes them.
    fallback = select(e.c.transaction_id.label("id"), e.c.occurred_at).where(
        e.c.contra_account_id == unassigned,
        e.c.bank_transfer,
        ~is_leg,
        ~identified,
        ~rejected,
        transfer_matches(
            system, record_columns(e), settings, counterpart_id=None, exact=True
        ).exists(),
    )
    if in_transit is not None:
        # (a) in In transit without a transfer.
        queries.append(
            select(e.c.transaction_id.label("id"), e.c.occurred_at).where(
                e.c.contra_account_id == in_transit, ~is_leg
            )
        )
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
            # (d) legs that no longer fit their transfer.
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
            # (d) a lone record whose counterpart's account history now starts after the
            # counterpart's latest time, or no longer does (older history was fetched).
            predates = before_history(record_columns(e), counterpart, settings)
            queries.append(
                select(e.c.transaction_id.label("id"), e.c.occurred_at)
                .select_from(tr)
                .join(e, e.c.transaction_id == leg)
                .where(
                    other_leg.is_(None),
                    or_(
                        and_(tr.status == "before_history", ~predates),
                        and_(tr.status == "awaiting", predates),
                    ),
                )
            )
        # (d) placeholders and settlements out of step with their record: other dates,
        # void, or missing.
        mirrored, derived = aliased(t, name="mirrored"), aliased(t, name="derived")
        queries.append(
            select(mirrored.id.label("id"), mirrored.occurred_at)
            .select_from(tr)
            .join(
                mirrored,
                mirrored.id == func.coalesce(tr.out_transaction_id, tr.in_transaction_id),
            )
            .outerjoin(
                derived,
                derived.id
                == func.coalesce(tr.placeholder_transaction_id, tr.settlement_transaction_id),
            )
            .where(
                active,
                or_(
                    and_(tr.status == "inferred", tr.placeholder_transaction_id.is_(None)),
                    and_(tr.status == "before_history", tr.settlement_transaction_id.is_(None)),
                    derived.status == "void",
                    derived.occurred_at != mirrored.occurred_at,
                    derived.booking_date != mirrored.booking_date,
                ),
            )
        )
    due = union(*queries).subquery()
    rows = {
        (row.id, row.occurred_at)
        for row in await session.execute(select(due.c.id, due.c.occurred_at))
    }
    rows.update(await _fallback_due(session, system, settings, fallback))
    return [transaction_id for transaction_id, _ in sorted(rows, key=lambda r: (r[1], r[0]))]


async def heal_transfers(ctx: CommandContext) -> int:
    """Match every record whose state disagrees with the rule, until none does; returns how
    many records it matched. Right after one heal, another writes nothing."""
    await _lock(ctx)
    settings = await load_transfer_settings(ctx.session)
    matched = 0
    for _ in range(_MAX_HEAL_PASSES):
        due = await _records_to_heal(ctx.session, settings)
        if not due:
            return matched
        system = await _system(ctx)
        # A record linked earlier in this pass already agrees with the rule.
        done: set[UUID] = set()
        for transaction_id in due:
            if transaction_id in done:
                continue
            done |= await _match(ctx, transaction_id, settings, system)
            matched += 1
    logger.error("transfers still disagree with the rule after %d heal passes", _MAX_HEAL_PASSES)
    return matched
