"""Matching: linking a transfer's sending and receiving records, opening a transfer for an
identified record, and keeping placeholders and settlements in step with the records they
mirror."""

import logging
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    DateTime,
    Numeric,
    String,
    Uuid,
    and_,
    delete,
    exists,
    func,
    insert,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession

from lararium.core.commands import CommandContext
from lararium.modules.finance import models
from lararium.modules.finance.accounts import AccountRecord, get_account
from lararium.modules.finance.errors import TransactionNotFoundError
from lararium.modules.finance.identifiers import (
    IdentifierRecord,
    identifier_account,
    unassign_identifier,
)
from lararium.modules.finance.ledger import (
    CounterpartyFields,
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
    RecordColumns,
    bound_id,
    is_active_leg,
    is_rejected,
    load_own_record,
)
from lararium.modules.finance.transfers.heal import records_to_heal
from lararium.modules.finance.transfers.queries import (
    OPEN_STATUSES,
    ReviewReason,
    TransferRecord,
    TransferStatus,
    before_history,
    load_transfers,
    pair_candidates,
    tiered_pairs,
    transfer_change_data,
    unposted_pairs,
)
from lararium.modules.finance.transfers.settings import TransferSettings, load_transfer_settings

logger = logging.getLogger(__name__)

# Serialises matching, so two syncs of different connections can't both link one transfer.
_LOCK_KEY = "finance.transfers"
_MAX_HEAL_PASSES = 5

type _SystemIds = Mapping[SystemAccountKey, UUID]
# The transfers a matching run dissolved, by each of their own records: one reopened for the
# same record carries its notified_at over, so the owner isn't told twice (Decision 9).
type _Dissolved = dict[UUID, TransferRecord]


@dataclass(frozen=True)
class _System:
    unassigned: UUID
    in_transit: UUID


async def lock_transfers(ctx: CommandContext) -> None:
    """Take the matching lock for the rest of the transaction (re-entrant)."""
    await ctx.session.execute(select(func.pg_advisory_xact_lock(func.hashtext(_LOCK_KEY))))


async def _system(ctx: CommandContext) -> _System:
    return _System(
        unassigned=await get_or_create_system_account(ctx, "unassigned"),
        in_transit=await get_or_create_system_account(ctx, "in_transit"),
    )


def publish_transfer(
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


async def _dissolve(ctx: CommandContext, transaction_id: UUID, dissolved: _Dissolved) -> list[UUID]:
    """Delete the transaction's transfer (unless rejected) with its placeholder and
    settlement, noting it in `dissolved`; returns the transfer's other own record, which
    needs matching again."""
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
        publish_transfer(ctx, "finance.transfer.dissolved", transfer.id, transfer, None)
        for leg in (transfer.out_transaction_id, transfer.in_transaction_id):
            if leg is not None:
                dissolved[leg] = transfer
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


async def _link(ctx: CommandContext, transfer_id: UUID, record: OwnRecord) -> None:
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
    publish_transfer(
        ctx, "finance.transfer.matched", transfer_id, before, await _get(session, transfer_id)
    )


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
    publish_transfer(
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
    previous: TransferRecord | None,
) -> None:
    """Open the record's own transfer: inferred with a placeholder when the counterpart is
    manual; else before_history with a settlement when the counterpart would predate that
    account's history, or awaiting the counterpart's own record.

    `previous`: the record's transfer this matching dissolved, if any. When the owner was
    told about it and the new one is the same situation (the same accounts and amount, and
    needing review or not alike), the new one counts as told too."""
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
    notified_at = None
    if previous is not None and (
        previous.from_account_id,
        previous.to_account_id,
        previous.instrument_id,
        previous.amount,
        previous.review_reason is None,
    ) == (from_id, to_id, record.instrument_id, record.amount, review_reason is None):
        notified_at = previous.notified_at
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
            notified_at=notified_at,
        )
        .returning(tr.id)
    )
    assert transfer_id is not None
    publish_transfer(
        ctx, "finance.transfer.opened", transfer_id, None, await _get(session, transfer_id)
    )


async def _pair_candidates(
    session: AsyncSession,
    ids: _SystemIds,
    settings: TransferSettings,
    transaction_id: UUID,
    *,
    exact: bool,
    excluding: Collection[UUID] = (),
) -> list[UUID]:
    """The own records the record could pair with (queries.pair_candidates)."""
    pairs = pair_candidates(ids, settings, exact=exact, excluding=excluding).subquery()
    return list(
        await session.scalars(
            select(pairs.c.candidate_id)
            .where(pairs.c.record_id == transaction_id)
            .order_by(pairs.c.candidate_id)
        )
    )


async def _waiting_transfer(session: AsyncSession, record_id: UUID) -> TransferRecord | None:
    """The open transfer the record is the only own record of, if any."""
    tr = models.Transfer
    found = await load_transfers(
        session,
        tr.status.in_(OPEN_STATUSES),
        or_(
            and_(tr.out_transaction_id == record_id, tr.in_transaction_id.is_(None)),
            and_(tr.in_transaction_id == record_id, tr.out_transaction_id.is_(None)),
        ),
    )
    return found[0] if found else None


async def _link_pair(
    ctx: CommandContext,
    ids: _SystemIds,
    record: OwnRecord,
    partner_id: UUID,
    system: _System,
    dissolved: _Dissolved,
) -> tuple[list[UUID], set[UUID]]:
    """Link the record with its unique partner: into the partner's open transfer when it
    has one between the same accounts (so that transfer's placeholder or settlement goes),
    else as a new matched transfer after dissolving whatever the partner was in. Returns
    the records left without their transfer, and the partner."""
    session = ctx.session
    partner = await load_own_record(session, ids, partner_id)
    assert partner is not None  # pair_candidates found it eligible
    others: list[UUID] = []
    waiting = await _waiting_transfer(session, partner.id)
    if waiting is None or (waiting.from_account_id, waiting.to_account_id) != _accounts(
        record, partner.account_id
    ):
        others = await _dissolve(ctx, partner.id, dissolved)
        waiting = None
    if not await _set_contra(ctx, partner, system.in_transit):
        return others, set()
    if not await _set_contra(ctx, record, system.in_transit):
        return others, set()
    if waiting is not None:
        await _link(ctx, waiting.id, record)
    else:
        await _link_records(ctx, record, partner)
    return others, {partner.id}


async def _match_one(
    ctx: CommandContext,
    transaction_id: UUID,
    settings: TransferSettings,
    system: _System,
    excluding: Collection[UUID],
    dissolved: _Dissolved,
) -> tuple[list[UUID], set[UUID]]:
    """Match one record from scratch; returns other records left without their transfer,
    and the own record it was linked with, if any.

    Order independence: a record is linked only with a partner it pairs with uniquely, and
    which pairs uniquely with it (queries.pair_candidates both ways). That outcome, and the
    review reason, depend on the records alone, never on which arrived first."""
    session = ctx.session
    others = await _dissolve(ctx, transaction_id, dissolved)
    ids = await system_account_ids(session)
    record = await load_own_record(session, ids, transaction_id)
    if record is None:
        return others, set()  # void, derived or not shaped like a transfer's leg
    # 1. The owner said it is not a transfer.
    if await is_rejected(session, record.source):
        await _set_contra(ctx, record, system.unassigned)
        return others, set()
    # 2, 3 and 4. Paired by the owner, by the registry, or by amount and date: link a
    # unique pair.
    found = await _pair_candidates(
        session, ids, settings, record.id, exact=True, excluding=excluding
    )
    if len(found) == 1 and await _pair_candidates(
        session, ids, settings, found[0], exact=True, excluding=excluding
    ) == [record.id]:
        more, linked = await _link_pair(ctx, ids, record, found[0], system, dissolved)
        return others + more, linked
    counterpart_id = await _registry_counterpart(session, record)
    if counterpart_id is None:
        # 5. Not a transfer, or not one yet: a candidate's own transfer carries the review.
        await _set_contra(ctx, record, system.unassigned)
        return others, set()
    # 3. Identified, with no unique partner: its own transfer, for review if it has any.
    if not await _set_contra(ctx, record, system.in_transit):
        return others, set()
    review_reason: ReviewReason | None = None
    if found:
        review_reason = "several_candidates"
    elif await _pair_candidates(
        session, ids, settings, record.id, exact=False, excluding=excluding
    ):
        review_reason = "amount_differs"
    counterpart = await get_account(session, counterpart_id)
    await _open(ctx, record, counterpart, system, settings, review_reason, dissolved.get(record.id))
    return others, set()


async def _match(
    ctx: CommandContext,
    transaction_id: UUID,
    settings: TransferSettings,
    system: _System,
    dissolved: _Dissolved,
    excluding: Collection[UUID] = (),
) -> set[UUID]:
    """match_transaction under the lock; returns every record this matched afresh or
    linked, all of which then agree with the rule. Records in `excluding` are treated as
    gone: never matched, and never anyone's candidate."""
    session = ctx.session
    seen: set[UUID] = set()
    linked: set[UUID] = set()
    queue = [transaction_id]
    while queue:
        current = queue.pop(0)
        if current in seen or current in excluding:
            continue
        seen.add(current)
        if current != transaction_id and await is_active_leg(session, current):
            continue
        others, partners = await _match_one(ctx, current, settings, system, excluding, dissolved)
        queue += others
        linked |= partners
    return seen | linked


async def match_transaction(ctx: CommandContext, transaction_id: UUID) -> None:
    """Bring one record in line with the rule (Decision 6): dissolve its current transfer,
    then decide afresh. The dissolved transfer's other own record is matched too, unless
    this cascade has linked it again meanwhile. Other records whose review reason this
    record changes are left to heal_transfers, which every caller runs afterwards."""
    t = models.Transaction
    if await ctx.session.scalar(select(t.id).where(t.id == transaction_id)) is None:
        raise TransactionNotFoundError(str(transaction_id))
    await lock_transfers(ctx)
    settings = await load_transfer_settings(ctx.session)
    await _match(ctx, transaction_id, settings, await _system(ctx), {})


async def heal_transfers(ctx: CommandContext) -> int:
    """Match every record whose state disagrees with the rule, until none does; returns how
    many records it matched. Right after one heal, another writes nothing."""
    await lock_transfers(ctx)
    settings = await load_transfer_settings(ctx.session)
    matched = 0
    dissolved: _Dissolved = {}
    for _ in range(_MAX_HEAL_PASSES):
        due = await records_to_heal(ctx.session, settings)
        if not due:
            return matched
        system = await _system(ctx)
        # A record linked earlier in this pass already agrees with the rule.
        done: set[UUID] = set()
        for transaction_id in due:
            if transaction_id in done:
                continue
            done |= await _match(ctx, transaction_id, settings, system, dissolved)
            matched += 1
    logger.error("transfers still disagree with the rule after %d heal passes", _MAX_HEAL_PASSES)
    return matched


async def _release_rejected(ctx: CommandContext, transaction_id: UUID) -> None:
    """Take the transaction out of a rejected transfer: the transfer keeps its other own
    record, or goes when it has none. The reject decisions stay, keyed by reference."""
    session = ctx.session
    tr = models.Transfer
    for transfer in await load_transfers(
        session,
        tr.status == "rejected",
        or_(tr.out_transaction_id == transaction_id, tr.in_transaction_id == transaction_id),
    ):
        if transfer.out_transaction_id == transaction_id:
            side, other = "out_transaction_id", transfer.in_transaction_id
        else:
            side, other = "in_transaction_id", transfer.out_transaction_id
        if other is None:
            await session.execute(delete(tr).where(tr.id == transfer.id))
            publish_transfer(ctx, "finance.transfer.dissolved", transfer.id, transfer, None)
        else:
            await session.execute(update(tr).where(tr.id == transfer.id).values({side: None}))
            publish_transfer(
                ctx,
                "finance.transfer.unlinked",
                transfer.id,
                transfer,
                await _get(session, transfer.id),
            )


async def unlink_transaction(ctx: CommandContext, transaction_id: UUID) -> None:
    """Make a row about to be deleted (an import being rolled back) no transfer's leg: its
    transfer is dissolved, its contra goes back to Unassigned, and the transfer's other own
    record is matched again as if the row were gone, so a placeholder is posted again where
    one is due. The caller deletes the row next; heal_transfers after that settles the
    rest (it would link the row again if it ran before the delete)."""
    session = ctx.session
    t = models.Transaction
    if await session.scalar(select(t.id).where(t.id == transaction_id)) is None:
        raise TransactionNotFoundError(str(transaction_id))
    await lock_transfers(ctx)
    dissolved: _Dissolved = {}
    others = await _dissolve(ctx, transaction_id, dissolved)
    await _release_rejected(ctx, transaction_id)
    system = await _system(ctx)
    record = await load_own_record(session, await system_account_ids(session), transaction_id)
    if record is not None:
        await _set_contra(ctx, record, system.unassigned)
    settings = await load_transfer_settings(session)
    for other in others:
        await _match(ctx, other, settings, system, dissolved, excluding={transaction_id})


async def preview_match(
    session: AsyncSession,
    *,
    account_id: UUID,
    instrument_id: UUID,
    quantity: Decimal,
    occurred_at: datetime,
    counterparty: CounterpartyFields,
) -> TransferRecord | None:
    """The open transfer a row not yet posted would complete if it were posted now
    (Decision 6 steps 3 and 4, without writing): None when it would complete none, or
    could complete more than one. It reads the same pair relation matching does, with the
    row added: its one candidate in its strongest tier, which has no other candidate as
    strong."""
    system = await system_account_ids(session)
    settings = await load_transfer_settings(session)
    holder = (
        None
        if counterparty.identifier_id is None
        else await identifier_account(session, counterparty.identifier_id)
    )
    row = RecordColumns(
        transaction_id=literal(None, Uuid()),  # not posted yet
        account_id=bound_id(account_id),
        instrument_id=bound_id(instrument_id),
        quantity=literal(quantity, Numeric),
        occurred_at=literal(occurred_at, DateTime(timezone=True)),
        source_kind=literal(None, String),
        source_ref=literal(None, String),
    )
    found = unposted_pairs(
        system,
        settings,
        row,
        holder=literal(holder, Uuid()),
        bank_transfer=literal(counterparty.bank_transfer, Boolean),
    ).subquery()
    tiers = {r.candidate_id: r.tier for r in await session.execute(select(found))}
    if not tiers:
        return None
    best = min(tiers.values())
    partners = [candidate for candidate, tier in tiers.items() if tier == best]
    if len(partners) != 1:
        return None
    [partner] = partners
    pairs = tiered_pairs(system, settings).subquery()
    if await session.scalar(
        select(exists().where(pairs.c.record_id == partner, pairs.c.tier <= best))
    ):
        return None  # the row would make the partner's pair ambiguous, or lose to another
    return await _waiting_transfer(session, partner)


async def unassign_identifier_and_rematch(
    ctx: CommandContext, identifier_id: UUID
) -> IdentifierRecord:
    """unassign_identifier, then match again every record naming that bank account, which
    no longer identifies one of the owner's accounts."""
    record = await unassign_identifier(ctx, identifier_id)
    await lock_transfers(ctx)
    t = models.Transaction
    naming = await ctx.session.scalars(
        select(t.id)
        .where(t.counterparty_identifier_id == identifier_id)
        .order_by(t.occurred_at, t.id)
    )
    for transaction_id in list(naming):
        await match_transaction(ctx, transaction_id)
    await heal_transfers(ctx)
    return record
