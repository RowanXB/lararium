"""The owner's decisions about transfers (design §5.9): this is not a transfer (reject),
these two are not one transfer (unpair), and these two are (pair).

Decisions are keyed by the records' source references: matching dissolves and reopens
transfers (a new id each time), while a record's reference holds across re-syncs. So each
function finds the transfer's records when it is called, stores what it decides about
them, and lets matching bring the ledger in line. A reject also names its rejected
transfer, which matching never dissolves: a record rolled back and imported again goes
back into it, and its Undo withdraws exactly that rejection's decisions."""

from uuid import UUID

from sqlalchemy import and_, delete, or_, update
from sqlalchemy.dialects.postgresql import insert

from lararium.core.commands import CommandContext
from lararium.modules.finance import models
from lararium.modules.finance.errors import FinanceValidationError
from lararium.modules.finance.ledger import delete_derived_transaction
from lararium.modules.finance.transfers.contra import OwnLeg, own_leg, source_ref
from lararium.modules.finance.transfers.engine import (
    decision_change_key,
    forget_decisions,
    heal_transfers,
    lock_transfers,
    match_transaction,
    publish_transfer,
)
from lararium.modules.finance.transfers.queries import (
    OPEN_STATUSES,
    TransferRecord,
    candidates,
    get_transfer,
    load_transfers,
    require_pairable,
    weak_transfers,
)


def _records(transfer: TransferRecord) -> list[UUID]:
    """The transfer's own records (not its placeholder)."""
    return [
        leg for leg in (transfer.out_transaction_id, transfer.in_transaction_id) if leg is not None
    ]


async def _store(
    ctx: CommandContext,
    kind: str,
    subject: tuple[str, str],
    other: tuple[str, str] | None = None,
    *,
    transfer_id: UUID | None = None,
) -> None:
    """Store a decision, unless the same one is stored already. A reject (with the
    rejected transfer it is made on) is one per record: storing it again moves it to the
    new transfer, so a leftover can never leave a new rejection without its record."""
    other_kind, other_ref = (None, None) if other is None else other
    d = models.TransferDecision
    statement = insert(d).values(
        kind=kind,
        subject_kind=subject[0],
        subject_ref=subject[1],
        other_kind=other_kind,
        other_ref=other_ref,
        transfer_id=transfer_id,
    )
    if kind == "reject":
        statement = statement.on_conflict_do_update(
            index_elements=[d.kind, d.subject_kind, d.subject_ref, d.other_kind, d.other_ref],
            set_={"transfer_id": statement.excluded.transfer_id},
            where=d.transfer_id.is_distinct_from(statement.excluded.transfer_id),
        )
    else:
        statement = statement.on_conflict_do_nothing()
    stored = await ctx.session.scalar(statement.returning(d.id))
    if stored is not None:
        ctx.record_change(
            decision_change_key(kind, subject, (other_kind, other_ref)),
            {"before": None, "after": {"id": str(stored), "kind": kind}},
        )


async def reject(ctx: CommandContext, transfer_id: UUID) -> TransferRecord:
    """The owner says the transfer is not one: a reject decision for each of its own
    records, so neither opens a transfer again; the transfer stays, rejected, without its
    placeholder or settlement, and its records go back to Unassigned."""
    await lock_transfers(ctx)
    session = ctx.session
    transfer = await get_transfer(session, transfer_id)
    if transfer.status == "rejected":
        raise FinanceValidationError("the transfer is already rejected")
    records = _records(transfer)
    for record_id in records:
        await _store(ctx, "reject", await source_ref(session, record_id), transfer_id=transfer_id)
    tr = models.Transfer
    await session.execute(
        update(tr)
        .where(tr.id == transfer_id)
        .values(
            status="rejected",
            review_reason=None,
            placeholder_transaction_id=None,
            settlement_transaction_id=None,
        )
    )
    for derived in (transfer.placeholder_transaction_id, transfer.settlement_transaction_id):
        if derived is not None:
            await delete_derived_transaction(ctx, derived)
    # Matching a rejected record puts its contra back on Unassigned.
    for record_id in records:
        await match_transaction(ctx, record_id)
    rejected = await get_transfer(session, transfer_id)
    publish_transfer(ctx, "finance.transfer.rejected", transfer_id, transfer, rejected)
    await heal_transfers(ctx)
    return await get_transfer(session, transfer_id)


async def _forget_rejection(ctx: CommandContext, transfer_id: UUID) -> None:
    """Delete the rejected transfer's reject decisions, those of records rolled back since
    included."""
    d = models.TransferDecision
    deleted = await ctx.session.execute(
        delete(d)
        .where(d.kind == "reject", d.transfer_id == transfer_id)
        .returning(d.id, d.subject_kind, d.subject_ref)
    )
    for row in deleted:
        ctx.record_change(
            decision_change_key("reject", (row.subject_kind, row.subject_ref), (None, None)),
            {"before": {"id": str(row.id), "kind": "reject"}, "after": None},
        )


async def unreject(ctx: CommandContext, transfer_id: UUID) -> None:
    """Withdraw a rejection: its decisions (every one naming it) and the rejected transfer
    go, and its records are matched again."""
    await lock_transfers(ctx)
    session = ctx.session
    transfer = await get_transfer(session, transfer_id)
    if transfer.status != "rejected":
        raise FinanceValidationError("only a rejected transfer can be unrejected")
    records = _records(transfer)
    await _forget_rejection(ctx, transfer_id)
    tr = models.Transfer
    await session.execute(delete(tr).where(tr.id == transfer_id))
    publish_transfer(ctx, "finance.transfer.unrejected", transfer_id, transfer, None)
    for record_id in records:
        await match_transaction(ctx, record_id)
    await heal_transfers(ctx)


async def unpair(ctx: CommandContext, transfer_id: UUID) -> None:
    """The owner says the matched transfer's two records are not one transfer, though each
    may still be one: never pair them (both ways round), and match each again. A pair a sort
    code alone made (weak_transfers) is refused, before anything is stored: weak evidence
    makes no transfer of its own, so neither record would keep one listing the other as
    Unpaired, and there would be no way back. Reject, with its Undo, is the way."""
    await lock_transfers(ctx)
    session = ctx.session
    transfer = await get_transfer(session, transfer_id)
    if transfer.status != "matched" or len(_records(transfer)) != 2:
        raise FinanceValidationError("only a matched transfer of two own records can be unpaired")
    if transfer.id in await weak_transfers(session, [transfer]):
        raise FinanceValidationError("a pair made by sort code alone can only be rejected")
    sent, received = _records(transfer)
    sent_ref, received_ref = await source_ref(session, sent), await source_ref(session, received)
    await forget_decisions(ctx, "pair", sent_ref, received_ref)
    await _store(ctx, "never_pair", sent_ref, received_ref)
    await _store(ctx, "never_pair", received_ref, sent_ref)
    # Matching the sender dissolves the transfer, and matches the receiver after it.
    await match_transaction(ctx, sent)
    await heal_transfers(ctx)


async def _store_pair(ctx: CommandContext, present: UUID, chosen: UUID) -> TransferRecord:
    """Store the owner's pair decision for the two records (withdrawing any never_pair
    between them), match, and return the matched transfer. When it still can't link them,
    raise, which rolls the decision back."""
    session = ctx.session
    present_ref, chosen_ref = await source_ref(session, present), await source_ref(session, chosen)
    await forget_decisions(ctx, "never_pair", present_ref, chosen_ref)
    await _store(ctx, "pair", present_ref, chosen_ref)
    await match_transaction(ctx, present)
    await heal_transfers(ctx)
    tr = models.Transfer
    paired = await load_transfers(
        session,
        tr.status == "matched",
        or_(
            and_(tr.out_transaction_id == present, tr.in_transaction_id == chosen),
            and_(tr.out_transaction_id == chosen, tr.in_transaction_id == present),
        ),
    )
    if not paired:
        # Raising rolls the decisions back with the rest of the command.
        raise FinanceValidationError(
            f"pairing with transaction {chosen} did not take effect: another pair"
            " decision leaves it ambiguous"
        )
    return paired[0]


async def pair(ctx: CommandContext, transfer_id: UUID, transaction_id: UUID) -> TransferRecord:
    """The owner says the open transfer's missing side is this record: one of its
    candidates of the same amount, or one the owner unpaired from it (whose never_pair
    decisions this withdraws). Pairing them outranks any other candidate; when it still
    can't link them (another pair decision makes one of them ambiguous), nothing is
    stored."""
    await lock_transfers(ctx)
    session = ctx.session
    transfer = await get_transfer(session, transfer_id)
    if transfer.status not in OPEN_STATUSES:
        raise FinanceValidationError("only a transfer missing one side can be paired")
    found = {
        c.reason
        for c in await candidates(session, transfer_id)
        if c.transaction_id == transaction_id
    }
    if not found & {"exact", "unpaired"}:
        raise FinanceValidationError(
            f"transaction {transaction_id} is not a candidate of the same amount for this transfer"
        )
    [present] = _records(transfer)
    return await _store_pair(ctx, present, transaction_id)


async def pair_records(ctx: CommandContext, transaction_id: UUID, other_id: UUID) -> TransferRecord:
    """The owner says these two records are one transfer, whether or not either is in
    one: own records on different accounts, in one instrument, of opposite and equal
    amounts. Neither may be a leg of a matched transfer (unpair it first) nor rejected
    (undo the rejection first: a pair never silently overrides one). Otherwise as pair."""
    await lock_transfers(ctx)
    session = ctx.session
    if transaction_id == other_id:
        raise FinanceValidationError("a record cannot be paired with itself")
    legs: list[OwnLeg] = []
    for record_id in (transaction_id, other_id):
        await source_ref(session, record_id)  # unknown ids raise here
        leg = await own_leg(session, record_id)
        if leg is None:
            raise FinanceValidationError(f"transaction {record_id} cannot be a transfer's leg")
        legs.append(leg)
    first, second = legs
    if first.account_id == second.account_id:
        raise FinanceValidationError("the two records are on the same account")
    if first.instrument_id != second.instrument_id:
        raise FinanceValidationError("the two records are in different instruments")
    if first.quantity != -second.quantity:
        raise FinanceValidationError("the two records are not of opposite and equal amounts")
    for record_id in (transaction_id, other_id):
        await require_pairable(session, record_id)
    return await _store_pair(ctx, transaction_id, other_id)
