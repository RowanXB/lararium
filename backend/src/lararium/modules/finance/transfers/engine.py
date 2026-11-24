"""Matching: linking a transfer's sending and receiving records, opening a transfer for an
identified record, and keeping placeholders and settlements in step with the records they
mirror."""

import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    Numeric,
    and_,
    delete,
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
    register_card_merchant,
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
    resolve_booking_date,
)
from lararium.modules.finance.system_accounts import (
    SystemAccountKey,
    get_or_create_system_account,
    system_account_ids,
)
from lararium.modules.finance.transfers.contra import (
    DERIVED_ORIGINS,
    LegFee,
    OwnRecord,
    SourceRef,
    bound_id,
    is_active_leg,
    load_own_record,
    rejection_of,
    rejoins,
)
from lararium.modules.finance.transfers.heal import records_to_heal
from lararium.modules.finance.transfers.pairs import (
    Pairable,
    PairRelation,
    conversion_postings,
    leg_fees,
    pair_relation,
)
from lararium.modules.finance.transfers.queries import (
    OPEN_STATUSES,
    ReviewReason,
    TransferRecord,
    TransferStatus,
    before_history,
    history_start,
    load_transfers,
    manual_predates,
    transfer_change_data,
)
from lararium.modules.finance.transfers.settings import TransferSettings, load_transfer_settings

logger = logging.getLogger(__name__)

# Serialises matching, so two syncs of different connections can't both link one transfer.
_LOCK_KEY = "finance.transfers"
_MAX_HEAL_PASSES = 5

type _SystemIds = Mapping[SystemAccountKey, UUID]
# The id preview_match gives a row not yet posted, which no record has.
_UNPOSTED = UUID(int=0)
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


def _registry_counterpart(record: OwnRecord) -> UUID | None:
    """The other own account the record names (contra.eligible_records' holder)."""
    return None if record.holder == record.account_id else record.holder


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
                # The earliest of its own records' times, whichever arrived first.
                "opened_at": func.least(models.Transfer.opened_at, record.occurred_at),
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


async def _link_records(
    ctx: CommandContext,
    record: OwnRecord,
    partner: OwnRecord,
    conversion: list[PostingInput] | None,
) -> None:
    """A matched transfer of two own records neither of which was a leg, in the sent
    instrument and amount. Of two in different currencies, `conversion` holds the postings
    of the conversion between them (_conversion), which is its settlement."""
    sent, received = (record, partner) if record.role == "out" else (partner, record)
    settlement_id = None if conversion is None else await _post_conversion(ctx, sent, conversion)
    tr = models.Transfer
    transfer_id = await ctx.session.scalar(
        insert(tr)
        .values(
            from_account_id=sent.account_id,
            to_account_id=received.account_id,
            instrument_id=sent.instrument_id,
            amount=sent.amount,
            status="matched",
            out_transaction_id=sent.id,
            in_transaction_id=received.id,
            settlement_transaction_id=settlement_id,
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
    which that account's opening balance (a connector's adjustment, a manual account's
    first snapshot, or its first statement import's opening adjustment) already contains
    it through."""
    return await _post_mirror(
        ctx,
        record,
        system,
        account_id=await get_or_create_system_account(ctx, "opening_balances"),
        description=f"Transfer before {counterpart.name}'s history",
        status="booked",
        origin="rule",
    )


def _conversion(
    record: OwnRecord, partner: OwnRecord, fees: Sequence[LegFee], system: _System
) -> list[PostingInput] | None:
    """The postings of the conversion between two own records in different currencies
    (pairs.conversion_postings), as loaded now; None when they no longer fit one (both in
    one currency, or the fee not less than the amount: changed since the pair relation
    took them)."""
    sent, received = (record, partner) if record.role == "out" else (partner, record)
    if sent.instrument_id == received.instrument_id:
        return None
    return conversion_postings(
        (sent.instrument_id, sent.amount),
        (received.instrument_id, received.amount),
        fees,
        in_transit=system.in_transit,
        unassigned=system.unassigned,
    )


async def _post_conversion(
    ctx: CommandContext, sending: OwnRecord, postings: list[PostingInput]
) -> UUID:
    """Post a transfer's conversion between two currencies (eToro design §5.12,
    _conversion) on In transit, dated as the sending record, booked by rule: derived, like
    a placeholder, so it goes when the transfer does and is posted again when it is
    matched again."""
    derived = await post_transaction(
        ctx,
        TransactionInput(
            occurred_at=sending.occurred_at,
            booking_date=sending.booking_date,
            description="Currency conversion",
            status="booked",
            origin="rule",
            postings=postings,
        ),
    )
    return derived.id


async def _open(
    ctx: CommandContext,
    record: OwnRecord,
    counterpart: AccountRecord,
    system: _System,
    settings: TransferSettings,
    review_reason: ReviewReason | None,
    previous: TransferRecord | None,
) -> None:
    """Open the record's own transfer: before_history with a settlement when the
    counterpart is already held by that account's history (a manual hybrid account's first
    snapshot, or the days a manual transactions account's imports list or open with,
    manual_predates; a connected account's first record, before_history); else inferred
    with a placeholder when the counterpart is manual, or awaiting the counterpart's own
    record when it is connected.

    `previous`: the record's transfer this matching dissolved, if any. When the owner was
    told about it and the new one is the same situation (the same accounts and amount, and
    needing review or not alike), the new one counts as told too."""
    session = ctx.session
    placeholder_id: UUID | None = None
    settlement_id: UUID | None = None
    status: TransferStatus = "awaiting"
    if counterpart.source_kind == "manual":
        if await session.scalar(
            select(manual_predates(record.columns(), bound_id(counterpart.id), settings))
        ):
            settlement_id = await _post_settlement(ctx, record, counterpart, system)
            status = "before_history"
        else:
            placeholder_id = await _post_placeholder(ctx, record, counterpart, system)
            status = "inferred"
    elif await session.scalar(
        select(
            before_history(
                record.columns(),
                history_start(
                    bound_id(counterpart.id),
                    (await system_account_ids(session)).get("opening_balances"),
                ),
                settings,
            )
        )
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


async def _release_stray(ctx: CommandContext, transaction_id: UUID, system: _System) -> None:
    """Re-point the In transit posting of a recorded transaction that can't be a leg (one
    with a fee posting besides, say) back to Unassigned: only a transfer's leg posts there
    (design §5.5). Its transfer, if it had one, is dissolved already."""
    t, p = models.Transaction, models.Posting
    on_in_transit = await ctx.session.scalar(
        select(func.count())
        .select_from(p)
        .join(t, t.id == p.transaction_id)
        .where(
            p.transaction_id == transaction_id,
            p.account_id == system.in_transit,
            t.status != "void",
            t.origin.not_in(DERIVED_ORIGINS),
        )
    )
    if not on_in_transit or await is_active_leg(ctx.session, transaction_id):
        return
    if on_in_transit > 1:
        # Which one is the contra is ambiguous; find_transfer_violations reports it.
        logger.warning("transaction %s posts on In transit more than once", transaction_id)
        return
    await repoint_contra(ctx, transaction_id, system.in_transit, system.unassigned)


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
    conversion_fees: Sequence[LegFee] | None = None,
) -> tuple[list[UUID], set[UUID]]:
    """Link the record with its unique partner: into the partner's open transfer when it
    has one between the same accounts (so that transfer's placeholder or settlement goes),
    else as a new matched transfer after dissolving whatever the partner was in. A partner
    in another currency (`conversion_fees` not None: the legs' fees) always makes a new
    one, in the sent currency, settled by the conversion. Returns the records left without
    their transfer, and the partner (none linked when the partner changed under us)."""
    session = ctx.session
    partner = await load_own_record(session, ids, partner_id)
    if partner is None:
        # The pass's relation found it eligible, but a concurrent sync has since voided or
        # reshaped it; the heal after that sync, or the next pass, matches the record again.
        logger.warning(
            "transaction %s changed while %s was matched; left for the next heal",
            partner_id,
            record.id,
        )
        return [], set()
    conversion = None
    if conversion_fees is not None:
        conversion = _conversion(record, partner, conversion_fees, system)
        if conversion is None:
            # The pass's relation found the pair's fee fits, but a concurrent sync has since
            # re-posted a leg so it doesn't; the heal after that sync decides again.
            logger.warning(
                "transaction %s or %s changed while they were matched; left for the next heal",
                record.id,
                partner_id,
            )
            return [], set()
    others: list[UUID] = []
    waiting = await _waiting_transfer(session, partner.id)
    if (
        waiting is None
        or conversion_fees is not None
        or (waiting.from_account_id, waiting.to_account_id) != _accounts(record, partner.account_id)
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
        await _link_records(ctx, record, partner, conversion)
    return others, {partner.id}


async def _match_one(
    ctx: CommandContext,
    transaction_id: UUID,
    settings: TransferSettings,
    system: _System,
    relation: PairRelation,
    dissolved: _Dissolved,
) -> tuple[list[UUID], set[UUID]]:
    """Match one record from scratch; returns other records left without their transfer,
    and the own record it was linked with, if any.

    Order independence: a record is linked only with a partner it pairs with uniquely, and
    which pairs uniquely with it (the pair relation both ways). That outcome, and the
    review reason, depend on the records alone, never on which arrived first."""
    session = ctx.session
    others = await _dissolve(ctx, transaction_id, dissolved)
    ids = await system_account_ids(session)
    record = await load_own_record(session, ids, transaction_id)
    if record is None:
        # Void, derived or not shaped like a transfer's leg: no transfer, so nothing of it
        # stays on In transit.
        await _release_stray(ctx, transaction_id, system)
        return others, set()
    # 1. The owner said it is not a transfer: it is an own record of the rejection.
    rejection = await rejection_of(session, record.source)
    if rejection is not None:
        await _set_contra(ctx, record, system.unassigned)
        await _rejoin(ctx, rejection, record)
        return others, set()
    # 2, 3 and 4. Paired by the owner, by the registry, or by amount and date: link a
    # unique pair.
    partner = relation.partner(record.id)
    if partner is not None:
        fees = None
        if relation.converts.get(record.id) == partner:
            fees = leg_fees(relation.records[record.id], relation.records[partner])
        more, linked = await _link_pair(ctx, ids, record, partner, system, dissolved, fees)
        return others + more, linked
    counterpart_id = _registry_counterpart(record)
    if counterpart_id is None:
        # 5. Not a transfer, or not one yet: a candidate's own transfer carries the review.
        await _set_contra(ctx, record, system.unassigned)
        return others, set()
    # 3. Identified, with no unique partner: its own transfer, for review if it has any.
    if not await _set_contra(ctx, record, system.in_transit):
        return others, set()
    review_reason: ReviewReason | None = None
    if relation.several(record.id):
        review_reason = "several_candidates"
    elif relation.candidates(record.id, exact=False):
        review_reason = "amount_differs"
    counterpart = await get_account(session, counterpart_id)
    await _open(ctx, record, counterpart, system, settings, review_reason, dissolved.get(record.id))
    return others, set()


async def _match(
    ctx: CommandContext,
    transaction_id: UUID,
    settings: TransferSettings,
    system: _System,
    relation: PairRelation,
    dissolved: _Dissolved,
    excluding: Collection[UUID] = (),
) -> set[UUID]:
    """match_transaction under the lock, reading `relation` (the pair relation as the
    ledger stands, which matching doesn't change); returns every record this matched afresh
    or linked, all of which then agree with the rule. Records in `excluding` are treated as
    gone: never matched, and never anyone's candidate (the relation must leave them out
    too)."""
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
        others, partners = await _match_one(ctx, current, settings, system, relation, dissolved)
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
    await _match_all(ctx, [transaction_id])


async def _match_all(ctx: CommandContext, transaction_ids: list[UUID]) -> None:
    """_match each record in turn, under the lock, all reading one pair relation."""
    settings = await load_transfer_settings(ctx.session)
    system = await _system(ctx)
    relation = await pair_relation(ctx.session, await system_account_ids(ctx.session), settings)
    dissolved: _Dissolved = {}
    for transaction_id in transaction_ids:
        await _match(ctx, transaction_id, settings, system, relation, dissolved)


async def heal_transfers(ctx: CommandContext) -> int:
    """Match every record whose state disagrees with the rule, until none does; returns how
    many records it matched. Right after one heal, another writes nothing."""
    await lock_transfers(ctx)
    settings = await load_transfer_settings(ctx.session)
    matched = 0
    dissolved: _Dissolved = {}
    for _ in range(_MAX_HEAL_PASSES):
        # Matching doesn't change the pair relation, so one serves the whole pass.
        relation = await pair_relation(ctx.session, await system_account_ids(ctx.session), settings)
        due = await records_to_heal(ctx.session, settings, relation)
        if not due:
            return matched
        system = await _system(ctx)
        # A record linked earlier in this pass already agrees with the rule.
        done: set[UUID] = set()
        for transaction_id in due:
            if transaction_id in done:
                continue
            done |= await _match(ctx, transaction_id, settings, system, relation, dissolved)
            matched += 1
    logger.error("transfers still disagree with the rule after %d heal passes", _MAX_HEAL_PASSES)
    return matched


def decision_change_key(
    kind: str, subject: tuple[str, str], other: tuple[str | None, str | None]
) -> str:
    return f"transfer_decision:{kind}:{subject[0]}:{subject[1]}:{other[0]}:{other[1]}"


async def forget_decisions(
    ctx: CommandContext, kind: str, first: tuple[str, str], second: tuple[str, str] | None = None
) -> None:
    """Delete the decisions of this kind about the record (reject), or about the two
    records either way round (pair, never_pair)."""
    d = models.TransferDecision

    def about(subject: tuple[str, str], other: tuple[str, str] | None) -> ColumnElement[bool]:
        condition = and_(d.subject_kind == subject[0], d.subject_ref == subject[1])
        if other is None:
            return condition
        return and_(condition, d.other_kind == other[0], d.other_ref == other[1])

    which = (
        about(first, None) if second is None else or_(about(first, second), about(second, first))
    )
    deleted = await ctx.session.execute(
        delete(d)
        .where(d.kind == kind, which)
        .returning(d.id, d.subject_kind, d.subject_ref, d.other_kind, d.other_ref)
    )
    for row in deleted:
        ctx.record_change(
            decision_change_key(
                kind, (row.subject_kind, row.subject_ref), (row.other_kind, row.other_ref)
            ),
            {"before": {"id": str(row.id), "kind": kind}, "after": None},
        )


async def _release_rejected(ctx: CommandContext, transaction_id: UUID) -> None:
    """Take the transaction out of a rejected transfer, which stays, with its decisions,
    even with no own record left: a row imported again under the same reference goes back
    into it (_rejoin), and its Undo still withdraws the rejection."""
    session = ctx.session
    tr = models.Transfer
    for transfer in await load_transfers(
        session,
        tr.status == "rejected",
        or_(tr.out_transaction_id == transaction_id, tr.in_transaction_id == transaction_id),
    ):
        side = (
            "out_transaction_id"
            if transfer.out_transaction_id == transaction_id
            else "in_transaction_id"
        )
        await session.execute(update(tr).where(tr.id == transfer.id).values({side: None}))
        publish_transfer(
            ctx,
            "finance.transfer.unlinked",
            transfer.id,
            transfer,
            await _get(session, transfer.id),
        )


async def _rejoin(ctx: CommandContext, transfer_id: UUID, record: OwnRecord) -> None:
    """Put a rejected record back into the rejection its decision names (a row rolled back
    and imported again under the same reference), when the side for its role is free."""
    session = ctx.session
    tr = models.Transfer
    before = await _get(session, transfer_id)
    side = "out_transaction_id" if record.role == "out" else "in_transaction_id"
    rejoined = await session.scalar(
        update(tr)
        .where(
            tr.id == transfer_id,
            rejoins(
                bound_id(record.id),
                literal(record.quantity, Numeric()),
                bound_id(record.account_id),
            ),
        )
        .values({side: record.id})
        .returning(tr.id)
    )
    if rejoined is not None:
        publish_transfer(
            ctx, "finance.transfer.relinked", transfer_id, before, await _get(session, transfer_id)
        )
    elif record.id not in (before.out_transaction_id, before.in_transaction_id):
        logger.warning(
            "rejected transaction %s does not fit the rejection %s its decision names",
            record.id,
            transfer_id,
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
    gone = {transaction_id}
    relation = await pair_relation(
        session, await system_account_ids(session), settings, excluding=gone
    )
    for other in others:
        await _match(ctx, other, settings, system, relation, dissolved, excluding=gone)


async def preview_match(
    session: AsyncSession,
    *,
    account_id: UUID,
    instrument_id: UUID,
    quantity: Decimal,
    occurred_at: datetime,
    counterparty: CounterpartyFields,
    source: SourceRef | None = None,
    booking_date: date | None = None,
    complete: tuple[date, date] | None = None,
) -> UUID | None:
    """The own record a row not yet posted would be linked with if it were posted now
    (Decision 6 steps 2-4, without writing): its unique partner in the pair relation
    matching reads, with the row added; None when it has none (no candidate, or not
    unique). Posting the row links it with that record, completing the record's open
    transfer, or replacing whatever transfer the record is in.

    `source`: the reference the row will have once posted (a re-imported row keeps its
    own), so the owner's decisions about it apply, and its place among identical rows
    (pairs._twins) is known; None for a row with none yet.
    `booking_date`: the one the row will be posted with, if given; else it gets the one
    posting would give it (ledger.resolve_booking_date), which the window is counted in.
    `complete`: the days the row's import lists in full, which committing it would add
    (a counterpart found by amount and date is taken only from the first day imports list
    in full, design §5.8).

    The row's evidence is its counterparty identifier only: `counterparty.account_number`
    is not read. Only connectors give one (transfer-evidence design §5.1, item 2), and they
    post without previewing."""
    system = await system_account_ids(session)
    settings = await load_transfer_settings(session)
    holder = (
        None
        if counterparty.identifier_id is None
        else await identifier_account(session, counterparty.identifier_id)
    )
    row = Pairable(
        id=_UNPOSTED,
        source=("unposted", "") if source is None else source,
        account_id=account_id,
        instrument_id=instrument_id,
        quantity=quantity,
        occurred_at=occurred_at,
        booking_date=await resolve_booking_date(session, occurred_at, booking_date),
        holder=holder,
        bank_transfer=counterparty.bank_transfer,
        # A row naming a bank account, someone else's too, is never a weak counterpart.
        names_account=counterparty.identifier_id is not None,
    )
    listing = [] if complete is None else [(account_id, instrument_id, complete[0])]
    relation = await pair_relation(session, system, settings, adding=[row], listing=listing)
    if source is None and row.id in relation.twins:
        return None  # identical ones pair by reference (pairs._twins): which one is unknown
    return relation.partner(row.id)


async def _rematch_naming(ctx: CommandContext, identifier_id: UUID) -> None:
    """Match again every record naming the identifier, then heal."""
    await lock_transfers(ctx)
    t = models.Transaction
    naming = await ctx.session.scalars(
        select(t.id)
        .where(t.counterparty_identifier_id == identifier_id)
        .order_by(t.occurred_at, t.id)
    )
    await _match_all(ctx, list(naming))
    await heal_transfers(ctx)


async def unassign_identifier_and_rematch(
    ctx: CommandContext, identifier_id: UUID
) -> IdentifierRecord:
    """unassign_identifier, then match again every record naming that bank account, which
    no longer identifies one of the owner's accounts."""
    record = await unassign_identifier(ctx, identifier_id)
    await _rematch_naming(ctx, identifier_id)
    return record


async def register_card_merchant_and_rematch(
    ctx: CommandContext, merchant_id: str, account_ids: Sequence[UUID]
) -> IdentifierRecord:
    """register_card_merchant, then match again every record naming the merchant, so the
    new holders take effect at once. Records that don't name it yet (posted before it was
    registered) are named by their connector's next normalize (Monzo's replays them)."""
    record = await register_card_merchant(ctx, merchant_id, account_ids)
    await _rematch_naming(ctx, record.id)
    return record
